"""Сроки хранения истории ИИ (ИИ-02, решение Р-2).

Ночная очистка:
  * диалоги без новых вопросов дольше срока роли (история: админ 365, АУП сети и
    общества 180, РУ 90 дней) удаляются вместе с сообщениями; закреплённые — нет;
  * журнал аудита (вопросы, SQL, оценки, выгрузки, запуски) хранится дольше истории —
    audit_days (365) — и чистится по своему сроку. Оценка остаётся в «Качестве ИИ»
    и после удаления диалога: вопрос берётся из журнала.

За WARN_DAYS (7) дней до удаления диалог помечен в списке (dialogs.list_dialogs).
Роль владельца запоминается у диалога при каждом открытии раздела (owner_role);
у диалога без известной роли берётся самый долгий срок — лучше хранить дольше,
чем удалить раньше обещанного.

Запуск — фоновый поток API (start_scheduler): раз в час проверяет, прошло ли
20 часов с прошлой очистки, так что пропущенная ночь (Mac был выключен)
догоняется при следующем запуске.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

from . import journal, quotas

DAY = 86400
CHECK_EVERY_S = 3600
MIN_GAP_S = 20 * 3600

DDL = """
CREATE TABLE IF NOT EXISTS ai_retention_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        INTEGER NOT NULL,
    dialogs_deleted   INTEGER NOT NULL,
    messages_deleted  INTEGER NOT NULL,
    audit_deleted     INTEGER NOT NULL
);
"""

_started = False
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    from . import dialogs

    conn = dialogs._connect()          # схема диалогов с поздними колонками
    conn.executescript(DDL)
    return conn


def _longest_history() -> int:
    return max(int(quotas.value(role, "history_days")) for role in ("admin", "aup_network", "aup_npo", "regional_manager"))


def cleanup(now: int | None = None) -> dict:
    """Удалить истёкшую историю и журнал аудита; вернуть, сколько удалено."""
    now = int(now if now is not None else time.time())
    conn = _connect()
    deleted_dialogs = deleted_messages = audit = 0
    try:
        longest = _longest_history()
        rows = conn.execute("SELECT id, updated_at, owner_role FROM ai_dialogs WHERE pinned = 0").fetchall()
        expired = []
        for row in rows:
            days = int(quotas.value(row["owner_role"], "history_days")) if row["owner_role"] else longest
            if int(row["updated_at"]) + days * DAY < now:
                expired.append(int(row["id"]))
        for dialog_id in expired:
            deleted_messages += conn.execute("DELETE FROM ai_messages WHERE dialog_id = ?", (dialog_id,)).rowcount
            conn.execute("DELETE FROM ai_dialogs WHERE id = ?", (dialog_id,))
        deleted_dialogs = len(expired)

        audit_since = now - int(quotas.general("audit_days")) * DAY
        for table, column in (("ai_queries", "created_at"), ("ai_feedback", "created_at"),
                              ("ai_exports", "created_at"), ("ai_runs", "created_at")):
            try:
                audit += conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (audit_since,)).rowcount
            except sqlite3.OperationalError:
                continue                    # таблицы ещё нет — чистить нечего
        conn.execute("INSERT INTO ai_retention_runs (started_at, dialogs_deleted, messages_deleted, audit_deleted) "
                     "VALUES (?, ?, ?, ?)", (now, deleted_dialogs, deleted_messages, audit))
        conn.commit()
    finally:
        conn.close()
    return {"dialogs": deleted_dialogs, "messages": deleted_messages, "audit": audit, "at": now}


def last_run() -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM ai_retention_runs ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def due(now: int | None = None) -> bool:
    now = int(now if now is not None else time.time())
    run = last_run()
    return run is None or now - int(run["started_at"]) >= MIN_GAP_S


def start_scheduler() -> bool:
    """Фоновая очистка в процессе API. AI_RETENTION=0 — выключить (например, для отладки)."""
    global _started
    if (os.environ.get("AI_RETENTION") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    with _lock:
        if _started:
            return False
        _started = True

    def loop() -> None:
        time.sleep(60)
        while True:
            try:
                if due():
                    cleanup()
            except Exception:  # noqa: BLE001 - очистка не должна ронять API
                pass
            time.sleep(CHECK_EVERY_S)

    threading.Thread(target=loop, name="ai-retention", daemon=True).start()
    return True


def storage_report(now: int | None = None) -> dict:
    """Объём хранения по группам ролей — для администратора («Лимиты ИИ»)."""
    now = int(now if now is not None else time.time())
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT d.owner_role AS role, d.user_id, d.archived_at, d.pinned, d.updated_at,"
            "       (SELECT COUNT(*) FROM ai_messages m WHERE m.dialog_id = d.id) AS messages "
            "FROM ai_dialogs d").fetchall()
        folders = conn.execute("SELECT COUNT(*) FROM ai_folders").fetchone()[0]
    finally:
        conn.close()
    groups: dict[str, dict[str, Any]] = {
        code: {"code": code, "title": title, "users": set(), "dialogs": 0, "archived": 0, "messages": 0,
               "expiringSoon": 0, "oldest": None}
        for code, title in quotas.GROUP_TITLES.items()}
    unknown = {"code": "unknown", "title": "Роль не известна", "users": set(), "dialogs": 0, "archived": 0,
               "messages": 0, "expiringSoon": 0, "oldest": None}
    for row in rows:
        bucket = groups[quotas.group_of(row["role"])] if row["role"] else unknown
        bucket["users"].add(row["user_id"])
        bucket["dialogs"] += 1
        bucket["archived"] += 1 if row["archived_at"] else 0
        bucket["messages"] += int(row["messages"] or 0)
        if row["role"] and not row["pinned"]:
            ends = int(row["updated_at"]) + int(quotas.value(row["role"], "history_days")) * DAY
            if 0 <= ends - now <= 7 * DAY:
                bucket["expiringSoon"] += 1
        oldest = bucket["oldest"]
        bucket["oldest"] = int(row["updated_at"]) if oldest is None else min(oldest, int(row["updated_at"]))
    items = [*groups.values()] + ([unknown] if unknown["dialogs"] else [])
    for item in items:
        item["users"] = len(item["users"])
    size = journal.JOURNAL_DB.stat().st_size if journal.JOURNAL_DB.exists() else 0
    return {"groups": items, "folders": int(folders), "dbBytes": size, "lastRun": last_run()}
