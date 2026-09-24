"""Обратная связь по данным из раздела «Контроль» (общий бэклог №30).

Раньше замечание сохранялось только в браузере (localStorage azs:feedback):
пользователь видел «отправлено», а до администратора запись не доходила.
Теперь замечание уходит на сервер (таблица station_feedback в auth.sqlite3 —
той же базе, что пользователи и визиты, её же бэкапить), администратор видит
его на вкладке «Обратная связь» и ведёт по статусам: новое → в работе →
исправлено / отклонено, с комментарием и отметкой, кто разобрал.

Записи, накопленные раньше в браузере, интерфейс досылает один раз; повтор
отсекается по clientId (уникален для пользователя).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

STATUSES = {
    "new": "Новое",
    "in_progress": "В работе",
    "done": "Исправлено",
    "rejected": "Отклонено",
}
HOURLY_LIMIT = 30          # замечаний от одного человека за час — защита от случайного цикла отправки
LIST_LIMIT = 500

DDL = """
CREATE TABLE IF NOT EXISTS station_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  INTEGER NOT NULL,
    user_id     INTEGER,
    user_email  TEXT,
    user_name   TEXT,
    station     TEXT NOT NULL DEFAULT '',
    field       TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    admin_note  TEXT NOT NULL DEFAULT '',
    handled_by  TEXT,
    handled_at  INTEGER,
    client_id   TEXT,
    source      TEXT NOT NULL DEFAULT 'control'
);
CREATE INDEX IF NOT EXISTS idx_station_feedback_created ON station_feedback(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_station_feedback_client ON station_feedback(user_id, client_id)
    WHERE client_id IS NOT NULL;
"""


class FeedbackIn(BaseModel):
    station: str = Field(default="", max_length=120)
    field: str = Field(default="", max_length=200)
    message: str = Field(min_length=1, max_length=2000)
    # Для записей, сохранённых раньше только в браузере: их id и время создания.
    clientId: str | None = Field(default=None, max_length=80)
    createdAt: str | None = Field(default=None, max_length=40)


class FeedbackReview(BaseModel):
    status: str = Field(max_length=20)
    note: str = Field(default="", max_length=1000)


def _created(value: str | None, now: int) -> int:
    """Время создания из браузера — только для досылки старых записей и не из будущего."""
    if not value:
        return now
    try:
        from datetime import datetime

        stamp = int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return now
    return stamp if 0 < stamp <= now else now


def _row(row) -> dict:
    return {
        "id": row["id"], "createdAt": row["created_at"], "userEmail": row["user_email"] or "",
        "userName": row["user_name"] or "", "station": row["station"], "field": row["field"],
        "message": row["message"], "status": row["status"], "statusTitle": STATUSES.get(row["status"], row["status"]),
        "note": row["admin_note"], "handledBy": row["handled_by"] or "", "handledAt": row["handled_at"],
        "source": row["source"],
    }


def build_router(require_user: Callable, require_admin: Callable, connection: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"])

    @contextmanager
    def connect():
        conn = connection()
        try:
            conn.executescript(DDL)
            yield conn
            conn.commit()
        finally:
            conn.close()

    @router.post("/feedback")
    def feedback_create(payload: FeedbackIn, user=Depends(require_user)):
        message = payload.message.strip()
        if not message:
            raise HTTPException(status_code=422, detail="Опишите, что не так с данными")
        now = int(time.time())
        user_id = getattr(user, "id", None)
        with connect() as conn:
            if payload.clientId:
                known = conn.execute(
                    "SELECT id FROM station_feedback WHERE user_id IS ? AND client_id = ?",
                    (user_id, payload.clientId)).fetchone()
                if known:
                    return {"id": known["id"], "duplicate": True}
            recent = conn.execute(
                "SELECT COUNT(*) FROM station_feedback WHERE user_id IS ? AND created_at >= ? AND source = 'control'",
                (user_id, now - 3600)).fetchone()[0]
            if recent >= HOURLY_LIMIT:
                raise HTTPException(status_code=429, detail="Слишком много замечаний за час — попробуйте позже")
            cursor = conn.execute(
                "INSERT INTO station_feedback (created_at, user_id, user_email, user_name, station, field, message, "
                "client_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_created(payload.createdAt, now) if payload.clientId else now, user_id,
                 getattr(user, "email", "") or "", getattr(user, "name", "") or "",
                 payload.station.strip(), payload.field.strip(), message, payload.clientId,
                 "browser" if payload.clientId else "control"))
            conn.commit()
            return {"id": int(cursor.lastrowid), "duplicate": False}

    @router.get("/admin/feedback")
    def feedback_list(status: str = "", _admin=Depends(require_admin)):
        with connect() as conn:
            counts = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM station_feedback GROUP BY status")}
            if status and status not in STATUSES:
                raise HTTPException(status_code=400, detail="Неизвестный статус")
            where, args = ("WHERE status = ?", (status,)) if status else ("", ())
            rows = conn.execute(
                f"SELECT * FROM station_feedback {where} ORDER BY created_at DESC, id DESC LIMIT {LIST_LIMIT}",
                args).fetchall()
        return {
            "items": [_row(row) for row in rows],
            "counts": {code: int(counts.get(code, 0)) for code in STATUSES},
            "statuses": [{"code": code, "title": title} for code, title in STATUSES.items()],
        }

    @router.patch("/admin/feedback/{feedback_id}")
    def feedback_review(feedback_id: int, payload: FeedbackReview, admin=Depends(require_admin)):
        if payload.status not in STATUSES:
            raise HTTPException(status_code=400, detail="Неизвестный статус")
        with connect() as conn:
            found = conn.execute("SELECT id FROM station_feedback WHERE id = ?", (feedback_id,)).fetchone()
            if not found:
                raise HTTPException(status_code=404, detail="Замечание не найдено")
            conn.execute(
                "UPDATE station_feedback SET status = ?, admin_note = ?, handled_by = ?, handled_at = ? WHERE id = ?",
                (payload.status, payload.note.strip(), getattr(admin, "email", "") or "", int(time.time()), feedback_id))
            conn.commit()
            row = conn.execute("SELECT * FROM station_feedback WHERE id = ?", (feedback_id,)).fetchone()
        return _row(row)

    return router
