"""Диалоги, сообщения и оценки ответов ИИ.

Лежит в том же файле, что и журнал обращений: журнал отвечает на вопрос
«что система сделала», а эти таблицы — на вопрос «что видел пользователь».
Связаны они journal_id, и разделять их по разным базам было бы неудобно
ровно там, где чаще всего и нужно: в разборе конкретной низкой оценки.

Владение проверяется в каждой операции: диалог виден только своему автору,
включая администратора. Это модель приватности ИБ-4 — администратор работает
с метаданными и оценками, а не с чужой перепиской.

Папки и архив (ИИ-10, решение Р-6 — только личные папки): диалог лежит в папке
(folder_id) или «без папки», архивный (archived_at) скрыт из списка, находится
поиском и восстанавливается. Лимиты числа активных и закреплённых диалогов и
папок — по роли (ИИ-02, quotas.py); при превышении — понятный отказ, без
молчаливого удаления. Срок хранения истории — backend/ai/retention.py.
"""
from __future__ import annotations

import json
import sqlite3
import time

from . import quotas
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

-- ИИ-12: каждая выгрузка таблицы или графика из ответа (кто, какой ответ, что и в каком формате).
CREATE TABLE IF NOT EXISTS ai_exports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    journal_id INTEGER,
    part       TEXT    NOT NULL,
    format     TEXT    NOT NULL,
    row_count  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_exports_user ON ai_exports(user_id, created_at);

-- ИИ-10: личные папки диалогов (Р-6).
CREATE TABLE IF NOT EXISTS ai_folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    title      TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_folders_user ON ai_folders(user_id);

-- ИИ-07: файлы пользователя — только разобранное содержимое, без исходного файла.
-- dialog_id и folder_id пусты — черновик: файл приложен к вопросу нового диалога.
CREATE TABLE IF NOT EXISTS ai_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    dialog_id  INTEGER,
    folder_id  INTEGER,
    name       TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    size       INTEGER NOT NULL,
    sha256     TEXT    NOT NULL,
    meta_json  TEXT,
    notes_json TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_files_owner ON ai_files(user_id, dialog_id, folder_id);
CREATE TABLE IF NOT EXISTS ai_file_parts (
    file_id      INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT    NOT NULL,
    label        TEXT    NOT NULL,
    columns_json TEXT,
    rows_json    TEXT,
    text         TEXT,
    first_row    INTEGER,
    truncated    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_id, seq)
);

-- ИИ-11: память папки и история её правок.
CREATE TABLE IF NOT EXISTS ai_folder_memory (
    folder_id  INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_folder_memory_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id  INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_folder_memory_log ON ai_folder_memory_log(folder_id, id);
"""

# Колонки диалога, появившиеся после первых запусков (ИИ-02, ИИ-10).
LATE_COLUMNS = (("folder_id", "INTEGER"), ("archived_at", "INTEGER"), ("owner_role", "TEXT"))
WARN_DAYS = 7          # за столько дней до удаления по сроку диалог помечается в списке
FOLDER_TITLE_LIMIT = 60


class NotFound(Exception):
    """Диалог или сообщение не найдены либо принадлежат другому человеку."""


class Invalid(Exception):
    """Данные не проходят проверку — сообщение предназначено пользователю."""


class Limit(Exception):
    """Лимит роли (ИИ-02): сообщение для человека и что можно сделать."""

    def __init__(self, param: str, message: str, action: str = ""):
        super().__init__(message)
        self.param = param
        self.message = message
        self.action = action


def _connect() -> sqlite3.Connection:
    JOURNAL_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(JOURNAL_DB)
    conn.row_factory = sqlite3.Row
    # Сводка соединяет оценки с журналом обращений, поэтому обе схемы должны
    # существовать независимо от того, кто первым открыл файл.
    conn.executescript(JOURNAL_DDL)
    conn.executescript(DDL)
    have = {row[1] for row in conn.execute("PRAGMA table_info(ai_dialogs)")}
    for name, kind in LATE_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE ai_dialogs ADD COLUMN {name} {kind}")
    conn.commit()
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

def expires_at(updated_at: int, role: str | None, pinned: bool) -> int | None:
    """Когда диалог удалится по сроку хранения роли (ИИ-02); закреплённый — не удаляется."""
    if pinned:
        return None
    return int(updated_at) + int(quotas.value(role, "history_days")) * 86400


def list_dialogs(user_id: int, limit: int = 500, role: str | None = None) -> list[dict]:
    """Все диалоги владельца, включая архивные (интерфейс прячет их и находит поиском).

    `role` — действующая роль владельца: запоминается у диалогов, чтобы ночная
    очистка знала срок хранения, и даёт дату удаления для пометки в списке.
    """
    conn = _connect()
    try:
        if role:
            conn.execute("UPDATE ai_dialogs SET owner_role = ? WHERE user_id = ? AND owner_role IS NOT ?",
                         (role, user_id, role))
            conn.commit()
        rows = conn.execute(
            "SELECT d.id, d.title, d.created_at, d.updated_at, d.pinned, d.folder_id, d.archived_at,"
            "       (SELECT question FROM ai_messages m WHERE m.dialog_id = d.id"
            "         ORDER BY m.id DESC LIMIT 1) AS last_question,"
            "       (SELECT COUNT(*) FROM ai_messages m WHERE m.dialog_id = d.id) AS messages "
            "FROM ai_dialogs d WHERE d.user_id = ? "
            "ORDER BY d.pinned DESC, d.updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()
    now = _now()
    out = []
    for row in rows:
        item = dict(row)
        item["archived"] = bool(item.get("archived_at"))
        expires = expires_at(item["updated_at"], role, bool(item["pinned"])) if role else None
        item["expires_at"] = expires
        item["expires_soon"] = bool(expires and expires - now <= WARN_DAYS * 86400)
        out.append(item)
    return out


def _active_count(conn: sqlite3.Connection, user_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM ai_dialogs WHERE user_id = ? AND archived_at IS NULL",
                            (user_id,)).fetchone()[0])


def _check_active(conn: sqlite3.Connection, user_id: int, role: str | None) -> None:
    limit = quotas.value(role, "active_dialogs") if role else None
    if limit is not None and _active_count(conn, user_id) >= int(limit):
        quotas.record_hit(role, "active_dialogs")
        raise Limit("active_dialogs",
                    f"Активных диалогов — {limit} из {limit}. Перенесите старые в архив: "
                    "они останутся доступны через поиск.", action="archive_oldest")


def can_start(user_id: int, role: str | None) -> None:
    """Новый диалог по первому вопросу: проверить лимит активных до того, как спрашивать модель."""
    conn = _connect()
    try:
        _check_active(conn, user_id, role)
    finally:
        conn.close()


def create_dialog(user_id: int, title: str = "", role: str | None = None, check: bool = True) -> dict:
    """Новый диалог. `check=False` — ответ уже получен, его нельзя потерять из-за лимита."""
    now = _now()
    clean = " ".join((title or "").split())[:TITLE_LIMIT] or "Новый диалог"
    conn = _connect()
    try:
        if check:
            _check_active(conn, user_id, role)
        cursor = conn.execute(
            "INSERT INTO ai_dialogs (user_id, title, created_at, updated_at, owner_role) VALUES (?, ?, ?, ?, ?)",
            (user_id, clean, now, now, role),
        )
        conn.commit()
        dialog_id = int(cursor.lastrowid)
    finally:
        conn.close()
    return {"id": dialog_id, "title": clean, "created_at": now, "updated_at": now,
            "pinned": 0, "last_question": None, "messages": 0, "folder_id": None,
            "archived_at": None, "archived": False, "expires_at": None, "expires_soon": False}


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


def set_pinned(dialog_id: int, user_id: int, pinned: bool, role: str | None = None) -> dict:
    conn = _connect()
    try:
        dialog = _owned(conn, dialog_id, user_id)
        if pinned and not dialog["pinned"] and role:
            limit = int(quotas.value(role, "pinned_dialogs"))
            count = conn.execute("SELECT COUNT(*) FROM ai_dialogs WHERE user_id = ? AND pinned = 1 "
                                 "AND archived_at IS NULL", (user_id,)).fetchone()[0]
            if count >= limit:
                quotas.record_hit(role, "pinned_dialogs")
                raise Limit("pinned_dialogs", f"Закреплённых диалогов — {limit} из {limit}. "
                                              "Открепите один из них, чтобы закрепить этот.")
        conn.execute("UPDATE ai_dialogs SET pinned = ? WHERE id = ?",
                     (1 if pinned else 0, dialog_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": dialog_id, "pinned": bool(pinned)}


# --- папки и архив (ИИ-10) -------------------------------------------------

def _folder(conn: sqlite3.Connection, folder_id: int, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM ai_folders WHERE id = ? AND user_id = ?", (folder_id, user_id)).fetchone()
    if row is None:
        raise NotFound("Папка не найдена")
    return row


def _title_taken(conn: sqlite3.Connection, user_id: int, title: str, skip: int | None = None) -> bool:
    """Одинаковое имя без учёта регистра; SQLite NOCASE не знает кириллицы — сравниваем в Python."""
    wanted = title.casefold()
    return any(row["title"].casefold() == wanted and row["id"] != skip
               for row in conn.execute("SELECT id, title FROM ai_folders WHERE user_id = ?", (user_id,)))


def _folder_title(title: str) -> str:
    clean = " ".join((title or "").split())[:FOLDER_TITLE_LIMIT]
    if not clean:
        raise Invalid("Название папки не может быть пустым")
    return clean


def list_folders(user_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT f.id, f.title, f.created_at, f.updated_at,"
            "       (SELECT COUNT(*) FROM ai_dialogs d WHERE d.folder_id = f.id AND d.archived_at IS NULL) AS dialogs,"
            # ИИ-11: есть ли память папки и сколько в ней файлов — для пометок в истории.
            "       (SELECT COUNT(*) FROM ai_files x WHERE x.folder_id = f.id) AS files,"
            "       (SELECT LENGTH(m.text) > 0 FROM ai_folder_memory m WHERE m.folder_id = f.id) AS memory "
            "FROM ai_folders f WHERE f.user_id = ?",
            (user_id,)).fetchall()
    finally:
        conn.close()
    return sorted((dict(row) for row in rows), key=lambda item: (item["title"].casefold(), item["id"]))


def create_folder(user_id: int, title: str, role: str | None = None) -> dict:
    clean = _folder_title(title)
    now = _now()
    conn = _connect()
    try:
        if role:
            limit = int(quotas.value(role, "folders"))
            count = conn.execute("SELECT COUNT(*) FROM ai_folders WHERE user_id = ?", (user_id,)).fetchone()[0]
            if count >= limit:
                quotas.record_hit(role, "folders")
                raise Limit("folders", f"Папок — {limit} из {limit}. Удалите или объедините ненужные папки.")
        if _title_taken(conn, user_id, clean):
            raise Invalid("Папка с таким названием уже есть")
        cursor = conn.execute("INSERT INTO ai_folders (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                              (user_id, clean, now, now))
        conn.commit()
        folder_id = int(cursor.lastrowid)
    finally:
        conn.close()
    return {"id": folder_id, "title": clean, "created_at": now, "updated_at": now, "dialogs": 0}


def rename_folder(folder_id: int, user_id: int, title: str) -> dict:
    clean = _folder_title(title)
    conn = _connect()
    try:
        _folder(conn, folder_id, user_id)
        if _title_taken(conn, user_id, clean, skip=folder_id):
            raise Invalid("Папка с таким названием уже есть")
        conn.execute("UPDATE ai_folders SET title = ?, updated_at = ? WHERE id = ?", (clean, _now(), folder_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": folder_id, "title": clean}


def delete_folder(folder_id: int, user_id: int, dialogs: str = "move") -> dict:
    """Удалить папку: диалоги — в «Без папки» (move) или вместе с папкой (delete, необратимо)."""
    if dialogs not in {"move", "delete"}:
        raise Invalid("Выберите, что сделать с диалогами папки")
    conn = _connect()
    try:
        _folder(conn, folder_id, user_id)
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM ai_dialogs WHERE folder_id = ? AND user_id = ?", (folder_id, user_id))]
        if dialogs == "delete":
            for dialog_id in ids:
                _delete(conn, dialog_id)
        else:
            conn.execute("UPDATE ai_dialogs SET folder_id = NULL WHERE folder_id = ? AND user_id = ?",
                         (folder_id, user_id))
        # ИИ-11: память и файлы папки уходят вместе с папкой.
        conn.execute("DELETE FROM ai_file_parts WHERE file_id IN (SELECT id FROM ai_files WHERE folder_id = ?)",
                     (folder_id,))
        conn.execute("DELETE FROM ai_files WHERE folder_id = ?", (folder_id,))
        conn.execute("DELETE FROM ai_folder_memory WHERE folder_id = ?", (folder_id,))
        conn.execute("DELETE FROM ai_folder_memory_log WHERE folder_id = ?", (folder_id,))
        conn.execute("DELETE FROM ai_folders WHERE id = ?", (folder_id,))
        conn.commit()
    finally:
        conn.close()
    return {"deleted": folder_id, "dialogs": len(ids), "mode": dialogs}


def move_dialog(dialog_id: int, user_id: int, folder_id: int | None) -> dict:
    conn = _connect()
    try:
        _owned(conn, dialog_id, user_id)
        if folder_id is not None:
            _folder(conn, int(folder_id), user_id)
        conn.execute("UPDATE ai_dialogs SET folder_id = ? WHERE id = ?", (folder_id, dialog_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": dialog_id, "folder_id": folder_id}


def set_archived(dialog_id: int, user_id: int, archived: bool, role: str | None = None) -> dict:
    """В архив — скрыть из списка (закрепление снимается); из архива — в пределах лимита активных."""
    conn = _connect()
    try:
        dialog = _owned(conn, dialog_id, user_id)
        if archived:
            conn.execute("UPDATE ai_dialogs SET archived_at = ?, pinned = 0 WHERE id = ?", (_now(), dialog_id))
        elif dialog["archived_at"]:
            _check_active(conn, user_id, role)
            conn.execute("UPDATE ai_dialogs SET archived_at = NULL, updated_at = ? WHERE id = ?", (_now(), dialog_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": dialog_id, "archived": bool(archived)}


def archive_oldest(user_id: int, count: int = 10) -> int:
    """Перенести в архив самые старые незакреплённые активные диалоги — ответ на лимит активных."""
    count = max(1, min(int(count), 200))
    conn = _connect()
    try:
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM ai_dialogs WHERE user_id = ? AND archived_at IS NULL AND pinned = 0 "
            "ORDER BY updated_at ASC, id ASC LIMIT ?", (user_id, count))]
        now = _now()
        for dialog_id in ids:
            conn.execute("UPDATE ai_dialogs SET archived_at = ? WHERE id = ?", (now, dialog_id))
        conn.commit()
    finally:
        conn.close()
    return len(ids)


def _delete(conn: sqlite3.Connection, dialog_id: int) -> None:
    """Удаление человеком уносит и оценки: иначе разбор качества ссылался бы на вопрос, которого нет."""
    ids = [int(r["id"]) for r in conn.execute(
        "SELECT id FROM ai_messages WHERE dialog_id = ?", (dialog_id,))]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM ai_feedback WHERE message_id IN ({marks})", ids)
    conn.execute("DELETE FROM ai_messages WHERE dialog_id = ?", (dialog_id,))
    # ИИ-07: файлы диалога удаляются вместе с ним.
    conn.execute("DELETE FROM ai_file_parts WHERE file_id IN (SELECT id FROM ai_files WHERE dialog_id = ?)", (dialog_id,))
    conn.execute("DELETE FROM ai_files WHERE dialog_id = ?", (dialog_id,))
    conn.execute("DELETE FROM ai_dialogs WHERE id = ?", (dialog_id,))


def delete_dialog(dialog_id: int, user_id: int) -> None:
    conn = _connect()
    try:
        _owned(conn, dialog_id, user_id)
        _delete(conn, dialog_id)
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
        if dialog["archived_at"]:
            # Новый вопрос в архивном диалоге возвращает его в список.
            conn.execute("UPDATE ai_dialogs SET archived_at = NULL WHERE id = ?", (dialog_id,))
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


def message(message_id: int, user_id: int) -> dict:
    """Один ответ владельца с записью журнала (роль, привязка, SQL для паспорта выгрузки); чужой — NotFound."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT m.id, m.created_at, m.question, m.answer_json, m.journal_id, "
            "       q.role, q.binding, q.actor, q.scope_label, q.sql_final "
            "FROM ai_messages m JOIN ai_dialogs d ON d.id = m.dialog_id "
            "LEFT JOIN ai_queries q ON q.id = m.journal_id "
            "WHERE m.id = ? AND d.user_id = ?",
            (message_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise NotFound("Ответ не найден")
    try:
        answer = json.loads(row["answer_json"])
    except json.JSONDecodeError:
        answer = {}
    return {"id": int(row["id"]), "createdAt": int(row["created_at"]), "question": row["question"],
            "answer": answer, "journalId": row["journal_id"],
            "journal": {k: row[k] for k in ("role", "binding", "actor", "scope_label", "sql_final")}}


def record_export(message_id: int, user_id: int, journal_id: int | None, part: str, fmt: str, row_count: int) -> None:
    conn = _connect()
    try:
        conn.execute("INSERT INTO ai_exports (created_at, user_id, message_id, journal_id, part, format, row_count) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (_now(), user_id, message_id, journal_id, part[:60], fmt, row_count))
        conn.commit()
    finally:
        conn.close()


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


# --- ИИ-11: память папки -----------------------------------------------------------

MEMORY_HISTORY = 10


def _memory_limit(role: str | None) -> int:
    return int(quotas.value(role, "folder_memory_chars"))


def folder_memory(folder_id: int, user_id: int, role: str | None = None) -> dict:
    """Память папки, предел знаков и последние правки (новые сверху)."""
    conn = _connect()
    try:
        folder = _folder(conn, folder_id, user_id)
        row = conn.execute("SELECT text, updated_at FROM ai_folder_memory WHERE folder_id = ?", (folder_id,)).fetchone()
        history = [dict(r) for r in conn.execute(
            "SELECT id, text, created_at FROM ai_folder_memory_log WHERE folder_id = ? ORDER BY id DESC LIMIT ?",
            (folder_id, MEMORY_HISTORY))]
    finally:
        conn.close()
    return {"folderId": folder_id, "folder": folder["title"], "text": row["text"] if row else "",
            "updatedAt": row["updated_at"] if row else None, "limit": _memory_limit(role),
            "history": [{"id": h["id"], "text": h["text"], "at": h["created_at"]} for h in history]}


def set_folder_memory(folder_id: int, user_id: int, text: str, role: str | None = None) -> dict:
    """Сохранить память папки. Пустой текст — память выключена. Каждая правка — в историю."""
    text = (text or "").replace("\r\n", "\n").strip()
    limit = _memory_limit(role)
    if len(text) > limit:
        raise Limit("folder_memory_chars", f"Память папки — до {limit} знаков, сейчас {len(text)}.")
    conn = _connect()
    try:
        _folder(conn, folder_id, user_id)
        current = conn.execute("SELECT text FROM ai_folder_memory WHERE folder_id = ?", (folder_id,)).fetchone()
        if not current or current["text"] != text:
            now = _now()
            conn.execute("INSERT INTO ai_folder_memory (folder_id, user_id, text, updated_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(folder_id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at",
                         (folder_id, user_id, text, now))
            conn.execute("INSERT INTO ai_folder_memory_log (folder_id, user_id, text, created_at) VALUES (?, ?, ?, ?)",
                         (folder_id, user_id, text, now))
            conn.execute("DELETE FROM ai_folder_memory_log WHERE folder_id = ? AND id NOT IN (SELECT id FROM "
                         "ai_folder_memory_log WHERE folder_id = ? ORDER BY id DESC LIMIT 50)", (folder_id, folder_id))
            conn.commit()
    finally:
        conn.close()
    return folder_memory(folder_id, user_id, role)


def dialog_folder(dialog_id: int | None, user_id: int) -> dict | None:
    """Папка диалога и её память: {"id", "title", "memory"} или None."""
    if not dialog_id:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT f.id, f.title, m.text AS memory FROM ai_dialogs d JOIN ai_folders f ON f.id = d.folder_id "
            "LEFT JOIN ai_folder_memory m ON m.folder_id = f.id WHERE d.id = ? AND d.user_id = ? AND f.user_id = ?",
            (int(dialog_id), user_id, user_id)).fetchone()
    finally:
        conn.close()
    return {"id": int(row["id"]), "title": row["title"], "memory": row["memory"] or ""} if row else None
