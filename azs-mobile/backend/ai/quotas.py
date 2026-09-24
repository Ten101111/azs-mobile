"""Ролевые лимиты сложности ИИ (ИИ-03) и мягкий режим решения №12.

Решение владельца Р-2 (23.09.2026): для уровней «Лёгкий» и «Средний» мягкий
режим — счётных лимитов нет; квоты вводятся только для тяжёлых задач
(«Высокий», позже — презентации, отложенные задачи, изображения). Для всех:
не больше двух одновременных вопросов от человека и алерт администратору,
если человек за день задал больше 50 вопросов.

Кто считается:
  * квоты, одновременные вопросы и длина вопроса — по человеку, который
    спрашивает (его учётная запись и роль), даже когда администратор смотрит
    ответ «от имени» другой роли;
  * предел времени и строк по уровню — по действующей роли, как и бюджет шагов
    агента (backend/ai/limits.py): проверка «от имени» показывает то, что
    увидит сама роль. У администратора пределов нет (решение от 23.09.2026).

Тяжёлые задачи («Высокий») идут через общую очередь сервера: одновременно не
больше `heavy_slots` (2 на текущем Mac), администратор и субадминистратор —
в начале очереди. Запуск засчитывается в квоту, когда задача вышла из очереди
и начала работу; сбой модели или сервера квоту возвращает.

Значения по умолчанию — таблица ниже (стартовые значения Р-2). Вкладка
«Лимиты ИИ» (ИИ-26, backend/ai/limit_settings.py) хранит правки администратора
в базе журнала; здесь они читаются через кэш на CACHE_SECONDS — новое значение
действует со следующего вопроса, не позже чем через минуту, без перезапуска.
"""
from __future__ import annotations

import itertools
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import journal

MSK = timezone(timedelta(hours=3))

# Роль → столбец таблицы Р-2. Незнакомая роль получает самые строгие значения.
GROUPS = {
    "admin": "admin", "subadmin": "admin",
    "aup_network": "network",
    "aup_npo": "npo",
    "regional_manager": "ru",
}
GROUP_TITLES = {"admin": "Админ / Субадмин", "network": "АУП сети", "npo": "АУП общества", "ru": "РУ"}
STRICTEST = "ru"


def _all(value: Any) -> dict[str, Any]:
    return {group: value for group in GROUP_TITLES}


# Параметр → значение по группам; None — без лимита.
DEFAULTS: dict[str, dict[str, Any]] = {
    "deep_per_day": {"admin": None, "network": 30, "npo": 20, "ru": 10},
    "concurrent": _all(2),
    "question_chars": {"admin": 4000, "network": 2000, "npo": 2000, "ru": 2000},
    "seconds_fast": _all(60),
    "seconds_analyze": _all(120),
    "seconds_deep": _all(240),
    "rows_fast": _all(200),
    "rows_analyze": _all(1000),
    "rows_deep": {"admin": 2000, "network": 2000, "npo": 2000, "ru": 1000},
    "priority": {"admin": "high", "network": "normal", "npo": "normal", "ru": "normal"},
    # Хранение (ИИ-02, Р-2): срок истории, число диалогов, память диалога.
    "history_days": {"admin": 365, "network": 180, "npo": 180, "ru": 90},
    "active_dialogs": {"admin": None, "network": 100, "npo": 60, "ru": 40},
    "pinned_dialogs": {"admin": 20, "network": 15, "npo": 10, "ru": 10},
    "folders": {"admin": 30, "network": 20, "npo": 15, "ru": 10},
    "dialog_memory": {"admin": 10, "network": 8, "npo": 8, "ru": 6},
    # Появятся с ИИ-07 и ИИ-11 (файлы и память папки); значения утверждены заранее (Р-2).
    "folder_memory_chars": _all(2000),
    "files_per_folder": _all(5),
    "user_files_mb": {"admin": 500, "network": 300, "npo": 200, "ru": 100},
    # Появятся с ИИ-22, ИИ-24 и ИИ-19; значения утверждены заранее (Р-2).
    "presentations_per_day": {"admin": None, "network": 10, "npo": 5, "ru": 3},
    "scheduled_tasks": {"admin": 20, "network": 10, "npo": 10, "ru": 5},
    "images_per_day": {"admin": 20, "network": 10, "npo": 5, "ru": 0},
}

PARAM_TITLES = {
    "deep_per_day": "«Высокий», запусков в день",
    "concurrent": "Одновременных вопросов",
    "question_chars": "Длина вопроса, знаков",
    "seconds_fast": "Предел времени «Лёгкий», с",
    "seconds_analyze": "Предел времени «Средний», с",
    "seconds_deep": "Предел времени «Высокий», с",
    "rows_fast": "Строк в результате «Лёгкий»",
    "rows_analyze": "Строк в результате «Средний»",
    "rows_deep": "Строк в результате «Высокий»",
    "priority": "Приоритет в очереди тяжёлых задач",
    "presentations_per_day": "Презентаций в день",
    "scheduled_tasks": "Активных отложенных задач",
    "images_per_day": "Изображений в день",
    "history_days": "История диалогов, дней",
    "active_dialogs": "Активных диалогов",
    "pinned_dialogs": "Закреплённых диалогов",
    "folders": "Папок",
    "dialog_memory": "Память диалога, прошлых ходов",
    "folder_memory_chars": "Память папки, знаков",
    "files_per_folder": "Файлов на папку",
    "user_files_mb": "Объём файлов пользователя, МБ",
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Общие значения: не по ролям.
GENERAL: dict[str, Any] = {
    "heavy_slots": _env_int("AI_HEAVY_SLOTS", 2),        # одновременных «Высоких» на сервере
    "daily_alert": _env_int("AI_DAILY_ALERT", 50),       # решение №12: алерт при большем числе вопросов в день
    "queue_wait_s": _env_int("AI_QUEUE_WAIT", 600),      # дольше в очереди не ждём — отвечаем на «Среднем»
    "alert_recipient": (os.environ.get("AI_ALERT_RECIPIENT") or "").strip(),  # пусто — администраторы
    "audit_days": _env_int("AI_AUDIT_DAYS", 365),        # журнал аудита живёт дольше истории (ИИ-02)
}

# Правки поверх значений по умолчанию: _overrides — для тестов и скриптов,
# _stored — из «Лимитов ИИ» (база), перечитываются раз в CACHE_SECONDS.
CACHE_SECONDS = 60
_overrides: dict[tuple[str, str], Any] = {}
_cache: dict[str, Any] = {"at": float("-inf"), "values": {}}


def set_overrides(values: dict[tuple[str, str], Any]) -> None:
    """Значения поверх всего остального (тесты, скрипты): (группа, параметр) → значение."""
    _overrides.clear()
    _overrides.update(values)


def refresh() -> None:
    """Перечитать правки из базы при следующем обращении — после сохранения во вкладке."""
    _cache["at"] = float("-inf")


def _stored() -> dict[tuple[str, str], Any]:
    now = time.monotonic()
    if now - _cache["at"] >= CACHE_SECONDS:
        try:
            from . import limit_settings

            _cache["values"] = limit_settings.load()
        except Exception:  # noqa: BLE001 - без базы действуют значения по умолчанию
            pass
        _cache["at"] = now
    return _cache["values"]


def group_of(role: str | None) -> str:
    return GROUPS.get((role or "").strip(), STRICTEST)


def _lookup(key: tuple[str, str], default: Any) -> Any:
    for source in (_overrides, _stored()):
        if key in source:
            return source[key]
    return default


def value(role: str | None, param: str) -> Any:
    group = group_of(role)
    return _lookup((group, param), DEFAULTS[param][group])


def general(name: str) -> Any:
    return _lookup(("general", name), GENERAL[name])


# Общий потолок длины вопроса в API; предел роли — question_chars.
MAX_QUESTION_CHARS = 4000


def question_limit(role: str | None) -> int:
    return min(MAX_QUESTION_CHARS, int(value(role, "question_chars")))


# --- учёт запусков ------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS ai_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key        TEXT NOT NULL,
    actor           TEXT,
    role            TEXT,
    created_at      INTEGER NOT NULL,
    day             TEXT NOT NULL,
    depth_requested TEXT,
    depth           TEXT,
    status          TEXT NOT NULL,
    reason          TEXT,
    heavy           INTEGER NOT NULL DEFAULT 0,
    wait_ms         INTEGER,
    finished_at     INTEGER,
    journal_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ai_runs_user_day ON ai_runs(user_key, day);
CREATE INDEX IF NOT EXISTS idx_ai_runs_created ON ai_runs(created_at);
CREATE TABLE IF NOT EXISTS ai_limit_hits (
    day   TEXT NOT NULL,
    grp   TEXT NOT NULL,
    param TEXT NOT NULL,
    n     INTEGER NOT NULL,
    PRIMARY KEY (day, grp, param)
);
CREATE TABLE IF NOT EXISTS ai_alerts (
    day        TEXT NOT NULL,
    user_key   TEXT NOT NULL,
    actor      TEXT,
    questions  INTEGER NOT NULL,
    threshold  INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (day, user_key)
);
"""
# status: running — идёт; ok — ответ; failed — сбой; cancelled — остановлен человеком;
#         rejected — не принят (reason: concurrent, deep_quota, question_chars).
# reason у принятых: downgraded — «Высокий» заменён «Средним» (квота или очередь).

REASON_TITLES = {
    "concurrent": "два вопроса уже идут",
    "deep_quota": "«Высокий» израсходован на сегодня",
    "question_chars": "вопрос длиннее лимита",
    "downgraded": "«Высокий» заменён «Средним»",
    "queue_timeout": "очередь «Высокого» не дошла",
}


def record_hit(role: str | None, param: str) -> None:
    """Человек упёрся в лимит без запуска вопроса (закрепить, папка, активные диалоги) — для «Лимитов ИИ»."""
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO ai_limit_hits (day, grp, param, n) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(day, grp, param) DO UPDATE SET n = n + 1",
                (today(), group_of(role), param))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - счётчик не должен ломать действие
        pass

# Алерт решения №12 уходит письмом (main.py ставит отправителя, ИИ-26 — получателя).
alert_sink: Callable[[dict], None] | None = None

clock: Callable[[], float] = time.time


def _connect() -> sqlite3.Connection:
    path = journal.JOURNAL_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript(DDL)
    return conn


def today() -> str:
    return datetime.fromtimestamp(clock(), MSK).date().isoformat()


def _heavy_used(conn: sqlite3.Connection, user_key: str, day: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM ai_runs WHERE user_key = ? AND day = ? AND heavy = 1",
                       (user_key, day)).fetchone()
    return int(row[0] or 0)


def _questions(conn: sqlite3.Connection, user_key: str, day: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM ai_runs WHERE user_key = ? AND day = ? AND status != 'rejected'",
                       (user_key, day)).fetchone()
    return int(row[0] or 0)


def user_key_of(user) -> str:
    uid = getattr(user, "id", None)
    return f"id:{uid}" if uid is not None else f"actor:{getattr(user, 'email', '') or '—'}"


class Refused(Exception):
    """Вопрос не принят: лимит. `suggest` — уровень, на котором можно спросить сейчас."""

    def __init__(self, reason: str, message: str, suggest: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.suggest = suggest


# Тексты — для плашки «Лимит: …» в интерфейсе (src/note.jsx): подпись «Лимит» ставит
# интерфейс, здесь — суть одной-двумя фразами и что можно сделать сейчас.
def _quota_text(limit: int) -> str:
    return (f"«Высокий» на сегодня израсходован: {limit} из {limit}. "
            "Счётчик обнулится в 00:00 МСК, а пока можно спросить на «Среднем».")


@dataclass
class Run:
    id: int
    user_key: str
    role: str
    priority: int                    # 0 — высокий, 1 — обычный
    seq: int
    cancel: threading.Event = field(default_factory=threading.Event)
    heavy_slot: bool = False
    counted: bool = False
    wait_ms: int = 0
    reason: str = ""


class Runs:
    """Идущие вопросы и очередь тяжёлых задач одного процесса сервера."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._active: dict[int, Run] = {}
        self._waiting: list[Run] = []
        self._heavy: set[int] = set()
        self._seq = itertools.count()

    # --- приём вопроса -----------------------------------------------------
    def _reject(self, conn, user_key: str, actor: str | None, role: str, depth: str, reason: str) -> None:
        now = int(clock())
        conn.execute(
            "INSERT INTO ai_runs (user_key, actor, role, created_at, day, depth_requested, status, reason, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'rejected', ?, ?)",
            (user_key, actor, role, now, today(), depth, reason, now))
        conn.commit()

    def check_question(self, user, role: str, question: str, depth: str, actor: str | None = None) -> None:
        limit = question_limit(role)
        if len(question or "") > limit:
            conn = _connect()
            try:
                self._reject(conn, user_key_of(user), actor, role, depth, "question_chars")
            finally:
                conn.close()
            raise Refused("question_chars", f"Вопрос длиннее {limit} знаков. Сократите его и отправьте снова.")

    def start(self, user, role: str, depth: str, actor: str | None = None) -> Run:
        """Принять вопрос: не больше `concurrent` одновременно; «Высокий» — в пределах квоты."""
        user_key = user_key_of(user)
        conn = _connect()
        try:
            with self._cond:
                mine = [run for run in self._active.values() if run.user_key == user_key]
                allowed = int(value(role, "concurrent"))
                if len(mine) >= allowed:
                    self._reject(conn, user_key, actor, role, depth, "concurrent")
                    raise Refused("concurrent", f"Уже идут {_count_word(allowed)} ваших вопроса. Дождитесь "
                                  "ответа или остановите один из них.")
                day = today()
                limit = value(role, "deep_per_day")
                if depth == "deep" and limit is not None and _heavy_used(conn, user_key, day) >= int(limit):
                    self._reject(conn, user_key, actor, role, depth, "deep_quota")
                    raise Refused("deep_quota", _quota_text(int(limit)), suggest="analyze")
                now = int(clock())
                cursor = conn.execute(
                    "INSERT INTO ai_runs (user_key, actor, role, created_at, day, depth_requested, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'running')", (user_key, actor, role, now, day, depth))
                run = Run(id=int(cursor.lastrowid), user_key=user_key, role=role,
                          priority=0 if value(role, "priority") == "high" else 1, seq=next(self._seq))
                self._alert(conn, user_key, actor, day, now)
                conn.commit()
                self._active[run.id] = run
                return run
        finally:
            conn.close()

    def _alert(self, conn, user_key: str, actor: str | None, day: str, now: int) -> None:
        """Решение №12: больше порога вопросов за день — строка для администратора и письмо один раз в день."""
        threshold = int(general("daily_alert"))
        asked = _questions(conn, user_key, day)
        if asked <= threshold:
            return
        first = conn.execute("SELECT 1 FROM ai_alerts WHERE day = ? AND user_key = ?", (day, user_key)).fetchone() is None
        if first and alert_sink is not None:
            alert = {"day": day, "actor": actor, "questions": asked, "threshold": threshold}
            threading.Thread(target=_send_alert, args=(alert,), name="ai-alert", daemon=True).start()
        conn.execute(
            "INSERT INTO ai_alerts (day, user_key, actor, questions, threshold, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day, user_key) DO UPDATE SET questions = excluded.questions, updated_at = excluded.updated_at",
            (day, user_key, actor, asked, threshold, now, now))

    # --- очередь тяжёлых задач ----------------------------------------------
    def enter_heavy(self, run: Run, on_position: Callable[[int], None] | None = None) -> str:
        """Место для «Высокого»: granted | quota | cancelled | timeout.

        Ждёт в общей очереди сервера; позиция сообщается через `on_position`.
        Квота проверяется до очереди и ещё раз при выходе из неё — за время
        ожидания другой вопрос того же человека мог её израсходовать.
        """
        limit = value(run.role, "deep_per_day")
        if limit is not None and self._used(run.user_key) >= int(limit):
            run.reason = "downgraded"
            return "quota"
        started = time.monotonic()
        deadline = started + int(general("queue_wait_s"))
        told = 0
        with self._cond:
            self._waiting.append(run)
            try:
                while True:
                    if run.cancel.is_set():
                        return "cancelled"
                    order = sorted(self._waiting, key=lambda item: (item.priority, item.seq))
                    position = order.index(run)
                    free = int(general("heavy_slots")) - len(self._heavy)
                    if position < free:
                        break
                    if time.monotonic() >= deadline:
                        run.reason = "queue_timeout"
                        return "timeout"
                    place = position - max(free, 0) + 1
                    if place != told and on_position is not None:
                        told = place
                        on_position(place)
                    self._cond.wait(timeout=1.0)
            finally:
                if run in self._waiting:
                    self._waiting.remove(run)
            run.wait_ms = int((time.monotonic() - started) * 1000)
            conn = _connect()
            try:
                if limit is not None and _heavy_used(conn, run.user_key, today()) >= int(limit):
                    run.reason = "downgraded"
                    self._cond.notify_all()
                    return "quota"
                conn.execute("UPDATE ai_runs SET heavy = 1, wait_ms = ? WHERE id = ?", (run.wait_ms, run.id))
                conn.commit()
            finally:
                conn.close()
            self._heavy.add(run.id)
            run.heavy_slot = True
            run.counted = True
            return "granted"

    def leave_heavy(self, run: Run) -> None:
        with self._cond:
            if run.heavy_slot:
                self._heavy.discard(run.id)
                run.heavy_slot = False
                self._cond.notify_all()

    def _used(self, user_key: str) -> int:
        conn = _connect()
        try:
            return _heavy_used(conn, user_key, today())
        finally:
            conn.close()

    # --- завершение и отмена -------------------------------------------------
    def finish(self, run: Run, status: str, depth: str | None = None, journal_id: int | None = None,
               refund: bool = False) -> None:
        """Закрыть вопрос. `refund` — сбой модели или сервера: «Высокий» не засчитывается."""
        self.leave_heavy(run)
        with self._cond:
            self._active.pop(run.id, None)
            self._cond.notify_all()
        conn = _connect()
        try:
            conn.execute(
                "UPDATE ai_runs SET status = ?, depth = ?, journal_id = ?, finished_at = ?, "
                "reason = COALESCE(NULLIF(?, ''), reason), heavy = CASE WHEN ? THEN 0 ELSE heavy END WHERE id = ?",
                (status, depth, journal_id, int(clock()), run.reason, 1 if refund else 0, run.id))
            conn.commit()
        finally:
            conn.close()

    def cancel(self, run_id: int, user) -> bool:
        """Остановить свой вопрос. Чужой — как несуществующий."""
        with self._cond:
            run = self._active.get(int(run_id))
            if run is None or run.user_key != user_key_of(user):
                return False
            run.cancel.set()
            self._cond.notify_all()
            return True

    def active_count(self, user) -> int:
        key = user_key_of(user)
        with self._cond:
            return sum(1 for run in self._active.values() if run.user_key == key)

    def reset(self) -> None:
        """Для тестов."""
        with self._cond:
            self._active.clear()
            self._waiting.clear()
            self._heavy.clear()


RUNS = Runs()


def _send_alert(alert: dict) -> None:
    try:
        if alert_sink is not None:
            alert_sink(alert)
    except Exception:  # noqa: BLE001 - письмо не должно ронять вопрос
        pass


def _count_word(n: int) -> str:
    return {1: "один", 2: "два", 3: "три", 4: "четыре"}.get(n, str(n))


class Control:
    """То, что конвейер знает о запуске: отмена и место в очереди тяжёлых задач."""

    def __init__(self, run: Run | None, runs: Runs | None = None) -> None:
        self.run = run
        self.runs = runs or RUNS

    def cancelled(self) -> bool:
        return bool(self.run and self.run.cancel.is_set())

    def enter_deep(self, on_position: Callable[[int], None] | None = None) -> tuple[str, str]:
        """(уровень, пометка): deep — место получено; analyze — квота или очередь; cancelled."""
        if self.run is None:
            return "deep", ""
        result = self.runs.enter_heavy(self.run, on_position)
        if result == "granted":
            return "deep", ""
        if result == "cancelled":
            return "cancelled", ""
        if result == "quota":
            limit = value(self.run.role, "deep_per_day")
            return "analyze", (f"«Высокий» на сегодня израсходован ({limit} из {limit}), "
                               "поэтому ответ подготовлен на уровне «Средний».")
        minutes = max(1, int(general("queue_wait_s")) // 60)
        return "analyze", (f"Очередь уровня «Высокий» не дошла за {minutes} мин, поэтому ответ подготовлен "
                           "на уровне «Средний»; запуск «Высокого» не засчитан.")

    def leave_deep(self) -> None:
        if self.run is not None:
            self.runs.leave_heavy(self.run)


# --- что видит человек и администратор ----------------------------------------

def usage(user, role: str) -> dict:
    """Счётчики для поля вопроса: квота «Высокого», одновременные вопросы, длина вопроса."""
    limit = value(role, "deep_per_day")
    deep = None
    if limit is not None:
        used = RUNS._used(user_key_of(user))
        deep = {"limit": int(limit), "used": used, "left": max(0, int(limit) - used)}
    return {
        "deep": deep,
        "concurrent": int(value(role, "concurrent")),
        "active": RUNS.active_count(user),
        "questionChars": question_limit(role),
        "resets": "00:00 МСК",
        "group": GROUP_TITLES[group_of(role)],
    }


def load_report(days: int = 7) -> dict:
    """Нагрузка для «Качества ИИ»: алерты решения №12 и упоры в лимиты за последние дни."""
    since_day = (datetime.fromtimestamp(clock(), MSK).date() - timedelta(days=days - 1)).isoformat()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        alerts = [dict(row) for row in conn.execute(
            "SELECT day, actor, questions, threshold FROM ai_alerts WHERE day >= ? ORDER BY day DESC, questions DESC",
            (since_day,))]
        hits = {row["reason"]: int(row["n"]) for row in conn.execute(
            "SELECT reason, COUNT(*) AS n FROM ai_runs WHERE day >= ? AND reason IS NOT NULL GROUP BY reason",
            (since_day,))}
        heavy = conn.execute(
            "SELECT COUNT(*) AS n, MAX(wait_ms) AS wait FROM ai_runs WHERE day >= ? AND heavy = 1",
            (since_day,)).fetchone()
        cancelled = conn.execute(
            "SELECT COUNT(*) FROM ai_runs WHERE day >= ? AND status = 'cancelled'", (since_day,)).fetchone()[0]
    finally:
        conn.close()
    return {
        "days": days,
        "threshold": int(general("daily_alert")),
        "alerts": alerts,
        "hits": [{"reason": key, "title": REASON_TITLES.get(key, key), "count": hits.get(key, 0)}
                 for key in ("deep_quota", "downgraded", "queue_timeout", "concurrent", "question_chars")],
        "heavyRuns": int(heavy["n"] or 0),
        "maxWaitMs": int(heavy["wait"] or 0),
        "cancelled": int(cancelled or 0),
    }
