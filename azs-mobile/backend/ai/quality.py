"""Качество ответов ИИ: сводка, лента оценок и разбор.

Модель приватности ИБ-4 (БТ-КК8): администратору доступны метаданные,
оценка, комментарий и текст запроса к витрине. Переписка целиком остаётся
у автора диалога — здесь её нет и быть не должно.

И отдельно про БТ-КК10: оценки — обратная связь по продукту, а не мера
работы сотрудника. Как только они начнут попадать в разговор о человеке,
низкие оценки исчезнут, и продукт перестанет получать сигнал о том, где
врёт. Поэтому надпись об этом в интерфейсе — не формальность.
"""
from __future__ import annotations

import sqlite3
import time

from .dialogs import MAX_RATING, MIN_RATING, _connect

# Разбор низкой оценки: статусы и срок первичной реакции (БТ-КК5,
# решение по вопросу №25 реестра — два рабочих дня).
STATUSES = ("new", "review", "resolved", "declined")
STATUS_TITLES = {
    "new": "Новая",
    "review": "В разборе",
    "resolved": "Решена",
    "declined": "Отклонена",
}
FIRST_RESPONSE_DAYS = 2
DAY = 86400

# Отказ и техническая ошибка — разные вещи: первое означает, что граница
# области данных сработала как задумано, второе — что сломался контур.
FAILURE_RULES = ("execution", "model_unavailable")


class Invalid(Exception):
    """Данные не проходят проверку — сообщение предназначено пользователю."""


REVIEW_DDL = """
CREATE TABLE IF NOT EXISTS ai_feedback_review (
    message_id  INTEGER PRIMARY KEY,
    status      TEXT    NOT NULL DEFAULT 'new',
    owner       TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    in_golden   INTEGER NOT NULL DEFAULT 0,
    first_seen  INTEGER,
    updated_at  INTEGER NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    conn = _connect()
    conn.executescript(REVIEW_DDL)
    return conn


def _period(days: int) -> int:
    return int(time.time()) - max(1, min(days, 400)) * DAY


def summary(days: int = 30) -> dict:
    """Сводка за период (БТ-КК2)."""
    since = _period(days)
    conn = _conn()
    try:
        asked = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_queries WHERE created_at >= ?", (since,)
        ).fetchone()["n"]
        verdicts = {row["verdict"]: row["n"] for row in conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM ai_queries WHERE created_at >= ? "
            "GROUP BY verdict", (since,))}
        timing = conn.execute(
            "SELECT AVG(COALESCE(model_ms,0) + COALESCE(sql_ms,0)) AS avg_ms,"
            "       MAX(COALESCE(model_ms,0) + COALESCE(sql_ms,0)) AS max_ms "
            "FROM ai_queries WHERE created_at >= ? AND verdict = 'ok'", (since,)
        ).fetchone()
        rated = conn.execute(
            "SELECT COUNT(*) AS n, AVG(rating) AS avg FROM ai_feedback WHERE created_at >= ?",
            (since,)
        ).fetchone()
        spread = {int(r["rating"]): int(r["n"]) for r in conn.execute(
            "SELECT rating, COUNT(*) AS n FROM ai_feedback WHERE created_at >= ? "
            "GROUP BY rating", (since,))}
        open_review = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_feedback f "
            "LEFT JOIN ai_feedback_review r ON r.message_id = f.message_id "
            "WHERE f.created_at >= ? AND f.rating <= 3 "
            "  AND COALESCE(r.status, 'new') IN ('new', 'review')", (since,)
        ).fetchone()["n"]
        overdue = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_feedback f "
            "LEFT JOIN ai_feedback_review r ON r.message_id = f.message_id "
            "WHERE f.rating <= 3 AND COALESCE(r.status, 'new') = 'new' AND f.created_at < ?",
            (int(time.time()) - FIRST_RESPONSE_DAYS * DAY,)
        ).fetchone()["n"]
    finally:
        conn.close()

    refused = sum(n for v, n in verdicts.items() if v == "rejected")
    failed = sum(n for v, n in verdicts.items() if v in ("execution_error", "model_unavailable"))
    return {
        "days": days,
        "asked": int(asked),
        "answered": int(verdicts.get("ok", 0)),
        "refused": int(refused),
        "failed": int(failed),
        "rated": int(rated["n"] or 0),
        "average": round(float(rated["avg"]), 2) if rated["avg"] is not None else None,
        "spread": {str(n): spread.get(n, 0) for n in range(MIN_RATING, MAX_RATING + 1)},
        "avgMs": int(timing["avg_ms"] or 0),
        "maxMs": int(timing["max_ms"] or 0),
        "openReview": int(open_review),
        "overdue": int(overdue),
        "firstResponseDays": FIRST_RESPONSE_DAYS,
    }


def entries(days: int = 30, rating: int | None = None, role: str = "",
            verdict: str = "", status: str = "", prompt_version: str = "",
            model: str = "", limit: int = 200) -> list[dict]:
    """Лента оценок с фильтрами (БТ-КК3, БТ-КК4)."""
    since = _period(days)
    where = ["f.created_at >= ?"]
    args: list = [since]
    if rating is not None:
        where.append("f.rating = ?")
        args.append(int(rating))
    if role:
        where.append("q.role = ?")
        args.append(role)
    if verdict == "refused":
        where.append("q.verdict = 'rejected'")
    elif verdict == "failed":
        where.append("q.verdict IN ('execution_error', 'model_unavailable')")
    elif verdict == "ok":
        where.append("q.verdict = 'ok'")
    if status:
        where.append("COALESCE(r.status, 'new') = ?")
        args.append(status)
    if prompt_version:
        where.append("q.prompt_version = ?")
        args.append(prompt_version)
    if model:
        where.append("q.model = ?")
        args.append(model)

    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT f.message_id, f.rating, f.comment, f.created_at,"
            "       m.question, q.role, q.binding, q.scope_label, q.model, q.verdict,"
            "       q.rule, q.sql_final, q.prompt_version,"
            "       COALESCE(q.model_ms, 0) + COALESCE(q.sql_ms, 0) AS total_ms,"
            "       COALESCE(r.status, 'new') AS status, COALESCE(r.owner, '') AS owner,"
            "       COALESCE(r.note, '') AS note, COALESCE(r.in_golden, 0) AS in_golden,"
            "       r.first_seen "
            "FROM ai_feedback f "
            "JOIN ai_messages m ON m.id = f.message_id "
            "LEFT JOIN ai_queries q ON q.id = f.journal_id "
            "LEFT JOIN ai_feedback_review r ON r.message_id = f.message_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY f.created_at DESC LIMIT ?",
            (*args, max(1, min(limit, 1000))),
        ).fetchall()
    finally:
        conn.close()

    now = int(time.time())
    out = []
    for row in rows:
        item = dict(row)
        item["statusTitle"] = STATUS_TITLES.get(item["status"], item["status"])
        # Просрочка считается только по неразобранным низким оценкам: высокая
        # оценка разбора не требует.
        item["overdue"] = bool(
            item["rating"] <= 3 and item["status"] == "new"
            and now - int(item["created_at"]) > FIRST_RESPONSE_DAYS * DAY
        )
        out.append(item)
    return out


def versions(days: int = 120) -> list[dict]:
    """Срез по версиям инструкции (БТ-КК7).

    Без него нельзя сказать, улучшила ли новая версия качество: средняя
    оценка сама по себе ничего не говорит, пока не с чем сравнить.
    """
    since = _period(days)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT COALESCE(q.prompt_version, '—') AS version, q.model AS model,"
            "       COUNT(*) AS asked,"
            "       SUM(CASE WHEN q.verdict = 'ok' THEN 1 ELSE 0 END) AS answered,"
            "       MIN(q.created_at) AS since_at, MAX(q.created_at) AS until_at "
            "FROM ai_queries q WHERE q.created_at >= ? "
            "GROUP BY version, model ORDER BY until_at DESC", (since,)
        ).fetchall()
        marks = {(r["version"], r["model"]): r for r in conn.execute(
            "SELECT COALESCE(q.prompt_version, '—') AS version, q.model AS model,"
            "       COUNT(*) AS rated, AVG(f.rating) AS average,"
            "       SUM(CASE WHEN f.rating <= 3 THEN 1 ELSE 0 END) AS low "
            "FROM ai_feedback f JOIN ai_queries q ON q.id = f.journal_id "
            "WHERE f.created_at >= ? GROUP BY version, model", (since,))}
    finally:
        conn.close()

    out = []
    for row in rows:
        mark = marks.get((row["version"], row["model"]))
        out.append({
            "version": row["version"],
            "model": row["model"],
            "asked": int(row["asked"]),
            "answered": int(row["answered"] or 0),
            "sinceAt": int(row["since_at"] or 0),
            "untilAt": int(row["until_at"] or 0),
            "rated": int(mark["rated"]) if mark else 0,
            "average": round(float(mark["average"]), 2) if mark and mark["average"] is not None else None,
            "low": int(mark["low"]) if mark else 0,
        })
    return out


def set_review(message_id: int, status: str = "", owner: str | None = None,
               note: str | None = None, in_golden: bool | None = None) -> dict:
    """Разбор оценки (БТ-КК5) и отправка вопроса в эталонный набор (БТ-КК6)."""
    if status and status not in STATUSES:
        raise Invalid(f"Неизвестный статус разбора: {status}")
    now = int(time.time())
    conn = _conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM ai_feedback WHERE message_id = ?", (message_id,)
        ).fetchone()
        if exists is None:
            raise Invalid("Оценка не найдена")
        current = conn.execute(
            "SELECT * FROM ai_feedback_review WHERE message_id = ?", (message_id,)
        ).fetchone()
        row = {
            "status": current["status"] if current else "new",
            "owner": current["owner"] if current else "",
            "note": current["note"] if current else "",
            "in_golden": current["in_golden"] if current else 0,
            # Дата первичной реакции ставится один раз — когда оценку впервые
            # взяли в работу. Потом она не двигается, иначе срок реакции
            # можно было бы «обновить» повторным нажатием.
            "first_seen": current["first_seen"] if current else None,
        }
        if status:
            row["status"] = status
            if status != "new" and row["first_seen"] is None:
                row["first_seen"] = now
        if owner is not None:
            row["owner"] = owner.strip()[:120]
        if note is not None:
            row["note"] = note.strip()[:2000]
        if in_golden is not None:
            row["in_golden"] = 1 if in_golden else 0
        conn.execute(
            "INSERT INTO ai_feedback_review "
            "(message_id, status, owner, note, in_golden, first_seen, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET status = excluded.status, "
            "owner = excluded.owner, note = excluded.note, in_golden = excluded.in_golden, "
            "first_seen = excluded.first_seen, updated_at = excluded.updated_at",
            (message_id, row["status"], row["owner"], row["note"],
             row["in_golden"], row["first_seen"], now),
        )
        conn.commit()
    finally:
        conn.close()
    row["messageId"] = message_id
    row["statusTitle"] = STATUS_TITLES.get(row["status"], row["status"])
    return row
