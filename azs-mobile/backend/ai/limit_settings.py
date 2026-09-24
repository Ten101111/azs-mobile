"""Лимиты ИИ, которые администратор меняет сам (ИИ-26).

Значения по умолчанию — таблица Р-2 в quotas.py. Здесь хранятся только правки:
таблица `ai_limits` (группа ролей, параметр, значение, кто и когда изменил) и
журнал `ai_limits_log` (было → стало). Конвейер читает правки через кэш
quotas (CACHE_SECONDS = 60): новое значение действует со следующего вопроса,
не позже чем через минуту, без перезапуска сервера.

Каждый параметр имеет допустимый диапазон; пределы железа из интерфейса не
превысить: время уровня — не больше AI_AGENT_MAX_SECONDS, строки — не больше
потолка исполнителя (executor.MAX_ROWS), одновременных «Высоких» — не больше
AI_HEAVY_SLOTS_MAX. Рядом с каждым лимитом — сколько раз за неделю в него
упирались: решение о правке опирается на данные, а не на ощущение.

Администратор работает без пределов времени и строк (решение от 23.09.2026,
limits.ADMIN) — столбец «Админ / Субадмин» для них действует на субадминистратора.
Сроки хранения, число диалогов и папок, память диалога (ИИ-02) действуют на всех.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import executor, journal, quotas


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


TIME_CEILING = _env_int("AI_AGENT_MAX_SECONDS", 240)
HEAVY_CEILING = _env_int("AI_HEAVY_SLOTS_MAX", 4)
GENERAL = "general"
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


@dataclass(frozen=True)
class Param:
    key: str
    title: str
    unit: str = ""
    kind: str = "int"            # int | choice | email
    min: int = 0
    max: int = 0
    nullable: bool = False       # пусто — «без лимита»
    choices: tuple[tuple[str, str], ...] = ()
    hint: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "unit": self.unit, "kind": self.kind,
                "min": self.min, "max": self.max, "nullable": self.nullable,
                "choices": [{"code": code, "title": title} for code, title in self.choices], "hint": self.hint}


def role_params() -> list[Param]:
    rows = executor.MAX_ROWS
    return [
        Param("deep_per_day", "«Высокий», запусков в день", "запусков", min=0, max=500, nullable=True,
              hint="Пусто — без лимита, 0 — уровень недоступен"),
        Param("concurrent", "Одновременных вопросов", "вопросов", min=1, max=5),
        Param("question_chars", "Длина вопроса", "знаков", min=200, max=quotas.MAX_QUESTION_CHARS),
        Param("seconds_fast", "Предел времени «Лёгкий»", "с", min=15, max=min(180, TIME_CEILING)),
        Param("seconds_analyze", "Предел времени «Средний»", "с", min=30, max=TIME_CEILING),
        Param("seconds_deep", "Предел времени «Высокий»", "с", min=60, max=TIME_CEILING),
        Param("rows_fast", "Строк в результате «Лёгкий»", "строк", min=50, max=rows),
        Param("rows_analyze", "Строк в результате «Средний»", "строк", min=50, max=rows),
        Param("rows_deep", "Строк в результате «Высокий»", "строк", min=50, max=rows),
        Param("priority", "Приоритет в очереди «Высокого»", kind="choice",
              choices=(("high", "высокий"), ("normal", "обычный"))),
        # Хранение (ИИ-02).
        Param("history_days", "История диалогов", "дней", min=30, max=1095,
              hint="Диалог без новых вопросов дольше срока удаляется ночью; закреплённые — нет"),
        Param("active_dialogs", "Активных диалогов", "диалогов", min=10, max=1000, nullable=True,
              hint="Пусто — без лимита; архивные не считаются"),
        Param("pinned_dialogs", "Закреплённых диалогов", "диалогов", min=1, max=50),
        Param("folders", "Папок", "папок", min=1, max=100),
        Param("dialog_memory", "Память диалога", "прошлых ходов", min=2, max=20,
              hint="Сколько прошлых вопросов и ответов ИИ учитывает в диалоге"),
    ]


def general_params() -> list[Param]:
    return [
        Param("heavy_slots", "Одновременных «Высоких» на сервере", "задач", min=1, max=HEAVY_CEILING,
              hint=f"Предел железа — {HEAVY_CEILING} (AI_HEAVY_SLOTS_MAX)"),
        Param("queue_wait_s", "Ожидание в очереди «Высокого»", "с", min=60, max=1800,
              hint="Дольше — ответ на «Среднем», запуск не засчитывается"),
        Param("daily_alert", "Порог алерта: вопросов в день от человека", "вопросов", min=10, max=1000,
              hint="Решение №12: больше порога — письмо и строка в «Качестве ИИ»"),
        Param("alert_recipient", "Получатель алерта", kind="email",
              hint="Почта; пусто — все администраторы (ADMIN_EMAILS)"),
        Param("audit_days", "Журнал аудита", "дней", min=365, max=1825,
              hint="Журнал вопросов и оценок хранится дольше истории — для разбора жалоб"),
    ]


def _params() -> dict[tuple[str, str], Param]:
    out = {(group, p.key): p for group in quotas.GROUP_TITLES for p in role_params()}
    out.update({(GENERAL, p.key): p for p in general_params()})
    return out


def default_of(group: str, key: str) -> Any:
    return quotas.GENERAL[key] if group == GENERAL else quotas.DEFAULTS[key][group]


def _title(group: str) -> str:
    return "общие" if group == GENERAL else quotas.GROUP_TITLES.get(group, group)


# --- хранение -------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS ai_limits (
    grp        TEXT NOT NULL,
    param      TEXT NOT NULL,
    value      TEXT,
    changed_by TEXT,
    changed_at INTEGER NOT NULL,
    PRIMARY KEY (grp, param)
);
CREATE TABLE IF NOT EXISTS ai_limits_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at INTEGER NOT NULL,
    changed_by TEXT,
    grp        TEXT NOT NULL,
    param      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    action     TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    path = journal.JOURNAL_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript(DDL)
    return conn


def load() -> dict[tuple[str, str], Any]:
    """Правки администратора: (группа, параметр) → значение. Незнакомые параметры пропускаются."""
    known = _params()
    conn = _connect()
    try:
        rows = conn.execute("SELECT grp, param, value FROM ai_limits").fetchall()
    finally:
        conn.close()
    out = {}
    for grp, param, raw in rows:
        if (grp, param) in known:
            try:
                out[(grp, param)] = json.loads(raw) if raw is not None else None
            except ValueError:
                continue
    return out


class Invalid(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _clean(param: Param, group: str, value: Any) -> tuple[Any, str | None]:
    where = f"«{param.title}» · {_title(group)}"
    if param.kind == "choice":
        codes = [code for code, _ in param.choices]
        if value not in codes:
            return None, f"{where}: выберите одно из значений — {', '.join(t for _, t in param.choices)}"
        return value, None
    if param.kind == "email":
        text = str(value or "").strip().lower()
        if text and not EMAIL_RE.match(text):
            return None, f"{where}: нужен адрес почты или пусто"
        return text, None
    if value is None or (isinstance(value, str) and not value.strip()):
        if param.nullable:
            return None, None
        return None, f"{where}: укажите число от {param.min} до {param.max}"
    if isinstance(value, bool):
        return None, f"{where}: нужно целое число"
    try:
        number = float(str(value).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None, f"{where}: нужно целое число"
    if number != int(number):
        return None, f"{where}: нужно целое число"
    number = int(number)
    if not param.min <= number <= param.max:
        return None, f"{where}: допустимо от {param.min} до {param.max}"
    return number, None


def validate(changes: list[dict]) -> list[tuple[str, str, Any]]:
    """Проверить все правки разом: ошибка любой — ничего не сохраняется."""
    known = _params()
    clean, errors = [], []
    for change in changes:
        group, key = str(change.get("group") or ""), str(change.get("param") or "")
        param = known.get((group, key))
        if param is None:
            errors.append(f"Неизвестный параметр: {group} / {key}")
            continue
        value, error = _clean(param, group, change.get("value"))
        if error:
            errors.append(error)
        else:
            clean.append((group, key, value))
    if errors:
        raise Invalid(errors)
    return clean


def save(changes: list[dict], actor: str | None) -> int:
    """Сохранить правки и записать журнал «было → стало». Значение по умолчанию — не правка, строка удаляется."""
    clean = validate(changes)
    stored = load()
    now = int(time.time())
    changed = 0
    conn = _connect()
    try:
        for group, key, value in clean:
            default = default_of(group, key)
            old = stored.get((group, key), default)
            if value == old:
                continue
            if value == default:
                conn.execute("DELETE FROM ai_limits WHERE grp = ? AND param = ?", (group, key))
            else:
                conn.execute(
                    "INSERT INTO ai_limits (grp, param, value, changed_by, changed_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(grp, param) DO UPDATE SET value = excluded.value, changed_by = excluded.changed_by, "
                    "changed_at = excluded.changed_at",
                    (group, key, json.dumps(value, ensure_ascii=False), actor, now))
            conn.execute(
                "INSERT INTO ai_limits_log (changed_at, changed_by, grp, param, old_value, new_value, action) "
                "VALUES (?, ?, ?, ?, ?, ?, 'change')",
                (now, actor, group, key, json.dumps(old, ensure_ascii=False), json.dumps(value, ensure_ascii=False)))
            changed += 1
        conn.commit()
    finally:
        conn.close()
    quotas.refresh()
    return changed


def reset(actor: str | None) -> int:
    """Вернуть все значения по умолчанию (таблица Р-2); каждое снятие — в журнал."""
    stored = load()
    now = int(time.time())
    conn = _connect()
    try:
        for (group, key), value in stored.items():
            conn.execute(
                "INSERT INTO ai_limits_log (changed_at, changed_by, grp, param, old_value, new_value, action) "
                "VALUES (?, ?, ?, ?, ?, ?, 'reset')",
                (now, actor, group, key, json.dumps(value, ensure_ascii=False),
                 json.dumps(default_of(group, key), ensure_ascii=False)))
        conn.execute("DELETE FROM ai_limits")
        conn.commit()
    finally:
        conn.close()
    quotas.refresh()
    return len(stored)


# --- что показывает вкладка -------------------------------------------------------

def fmt(param: Param, value: Any) -> str:
    if param.kind == "choice":
        return dict(param.choices).get(value, str(value))
    if param.kind == "email":
        return value or "администраторы"
    if value is None:
        return "без лимита"
    return f"{int(value):,}".replace(",", " ")


def history(limit: int = 30) -> list[dict]:
    known = _params()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT changed_at, changed_by, grp, param, old_value, new_value, action FROM ai_limits_log "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    out = []
    for at, by, grp, key, old, new, action in rows:
        param = known.get((grp, key))
        if param is None:
            continue
        out.append({
            "at": int(at), "by": by or "", "group": _title(grp), "param": param.title, "action": action,
            "old": fmt(param, json.loads(old) if old is not None else None),
            "new": fmt(param, json.loads(new) if new is not None else None),
        })
    return out


def hits(days: int = 7) -> dict[tuple[str, str], int]:
    """Сколько раз за последние дни упирались в каждый лимит — по журналу вопросов и отказов."""
    since = int(time.time()) - days * 86400
    since_day = datetime.fromtimestamp(since, quotas.MSK).date().isoformat()
    out: dict[tuple[str, str], int] = {}

    def add(key: tuple[str, str], n: int) -> None:
        if n:
            out[key] = out.get(key, 0) + int(n)

    conn = quotas._connect()
    try:
        by_reason = {"deep_quota": "deep_per_day", "downgraded": "deep_per_day",
                     "concurrent": "concurrent", "question_chars": "question_chars"}
        for role, reason, n in conn.execute(
                "SELECT role, reason, COUNT(*) FROM ai_runs WHERE day >= ? AND reason IS NOT NULL "
                "GROUP BY role, reason", (since_day,)):
            if reason in by_reason:
                add((quotas.group_of(role), by_reason[reason]), n)
            elif reason == "queue_timeout":
                add((GENERAL, "queue_wait_s"), n)
        waited = conn.execute("SELECT COUNT(*) FROM ai_runs WHERE day >= ? AND heavy = 1 AND wait_ms >= 1000",
                              (since_day,)).fetchone()[0]
        add((GENERAL, "heavy_slots"), waited)
        alerts = conn.execute("SELECT COUNT(*) FROM ai_alerts WHERE day >= ?", (since_day,)).fetchone()[0]
        add((GENERAL, "daily_alert"), alerts)
        for grp, param, n in conn.execute(
                "SELECT grp, param, SUM(n) FROM ai_limit_hits WHERE day >= ? GROUP BY grp, param", (since_day,)):
            add((grp, param), n)
    finally:
        conn.close()

    conn = journal._connect()
    try:
        for role, depth, timed, cut in conn.execute(
                "SELECT role, depth, SUM(CASE WHEN stop_reason = 'лимит времени' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN truncated = 1 THEN 1 ELSE 0 END) FROM ai_queries "
                "WHERE created_at >= ? AND depth IN ('fast', 'analyze', 'deep') GROUP BY role, depth", (since,)):
            group = quotas.group_of(role)
            add((group, f"seconds_{depth}"), timed or 0)
            add((group, f"rows_{depth}"), cut or 0)
    finally:
        conn.close()
    return out


def state(days: int = 7) -> dict:
    stored = load()
    counted = hits(days)
    groups = list(quotas.GROUP_TITLES)
    params = []
    for param in role_params():
        item = param.as_dict()
        item["defaults"] = {g: default_of(g, param.key) for g in groups}
        item["values"] = {g: stored.get((g, param.key), item["defaults"][g]) for g in groups}
        item["hits"] = {g: counted.get((g, param.key), 0) for g in groups}
        params.append(item)
    general = []
    for param in general_params():
        item = param.as_dict()
        item["default"] = default_of(GENERAL, param.key)
        item["value"] = stored.get((GENERAL, param.key), item["default"])
        item["hits"] = counted.get((GENERAL, param.key), 0)
        general.append(item)
    return {
        "groups": [{"code": g, "title": quotas.GROUP_TITLES[g]} for g in groups],
        "params": params,
        "general": general,
        "history": history(),
        "days": days,
        "cacheSeconds": quotas.CACHE_SECONDS,
        "ceilings": {"seconds": TIME_CEILING, "rows": executor.MAX_ROWS, "heavySlots": HEAVY_CEILING},
        "adminNote": ("Администратор работает без пределов времени и строк (решение от 23.09.2026) — "
                      "время и строки в столбце «Админ / Субадмин» действуют на субадминистратора; "
                      "сроки хранения, диалоги и папки — на обоих."),
        "planned": ("Память папки, файлы и их объём появятся здесь вместе с ИИ-07 и ИИ-11; "
                    "презентации, отложенные задачи и изображения — с ИИ-22, ИИ-24 и ИИ-19."),
    }
