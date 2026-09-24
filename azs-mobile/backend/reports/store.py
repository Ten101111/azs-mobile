"""СП-01. Хранилище выпусков справок: версии, статусы, файлы.

Выпуск неизменяем: исправление — новая версия, прежняя получает статус
«заменён» и остаётся в архиве. «Не сформирован» — запись о попытке без
данных: экран честно показывает причину, а прошлая справка остаётся ниже.
Файлы лежат в data/reports/ рядом с базой выпусков и входят в резервную копию.

Выпуски собирает компьютер владельца по витрине ОХД и присылает готовыми
(решение владельца 24.09.2026); сервер их только хранит и показывает.
«Сформировать заново» на сервере — запрос на перевыпуск (report_requests):
его забирает следующий запуск сборщика на компьютере владельца.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
DB_PATH = Path(os.environ.get("REPORTS_DB") or DATA / "reports.sqlite3")
FILES_DIR = Path(os.environ.get("REPORTS_DIR") or DATA / "reports")

PUBLISHED, REPLACED, NOT_FORMED = "published", "replaced", "not_formed"
STATUS_TITLES = {PUBLISHED: "Опубликован", REPLACED: "Заменён новой версией", NOT_FORMED: "Не сформирован"}
# Колонки, добавленные после первого выпуска: старые базы дополняются при подключении.
EXTRA_COLUMNS = {"digest": "TEXT NOT NULL DEFAULT ''"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    level TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    week TEXT NOT NULL,
    week_from TEXT NOT NULL,
    week_to TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    data_latest TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    model_json TEXT NOT NULL DEFAULT '',
    pdf_path TEXT NOT NULL DEFAULT '',
    pdf_sha256 TEXT NOT NULL DEFAULT '',
    xlsx_path TEXT NOT NULL DEFAULT '',
    xlsx_sha256 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_report_issues_type_week ON report_issues(type, scope_key, week);
CREATE TABLE IF NOT EXISTS report_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    week TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    done_at INTEGER,
    issue_id INTEGER,
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS report_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    week TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    trigger TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    completeness REAL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


@contextmanager
def connect(path: Path | None = None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn) -> None:
    have = {row["name"] for row in conn.execute("PRAGMA table_info(report_issues)")}
    for name, ddl in EXTRA_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE report_issues ADD COLUMN {name} {ddl}")


def _stored(path: Path | None) -> str:
    """Путь файла для базы: относительно data/reports/, чтобы перенос каталога данных его не ломал."""
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(FILES_DIR.resolve()))
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def summary(row: sqlite3.Row) -> dict:
    """Карточка выпуска без модели — для списка и архива."""
    return {
        "id": row["id"], "type": row["type"], "level": row["level"], "scopeKey": row["scope_key"],
        "week": row["week"], "weekFrom": row["week_from"], "weekTo": row["week_to"],
        "version": row["version"], "status": row["status"], "statusTitle": STATUS_TITLES.get(row["status"], row["status"]),
        "createdAt": row["created_at"], "dataLatest": row["data_latest"], "reason": row["reason"],
        "hasPdf": bool(row["pdf_path"]), "hasXlsx": bool(row["xlsx_path"]),
    }


def full(row: sqlite3.Row) -> dict:
    out = summary(row)
    out["model"] = json.loads(row["model_json"]) if row["model_json"] else None
    return out


def published(conn, type_code: str, scope_key: str, week: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM report_issues WHERE type = ? AND scope_key = ? AND week = ? AND status = ? "
        "ORDER BY version DESC LIMIT 1", (type_code, scope_key, week, PUBLISHED)).fetchone()


def next_version(conn, type_code: str, scope_key: str, week: str) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM report_issues WHERE type = ? AND scope_key = ? AND week = ?",
                       (type_code, scope_key, week)).fetchone()
    return int(row["v"] or 0) + 1


def publish(conn, model: dict, pdf: Path | None, xlsx: Path | None, *, version: int,
            reason: str = "", created_by: str = "", digest: str = "") -> int:
    """Записать выпуск; прежний опубликованный той же недели — «заменён»."""
    week = model["week"]
    conn.execute("UPDATE report_issues SET status = ? WHERE type = ? AND scope_key = ? AND week = ? AND status = ?",
                 (REPLACED, model["type"], model["scopeKey"], week["iso"], PUBLISHED))
    cur = conn.execute(
        "INSERT INTO report_issues (type, level, scope_key, week, week_from, week_to, version, status, run_id, "
        "created_at, data_latest, reason, created_by, model_json, pdf_path, pdf_sha256, xlsx_path, xlsx_sha256, digest) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (model["type"], model["level"], model["scopeKey"], week["iso"], week["from"], week["to"], version,
         PUBLISHED, model["passport"]["runId"], int(time.time()), model["passport"]["latestDate"], reason,
         created_by, json.dumps(model, ensure_ascii=False),
         _stored(pdf), _sha256(pdf) if pdf else "", _stored(xlsx), _sha256(xlsx) if xlsx else "", digest))
    return int(cur.lastrowid)


def not_formed(conn, type_code: str, level: str, scope_key: str, week: dict, reason: str) -> bool:
    """Отметка «не сформирован»; повтор той же причины не плодит строк. True — отметка новая или причина сменилась."""
    row = conn.execute(
        "SELECT id, reason FROM report_issues WHERE type = ? AND scope_key = ? AND week = ? AND status = ? "
        "ORDER BY id DESC LIMIT 1", (type_code, scope_key, week["iso"], NOT_FORMED)).fetchone()
    if row and row["reason"] == reason:
        return False
    if row:
        conn.execute("UPDATE report_issues SET reason = ?, created_at = ? WHERE id = ?",
                     (reason, int(time.time()), row["id"]))
        return True
    conn.execute(
        "INSERT INTO report_issues (type, level, scope_key, week, week_from, week_to, version, status, created_at, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (type_code, level, scope_key, week["iso"], week["from"], week["to"], NOT_FORMED, int(time.time()), reason))
    return True


def add_request(conn, type_code: str, scope_key: str, week: str, reason: str, created_by: str = "") -> int:
    """Запрос на перевыпуск недели; открытый запрос той же недели заменяется новым."""
    conn.execute("UPDATE report_requests SET done_at = ?, outcome = 'superseded' WHERE type = ? AND scope_key = ? "
                 "AND week = ? AND done_at IS NULL", (int(time.time()), type_code, scope_key, week))
    cur = conn.execute("INSERT INTO report_requests (type, scope_key, week, reason, created_by, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (type_code, scope_key, week, reason, created_by, int(time.time())))
    return int(cur.lastrowid)


def open_request(conn, type_code: str, scope_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM report_requests WHERE type = ? AND scope_key = ? AND done_at IS NULL "
                        "ORDER BY id LIMIT 1", (type_code, scope_key)).fetchone()


def close_request(conn, request_id: int, outcome: str, issue_id: int | None = None) -> None:
    conn.execute("UPDATE report_requests SET done_at = ?, outcome = ?, issue_id = ? WHERE id = ? AND done_at IS NULL",
                 (int(time.time()), outcome, issue_id, request_id))


def request_summary(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    from .periods import parse_iso

    week = parse_iso(row["week"])
    return {"id": row["id"], "week": row["week"], "weekFrom": week.start.isoformat(), "weekTo": week.end.isoformat(),
            "reason": row["reason"], "createdBy": row["created_by"], "createdAt": row["created_at"]}


def clear_not_formed(conn, type_code: str, scope_key: str, week: str) -> None:
    conn.execute("DELETE FROM report_issues WHERE type = ? AND scope_key = ? AND week = ? AND status = ?",
                 (type_code, scope_key, week, NOT_FORMED))


def latest(conn, type_code: str, scope_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM report_issues WHERE type = ? AND scope_key = ? AND status = ? "
        "ORDER BY week DESC, version DESC LIMIT 1", (type_code, scope_key, PUBLISHED)).fetchone()


def pending(conn, type_code: str, scope_key: str) -> sqlite3.Row | None:
    """Попытка «не сформирован» новее последнего опубликованного выпуска."""
    last = latest(conn, type_code, scope_key)
    row = conn.execute(
        "SELECT * FROM report_issues WHERE type = ? AND scope_key = ? AND status = ? ORDER BY week DESC LIMIT 1",
        (type_code, scope_key, NOT_FORMED)).fetchone()
    if row and (last is None or row["week"] > last["week"]):
        return row
    return None


def archive(conn, type_code: str, scope_key: str, limit: int = 60) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM report_issues WHERE type = ? AND scope_key = ? AND status IN (?, ?) "
        "ORDER BY week DESC, version DESC LIMIT ?", (type_code, scope_key, PUBLISHED, REPLACED, limit)).fetchall()


def get(conn, issue_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM report_issues WHERE id = ?", (issue_id,)).fetchone()


def log_run(conn, type_code: str, week: str, started: float, trigger: str, outcome: str,
            completeness: float | None = None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO report_runs (type, week, started_at, duration_ms, trigger, outcome, completeness, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (type_code, week, int(started), int((time.time() - started) * 1000), trigger, outcome, completeness, detail[:500]))


def runs(conn, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM report_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
