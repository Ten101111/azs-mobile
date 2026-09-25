"""Тематики вопросов к ИИ — фильтры «Журнала ИИ» (25.09.2026).

Пять фильтров: тематика, показатель, разрез, тип задачи, период. У вопроса в
каждом фильтре может быть несколько значений («выручка и конверсия»).

Правила — код, мгновенно и одинаково для одинаковых вопросов:
  * показатель — фраза из названия или синонима показателя семантического слоя,
    найденная в вопросе целиком с учётом окончаний; длинная фраза важнее
    короткой («средний чек НТУ», а не «средний чек»). Нет в тексте — первые
    показатели плана агента;
  * тематика — семейство показателя (выручка и продажи НТУ, топливо, план…).
    Показатель вне известных семейств становится своей тематикой — тематики
    пополняются вместе с семантическим слоем. Без показателей — ключевые слова
    (сервис, объекты сети, руководители, простои);
  * разрез, тип задачи, период — словари фраз; тип задачи — из разбора агента.

Модель — фоном, после ответа: вопрос, который правила не отнесли ни к одной
тематике, локальная модель относит к существующей или заводит новую (1–4
слова, без чисел, имён и названий обществ). Так тематики пополняются новыми
вопросами. Модель работает, только когда никто не ждёт ответа ИИ, и только
раскладывает вопросы по темам — к данным она здесь доступа не имеет.

Реестр ai_topics помнит, когда значение появилось впервые: в интерфейсе новые
за NEW_DAYS дней помечены «новая». RULES_REV — версия правил: при её смене
вопросы раскладываются заново (темы от модели сохраняются).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from functools import lru_cache
from typing import Any, Iterable

from . import journal
from . import semantic as semantic_mod

RULES_REV = 1
FACETS = (
    ("theme", "Тематика"),
    ("metric", "Показатель"),
    ("slice", "Разрез"),
    ("task", "Тип задачи"),
    ("period", "Период"),
)
FACET_TITLES = dict(FACETS)
UNKNOWN_THEME = "Не определена"     # ждёт модель или модель выключена
OTHER_THEME = "Прочее"              # модель не смогла или тем уже слишком много
NEW_DAYS = 7


def _flag(name: str, default: str = "1") -> bool:
    return (os.environ.get(name) or default).strip().lower() not in {"0", "false", "no", "off"}


MODEL_ENABLED = _flag("AI_TOPICS_MODEL")
MODEL_TRIES = 3
MAX_MODEL_THEMES = int(os.environ.get("AI_TOPICS_MAX") or 40)
INTERVAL_S = float(os.environ.get("AI_TOPICS_INTERVAL") or 120)

DDL = """
CREATE TABLE IF NOT EXISTS ai_topics (
    facet       TEXT NOT NULL,
    value       TEXT NOT NULL,
    origin      TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,
    PRIMARY KEY (facet, value)
);
CREATE TABLE IF NOT EXISTS ai_query_topics (
    query_id  INTEGER NOT NULL,
    facet     TEXT NOT NULL,
    value     TEXT NOT NULL,
    origin    TEXT NOT NULL,
    PRIMARY KEY (query_id, facet, value)
);
CREATE INDEX IF NOT EXISTS ai_query_topics_value ON ai_query_topics (facet, value);
CREATE TABLE IF NOT EXISTS ai_topic_state (
    query_id     INTEGER PRIMARY KEY,
    rules_rev    INTEGER NOT NULL,
    model_state  TEXT,
    model_tries  INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = journal._connect()          # схема журнала с поздними колонками
    conn.executescript(DDL)
    conn.row_factory = sqlite3.Row
    return conn


# --- тематики по семействам показателей --------------------------------------

# (тематика, ключи показателей, начала ключей). Порядок важен: сначала точные ключи.
THEMES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("План и выполнение", (), ("plan_",)),
    ("Сервис и отзывы", ("ratings", "avg_rating", "negative_ratings", "complaints", "service_quality"), ("neg_",)),
    ("Лояльность", ("loyalty_checks", "loyalty_share", "points_out"), ("loyalty",)),
    ("Топливо", ("revenue_fuel", "avg_fill"), ("fuel",)),
    ("Доходность НТУ", ("margin_ntu", "vd_per_client"), ("vd_", "margin")),
    ("Конверсия и средний чек", ("conversion_ntu", "avg_check_ntu", "avg_check", "complexity_ntu", "avg_price_ntu",
                                 "items_per_100", "ntu_per_100"), ()),
    ("Трафик и чеки", ("traffic", "traffic_b2c", "traffic_b2b", "checks_fuel", "checks_ntu", "complex_checks",
                       "checks_per_station", "checks_per_day"), ("traffic",)),
    ("Выручка и продажи НТУ", ("revenue_ntu", "revenue", "ntu_share"), ("items_",)),
    ("Расходы и покрытие", ("opex", "coverage"), ()),
    ("Сеть и объекты", ("active_stations",), ()),
)

# Тематика без показателей — по словам вопроса (текст в нижнем регистре, «ё» → «е»).
KEYWORD_THEMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Сервис и отзывы", ("отзыв", "оценк", "жалоб", "сервис", "негатив")),
    ("Простои и остатки топлива", ("простой", "простои", "простоя", "остатк", "нет топлива", "отсутстви")),
    ("Руководители и персонал", ("руководител", "управляющ", "территориал", "менеджер", "сотрудник", "персонал")),
    ("Сеть и объекты", ("адрес", "где наход", "сколько азс", "сколько объект", "франчайз", "координат",
                        "тип азс", "формат азс", "режим работ", "в каких регион")),
    ("Методика и определения", ("как считает", "как рассчит", "формул", "что такое", "методик", "определени")),
)

# Однословные синонимы, которые в вопросе чаще значат другое («кафе» — тип АЗС, а не ВД кафе).
AMBIGUOUS = {"день", "дата", "кафе", "приложение", "мп", "продукты", "касса", "статус", "формат",
             "затраты", "расходы", "оборот"}
# Синоним только из общих слов («на АЗС», «на объект») показатель не называет.
GENERIC_STEMS = {"азс", "объект", "станци", "одн", "один", "одну", "день", "дат"}

SLICES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Общества (ОНПО)", (r"обществ", r"\bонпо\b", r"\bнпо\b", r"\b[а-я]{1,3}нп\b")),
    ("Руководители (РУ, ТМ)", (r"\bру\b", r"\bтм\b", r"руководител", r"территориал", r"менеджер")),
    ("АЗС", (r"\bазс\b", r"объект", r"\bтоп[- ]?\d", r"отстающ", r"лидер", r"\b\d{2}-\d{3}\b",
             r"\b(?!(?:19|20)\d\d\b)\d{4,5}\b")),
    ("Регионы и города", (r"регион", r"област", r"\bкра[йеюя]\b", r"город", r"республик")),
    ("Время: дни и месяцы", (r"по дням", r"по недел", r"по месяц", r"помесячн", r"ежеднев", r"динамик")),
)
NO_SLICE = "Без разреза"

TASK_TITLES = {
    "lookup": "Факт или справка", "compare": "Сравнение", "trend": "Динамика", "diagnose": "Причины",
    "anomaly": "Отклонения", "opportunity": "Как улучшить", "whatif": "Сценарии «что если»", "other": "Другое",
}
TASK_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("diagnose", ("почему", "причин", "из-за чего", "за счет чего")),
    ("opportunity", ("как повысить", "как поднять", "как увеличить", "как улучшить", "что сделать", "рекоменд", "помоги")),
    ("whatif", ("что если", "что будет, если", "что будет если")),
    ("compare", ("сравн", " против ", " vs ", "лучше чем", "хуже чем", "лучше, чем", "хуже, чем")),
    ("anomaly", ("аномал", "отклонени", "выброс", "резк")),
    ("trend", ("динамик", "тренд", "год к году", "по месяцам", "изменил", "рост", "падени", "к прошлому")),
)

MONTHS = r"(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)"
PERIODS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Год к году", (r"год к году", r"г/г", r"прошл\w* год", r"к прошлому году", r"аналогичн")),
    ("Текущий месяц", (r"текущ\w* месяц", r"(?:этом|этот|сей) месяц", r"с начала месяца")),
    ("Прошлый месяц", (r"(?:прошл|предыдущ)\w* месяц",)),
    ("Неделя", (r"недел",)),
    ("День", (r"вчера", r"сегодня", r"позавчера", r"за день", r"на дату")),
    ("Квартал", (r"квартал",)),
    ("Год", (r"с начала года", r"за год", r"за весь год")),
    ("Конкретный месяц", (MONTHS,)),
)
NO_PERIOD = "Не указан"


def _text(value: str | None) -> str:
    return " ".join((value or "").lower().replace("ё", "е").split())


def theme_of_metric(key: str, title: str) -> str:
    for theme, keys, _prefixes in THEMES:
        if key in keys:
            return theme
    for theme, _keys, prefixes in THEMES:
        if any(key.startswith(prefix) or prefix in key for prefix in prefixes):
            return theme
    return title          # новый показатель вне семейств — своя тематика


# --- показатели в тексте -------------------------------------------------------

def _stems(text: str) -> list[str]:
    return [semantic_mod._stem(t) for t in semantic_mod._tokens(semantic_mod._norm(text))]


@lru_cache(maxsize=1)
def _phrases() -> tuple[tuple[tuple[str, ...], str, str], ...]:
    """Фразы показателей всех слоёв: рабочего, витрины ОХД и стенда (название — из первого)."""
    out, titles = [], {}
    for sem in _semantics():
        for key, metric in sem.metrics.items():
            title = titles.setdefault(key, metric.title)
            for name in (metric.title, *metric.aliases):
                stems = tuple(_stems(name))
                if not stems or set(stems) <= GENERIC_STEMS:
                    continue
                if len(stems) == 1 and semantic_mod._norm(name) in AMBIGUOUS:
                    continue
                out.append((stems, key, title))
    return tuple(out)


def _semantics() -> list:
    # Рабочий слой (витрина или стенд), затем витрина ОХД — у плана агента ключи оттуда.
    out = [semantic_mod.SEMANTIC]
    for extra in (getattr(semantic_mod, "DWH", None), getattr(semantic_mod, "STAND", None)):
        if extra is not None and all(extra is not s for s in out):
            out.append(extra)
    return out


def metric_title(key: str) -> tuple[str, str] | None:
    for sem in _semantics():
        metric = sem.metrics.get(key)
        if metric is not None:
            return key, metric.title
    return None


def metrics_in_text(text: str) -> list[tuple[str, str]]:
    """Показатели, названные в тексте: [(ключ, название)] в порядке упоминания."""
    words = _stems(text)
    matches = []
    for stems, key, title in _phrases():
        size = len(stems)
        for start in range(0, len(words) - size + 1):
            if tuple(words[start:start + size]) == stems:
                matches.append((start, start + size, key, title))
    matches.sort(key=lambda m: (-(m[1] - m[0]), m[0]))
    taken: list[tuple[int, int]] = []
    chosen = []
    for start, end, key, title in matches:
        # Фраза внутри уже найденной длинной («средний чек» в «средний чек НТУ») — не отдельный показатель.
        if any(s <= start and end <= e for s, e in taken):
            continue
        taken.append((start, end))
        chosen.append((start, key, title))
    out, titles = [], set()
    for _start, key, title in sorted(chosen):
        if title not in titles:
            titles.add(title)
            out.append((key, title))
    return out


# --- разбор одного вопроса -----------------------------------------------------

def _plan(trace_json: str | None) -> dict:
    try:
        trace = json.loads(trace_json) if trace_json else None
    except (TypeError, ValueError):
        return {}
    plan = trace.get("plan") if isinstance(trace, dict) else None
    return plan if isinstance(plan, dict) else {}


def classify(question: str, task_type: str | None = None, trace_json: str | None = None) -> dict[str, list[str]]:
    """Значения фильтров по правилам. Тематики нет — список пуст (решит модель)."""
    plan = _plan(trace_json)
    main = plan.get("standaloneQuestion") or question or ""
    text = _text(main if main == question else f"{question} {main}")

    found = metrics_in_text(main) or metrics_in_text(question or "")
    if not found:
        found = [m for m in (metric_title(k) for k in (plan.get("metrics") or [])[:2] if isinstance(k, str)) if m]
    metrics = [title for _key, title in found][:4]
    themes = []
    for key, title in found:
        theme = theme_of_metric(key, title)
        if theme not in themes:
            themes.append(theme)
    if not themes:
        themes = [theme for theme, words in KEYWORD_THEMES if any(w in text for w in words)][:2]

    slices = [name for name, patterns in SLICES if any(re.search(p, text) for p in patterns)] or [NO_SLICE]

    task = (task_type or plan.get("taskType") or "").strip()
    if task not in TASK_TITLES or task == "lookup":
        by_words = next((code for code, words in TASK_RULES if any(w in f" {text} " for w in words)), None)
        task = by_words or task or "lookup"
    tasks = [TASK_TITLES.get(task, TASK_TITLES["other"])]

    period_text = f"{text} {_text(plan.get('period'))}"
    periods = [name for name, patterns in PERIODS if any(re.search(p, period_text) for p in patterns)]
    if len(set(re.findall(r"\b20\d\d\b", text))) >= 2 and "Год к году" not in periods:
        periods.insert(0, "Год к году")
    if "Конкретный месяц" in periods and ("Текущий месяц" in periods or "Прошлый месяц" in periods):
        periods.remove("Конкретный месяц")
    return {"theme": themes, "metric": metrics, "slice": slices, "task": tasks, "period": periods or [NO_PERIOD]}


# --- синхронизация с журналом ---------------------------------------------------

def _register(conn: sqlite3.Connection, facet: str, value: str, origin: str, seen: int) -> None:
    conn.execute("INSERT INTO ai_topics (facet, value, origin, first_seen) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(facet, value) DO UPDATE SET first_seen = MIN(first_seen, excluded.first_seen)",
                 (facet, value, origin, seen))


def _put(conn: sqlite3.Connection, query_id: int, facet: str, value: str, origin: str, seen: int) -> None:
    conn.execute("INSERT OR IGNORE INTO ai_query_topics (query_id, facet, value, origin) VALUES (?, ?, ?, ?)",
                 (query_id, facet, value, origin))
    _register(conn, facet, value, origin, seen)


def sync(limit: int = 2000, now: int | None = None) -> int:
    """Разложить по правилам вопросы, которых ещё нет в ai_topic_state (или правила новее)."""
    now = int(now if now is not None else time.time())
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT q.id, q.created_at, q.question, q.task_type, q.trace_json, s.model_state "
            "FROM ai_queries q LEFT JOIN ai_topic_state s ON s.query_id = q.id "
            "WHERE s.query_id IS NULL OR s.rules_rev < ? ORDER BY q.id LIMIT ?", (RULES_REV, limit)).fetchall()
        for row in rows:
            facets = classify(row["question"] or "", row["task_type"], row["trace_json"])
            conn.execute("DELETE FROM ai_query_topics WHERE query_id = ? AND origin = 'rules'", (row["id"],))
            seen = int(row["created_at"] or now)
            for facet, values in facets.items():
                for value in values:
                    _put(conn, row["id"], facet, value, "rules", seen)
            has_model_theme = conn.execute(
                "SELECT 1 FROM ai_query_topics WHERE query_id = ? AND facet = 'theme' AND origin = 'model'",
                (row["id"],)).fetchone()
            if facets["theme"]:
                # Правила узнали тематику — тема от модели больше не нужна.
                conn.execute("DELETE FROM ai_query_topics WHERE query_id = ? AND facet = 'theme' AND origin = 'model'",
                             (row["id"],))
                state = "rules"
            elif has_model_theme:
                state = "done"
            else:
                _put(conn, row["id"], "theme", UNKNOWN_THEME, "rules", seen)
                state = "pending" if (row["question"] or "").strip() else "skipped"
            conn.execute(
                "INSERT INTO ai_topic_state (query_id, rules_rev, model_state, model_tries, updated_at) "
                "VALUES (?, ?, ?, 0, ?) ON CONFLICT(query_id) DO UPDATE SET rules_rev = excluded.rules_rev, "
                "model_state = excluded.model_state, updated_at = excluded.updated_at",
                (row["id"], RULES_REV, state, now))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def pending_count() -> int:
    conn = connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM ai_topic_state WHERE model_state = 'pending'").fetchone()[0])
    finally:
        conn.close()


# --- тематика от модели ---------------------------------------------------------

MODEL_SYSTEM = (
    "Ты раскладываешь вопросы к аналитическому ИИ сети АЗС по обобщённым темам для журнала обращений. "
    "Выбери одну тему из списка, если она подходит по смыслу. Если ни одна не подходит — придумай новую: "
    "1–4 слова по-русски, обобщённо, без чисел, дат, номеров АЗС, имён людей и названий обществ. "
    'Ответь только JSON: {"topic": "тема"}'
)


def clean_topic(raw: Any) -> str:
    text = re.sub(r"[\d«»\"'`#№]+", " ", str(raw or ""))
    text = re.sub(r"\s+", " ", text).strip(" .,:;—–-")
    if not text or len(text) > 40 or len(text.split()) > 5:
        return ""
    return text[0].upper() + text[1:]


def themes(conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute("SELECT value, origin, first_seen FROM ai_topics WHERE facet = 'theme' "
                            "AND value NOT IN (?, ?) ORDER BY value", (UNKNOWN_THEME, OTHER_THEME)).fetchall()
    finally:
        if own:
            conn.close()
    return [dict(row) for row in rows]


def _model_theme(model, question: str, known: list[str]) -> str:
    from .agent.llm import extract_json

    catalogue = sorted(set(known) | {t for t, _k, _p in THEMES} | {t for t, _w in KEYWORD_THEMES})
    messages = [
        {"role": "system", "content": MODEL_SYSTEM},
        {"role": "user", "content": "Темы: " + "; ".join(catalogue) + f"\nВопрос: {question.strip()[:600]}"},
    ]
    reply = model.chat(messages, json_mode=True, max_tokens=60)
    parsed = extract_json(reply.content or "")
    return clean_topic(parsed.get("topic") if isinstance(parsed, dict) else "")


def label_pending(model=None, limit: int = 2, now: int | None = None) -> int:
    """Модель даёт тематику вопросам, которые не узнали правила. Возвращает, сколько разобрано."""
    now = int(now if now is not None else time.time())
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT q.id, q.question, q.trace_json, s.model_tries FROM ai_topic_state s "
            "JOIN ai_queries q ON q.id = s.query_id WHERE s.model_state = 'pending' "
            "ORDER BY q.id DESC LIMIT ?", (limit,)).fetchall()
        if not rows:
            return 0
        if model is None:
            from .agent.llm import OllamaChat
            model = OllamaChat(max_tokens=60, timeout_s=90)
        done = 0
        for row in rows:
            plan = _plan(row["trace_json"])
            question = plan.get("standaloneQuestion") or row["question"] or ""
            existing = themes(conn)
            known = {item["value"].casefold(): item["value"] for item in existing}
            try:
                topic = _model_theme(model, question, [item["value"] for item in existing])
            except Exception:  # noqa: BLE001 - модель недоступна: попробуем позже
                tries = int(row["model_tries"] or 0) + 1
                conn.execute("UPDATE ai_topic_state SET model_tries = ?, model_state = ?, updated_at = ? "
                             "WHERE query_id = ?", (tries, "failed" if tries >= MODEL_TRIES else "pending", now, row["id"]))
                conn.commit()
                continue
            base = {t.casefold(): t for t, _k, _p in THEMES} | {t.casefold(): t for t, _w in KEYWORD_THEMES}
            value = known.get(topic.casefold()) or base.get(topic.casefold())
            model_made = sum(1 for item in existing if item["origin"] == "model")
            if not value:
                value = topic if topic and topic != UNKNOWN_THEME and model_made < MAX_MODEL_THEMES else OTHER_THEME
            conn.execute("DELETE FROM ai_query_topics WHERE query_id = ? AND facet = 'theme'", (row["id"],))
            _put(conn, row["id"], "theme", value, "model", now)
            conn.execute("UPDATE ai_topic_state SET model_state = 'done', updated_at = ? WHERE query_id = ?",
                         (now, row["id"]))
            conn.commit()
            done += 1
        return done
    finally:
        conn.close()


# --- фоновый разбор -------------------------------------------------------------

_started = False
_lock = threading.Lock()


def start_worker() -> bool:
    """Поток в процессе API: правила — сразу, модель — когда никто не ждёт ответа ИИ.

    AI_TOPICS=0 — выключить совсем; AI_TOPICS_MODEL=0 — только правила.
    """
    global _started
    if not _flag("AI_TOPICS"):
        return False
    with _lock:
        if _started:
            return False
        _started = True

    def loop() -> None:
        from . import quotas

        time.sleep(30)
        while True:
            try:
                sync()
                if MODEL_ENABLED and not quotas.RUNS.busy():
                    label_pending(limit=2)
            except Exception:  # noqa: BLE001 - разбор тем не должен ронять API
                pass
            time.sleep(INTERVAL_S)

    threading.Thread(target=loop, name="ai-topics", daemon=True).start()
    return True


def query_topics(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, dict[str, list[str]]]:
    ids = [int(i) for i in ids]
    out: dict[int, dict[str, list[str]]] = {i: {} for i in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for row in conn.execute(f"SELECT query_id, facet, value FROM ai_query_topics WHERE query_id IN ({marks}) "
                            "ORDER BY facet, value", ids):
        out[int(row["query_id"])].setdefault(row["facet"], []).append(row["value"])
    return out
