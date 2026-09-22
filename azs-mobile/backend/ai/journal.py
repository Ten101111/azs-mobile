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
LATE_COLUMNS = (("prompt_version", "TEXT"), ("depth", "TEXT"), ("task_type", "TEXT"), ("trace_json", "TEXT"))


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
