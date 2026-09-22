"""Диалоги, сообщения и оценки ответов ИИ.

Лежит в том же файле, что и журнал обращений: журнал отвечает на вопрос
«что система сделала», а эти таблицы — на вопрос «что видел пользователь».
Связаны они journal_id, и разделять их по разным базам было бы неудобно
ровно там, где чаще всего и нужно: в разборе конкретной низкой оценки.

Владение проверяется в каждой операции: диалог виден только своему автору,
включая администратора. Это модель приватности ИБ-4 — администратор работает
с метаданными и оценками, а не с чужой перепиской.
"""
from __future__ import annotations

import json
import sqlite3
import time

from .journal import DDL as JOURNAL_DDL, JOURNAL_DB

MIN_RATING = 1
MAX_RATING = 5
# Оценка до этого значения включительно требует объяснения: низкая оценка
# без причины ничего не даёт разбору, а разбирать нужно именно такие ответы.
COMMENT_REQUIRED_UPTO = 3
TITLE_LIMIT = 120
COMMENT_LIMIT = 2000

DDL = """
CREATE TABLE IF NOT EXISTS ai_dialogs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    title      TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    pinned     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_dialogs_user ON ai_dialogs(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS ai_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_id   INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    question    TEXT    NOT NULL,
    answer_json TEXT    NOT NULL,
    journal_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ai_messages_dialog ON ai_messages(dialog_id, id);

CREATE TABLE IF NOT EXISTS ai_feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE,
    journal_id INTEGER,
    user_id    INTEGER NOT NULL,
    rating     INTEGER NOT NULL,
    comment    TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'new',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_feedback_rating ON ai_feedback(rating, created_at);
"""


class NotFound(Exception):
    """Диалог или сообщение не найдены либо принадлежат другому человеку."""


class Invalid(Exception):
    """Данные не проходят проверку — сообщение предназначено пользователю."""


def _connect() -> sqlite3.Connection:
    JOURNAL_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(JOURNAL_DB)
    conn.row_factory = sqlite3.Row
    # Сводка соединяет оценки с журналом обращений, поэтому обе схемы должны
    # существовать независимо от того, кто первым открыл файл.
    conn.executescript(JOURNAL_DDL)
    conn.executescript(DDL)
    return conn


def _now() -> int:
    return int(time.time())


def _title_from_question(question: str) -> str:
    text = " ".join(question.split())
    if len(text) <= 48:
        return text or "Новый диалог"
    cut = text[:48].rsplit(" ", 1)[0]
    return (cut or text[:48]) + "…"


# --- диалоги ---------------------------------------------------------------

def list_dialogs(user_id: int, limit: int = 200) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT d.id, d.title, d.created_at, d.updated_at, d.pinned,"
            "       (SELECT question FROM ai_messages m WHERE m.dialog_id = d.id"
            "         ORDER BY m.id DESC LIMIT 1) AS last_question,"
            "       (SELECT COUNT(*) FROM ai_messages m WHERE m.dialog_id = d.id) AS messages "
            "FROM ai_dialogs d WHERE d.user_id = ? "
            "ORDER BY d.pinned DESC, d.updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def create_dialog(user_id: int, title: str = "") -> dict:
    now = _now()
    clean = " ".join((title or "").split())[:TITLE_LIMIT] or "Новый диалог"
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO ai_dialogs (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, clean, now, now),
        )
        conn.commit()
        dialog_id = int(cursor.lastrowid)
    finally:
        conn.close()
    return {"id": dialog_id, "title": clean, "created_at": now, "updated_at": now,
            "pinned": 0, "last_question": None, "messages": 0}


def _owned(conn: sqlite3.Connection, dialog_id: int, user_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM ai_dialogs WHERE id = ? AND user_id = ?", (dialog_id, user_id)
    ).fetchone()
    if row is None:
        raise NotFound("Диалог не найден")
    return row


def rename_dialog(dialog_id: int, user_id: int, title: str) -> dict:
    clean = " ".join((title or "").split())[:TITLE_LIMIT]
    if not clean:
        raise Invalid("Название не может быть пустым")
    conn = _connect()
    try:
        _owned(conn, dialog_id, user_id)
        conn.execute("UPDATE ai_dialogs SET title = ?, updated_at = ? WHERE id = ?",
                     (clean, _now(), dialog_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": dialog_id, "title": clean}


def set_pinned(dialog_id: int, user_id: int, pinned: bool) -> dict:
    conn = _connect()
    try:
        _owned(conn, dialog_id, user_id)
        conn.execute("UPDATE ai_dialogs SET pinned = ? WHERE id = ?",
                     (1 if pinned else 0, dialog_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": dialog_id, "pinned": bool(pinned)}


def delete_dialog(dialog_id: int, user_id: int) -> None:
    conn = _connect()
    try:
        _owned(conn, dialog_id, user_id)
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM ai_messages WHERE dialog_id = ?", (dialog_id,))]
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM ai_feedback WHERE message_id IN ({marks})", ids)
        conn.execute("DELETE FROM ai_messages WHERE dialog_id = ?", (dialog_id,))
        conn.execute("DELETE FROM ai_dialogs WHERE id = ?", (dialog_id,))
        conn.commit()
    finally:
        conn.close()


# --- сообщения -------------------------------------------------------------

def append_message(dialog_id: int, user_id: int, question: str, answer: dict,
                   journal_id: int | None) -> dict:
    now = _now()
    conn = _connect()
    try:
        dialog = _owned(conn, dialog_id, user_id)
        cursor = conn.execute(
            "INSERT INTO ai_messages (dialog_id, created_at, question, answer_json, journal_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (dialog_id, now, question, json.dumps(answer, ensure_ascii=False), journal_id),
        )
        message_id = int(cursor.lastrowid)
        # Диалог, названный по первому вопросу, читается в списке лучше,
        # чем «Новый диалог» — переименовываем только автоматическое имя.
        title = dialog["title"]
        if title == "Новый диалог":
            title = _title_from_question(question)
        conn.execute("UPDATE ai_dialogs SET updated_at = ?, title = ? WHERE id = ?",
                     (now, title, dialog_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": message_id, "dialogId": dialog_id, "title": title, "createdAt": now}


def messages(dialog_id: int, user_id: int) -> list[dict]:
    conn = _connect()
    try:
        _owned(conn, dialog_id, user_id)
        rows = conn.execute(
            "SELECT m.id, m.created_at, m.question, m.answer_json, m.journal_id,"
            "       f.rating, f.comment "
            "FROM ai_messages m LEFT JOIN ai_feedback f ON f.message_id = m.id "
            "WHERE m.dialog_id = ? ORDER BY m.id",
            (dialog_id,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            answer = json.loads(row["answer_json"])
        except json.JSONDecodeError:
            answer = {"ok": False, "error": "Ответ не читается"}
        out.append({
            "id": int(row["id"]),
            "createdAt": int(row["created_at"]),
            "question": row["question"],
            "answer": answer,
            "journalId": row["journal_id"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
        })
    return out


# --- оценки ----------------------------------------------------------------

def save_feedback(message_id: int, user_id: int, rating: int, comment: str) -> dict:
    if rating < MIN_RATING or rating > MAX_RATING:
        raise Invalid(f"Оценка задаётся числом от {MIN_RATING} до {MAX_RATING}")
    text = (comment or "").strip()[:COMMENT_LIMIT]
    if rating <= COMMENT_REQUIRED_UPTO and not text:
        raise Invalid("При оценке до трёх звёзд нужно указать, что именно не так")
    now = _now()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT m.id, m.journal_id FROM ai_messages m "
            "JOIN ai_dialogs d ON d.id = m.dialog_id "
            "WHERE m.id = ? AND d.user_id = ?",
            (message_id, user_id),
        ).fetchone()
        if row is None:
            raise NotFound("Ответ не найден")
        conn.execute(
            "INSERT INTO ai_feedback (message_id, journal_id, user_id, rating, comment, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET rating = excluded.rating, "
            "comment = excluded.comment, updated_at = excluded.updated_at",
            (message_id, row["journal_id"], user_id, rating, text, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"messageId": message_id, "rating": rating, "comment": text}
