"""Журнал обращений к ИИ.

Пишется всегда: и по успешным запросам, и по отклонённым. Повторяющиеся
отказы одного класса — материал для пересмотра матрицы доступа, а не
повод наказывать пользователя.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
JOURNAL_DB = Path(os.environ.get("AI_JOURNAL_DB") or DATA / "ai_journal.sqlite3")

DDL = """
CREATE TABLE IF NOT EXISTS ai_queries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  INTEGER NOT NULL,
    actor       TEXT,
    role        TEXT,
    binding     TEXT,
    scope_label TEXT,
    question    TEXT NOT NULL,
    model       TEXT,
    sql_raw     TEXT,
    sql_final   TEXT,
    verdict     TEXT NOT NULL,
    rule        TEXT,
    message     TEXT,
    row_count   INTEGER,
    model_ms    INTEGER,
    sql_ms      INTEGER,
    prompt_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_queries_created ON ai_queries(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_queries_verdict ON ai_queries(verdict);
"""


def _connect() -> sqlite3.Connection:
    JOURNAL_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(JOURNAL_DB)
    conn.executescript(DDL)
    _add_missing_columns(conn)
    return conn


# Колонки, появившиеся после первых запусков. У уже созданной базы их нет,
# а ронять журнал из-за этого нельзя: он пишется по каждому обращению.
LATE_COLUMNS = (("prompt_version", "TEXT"), ("depth", "TEXT"), ("task_type", "TEXT"), ("trace_json", "TEXT"),
                ("total_ms", "INTEGER"))


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    have = {row[1] for row in conn.execute("PRAGMA table_info(ai_queries)")}
    for name, kind in LATE_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE ai_queries ADD COLUMN {name} {kind}")
    conn.commit()


def write(entry: dict) -> int:
    fields = (
        "actor", "role", "binding", "scope_label", "question", "model",
        "sql_raw", "sql_final", "verdict", "rule", "message", "row_count",
        "model_ms", "sql_ms", "prompt_version", "depth", "task_type", "trace_json",
    )
    values = [int(time.time())] + [entry.get(f) for f in fields]
    conn = _connect()
    try:
        cursor = conn.execute(
            f"INSERT INTO ai_queries (created_at, {', '.join(fields)}) "
            f"VALUES ({', '.join('?' * (len(fields) + 1))})",
            values,
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def set_total_ms(entry_id: int | None, total_ms: int) -> None:
    """Полное время ответа — от вопроса до готового ответа, как его ждал человек.

    Пишется отдельно, после записи строки: сама запись делается изнутри
    конвейера, а общее время известно только снаружи.
    """
    if not entry_id:
        return
    conn = _connect()
    try:
        conn.execute("UPDATE ai_queries SET total_ms = ? WHERE id = ?", (int(total_ms), int(entry_id)))
        conn.commit()
    finally:
        conn.close()


def depth_timings(days: int = 7) -> dict[str, dict]:
    """Медиана полного времени ответа по уровням глубины за последние дни.

    Берутся только успешные ответы: отказ валидатора приходит за секунды и
    занизил бы ориентир. Уровень — фактический (после выбора «Авто»).
    """
    since = int(time.time()) - days * 86400
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT depth, total_ms FROM ai_queries "
            "WHERE created_at >= ? AND verdict = 'ok' AND total_ms IS NOT NULL AND depth IS NOT NULL",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    by_depth: dict[str, list[int]] = {}
    for depth, total in rows:
        by_depth.setdefault(str(depth), []).append(int(total))
    out = {}
    for depth, values in by_depth.items():
        values.sort()
        middle = len(values) // 2
        median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) // 2
        out[depth] = {"median_ms": median, "count": len(values)}
    return out


def recent(limit: int = 50) -> list[dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM ai_queries ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
