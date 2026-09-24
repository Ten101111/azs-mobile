"""API справок: список и актуальный выпуск, файлы, запрос на перевыпуск, приём выпусков.

Всё — за авторизацией; справки пока видит только администратор (Р-13). Файлы
отдаются только через этот API, публичных ссылок нет.

Справку собирает компьютер владельца по витрине ОХД (решение 24.09.2026) и
присылает её на внутренние адреса со своим токеном (REPORTS_IMPORT_TOKEN):
`status` — что нужно собрать, `import` — готовый выпуск, `not-formed` — почему
выпуска нет. Сервер к ОХД не ходит и справку сам не считает.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import periods, registry, service, store, weekly

logger = logging.getLogger("reports")
SCHEDULE_TEXT = ("Собирается по витрине ОХД на компьютере с доступом к ОХД: в понедельник с 07:10 МСК каждый час, "
                 "пока данные за воскресенье не загрузятся")
MEDIA = {"pdf": "application/pdf",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
MAX_MODEL_BYTES = 2_000_000


class GenerateRequest(BaseModel):
    type: str = ""
    reason: str = Field(default="", max_length=300)
    week: str = ""   # «2026-W38»; пусто — последняя завершённая неделя


class StatusRequest(BaseModel):
    week: str = ""


class ImportRequest(BaseModel):
    model: dict
    requestId: int | None = None


class NotFormedRequest(BaseModel):
    week: str
    reason: str = Field(default="", max_length=1000)
    requestId: int | None = None


def resolve_file(stored: str) -> Path | None:
    if not stored:
        return None
    path = Path(stored)
    if not path.is_absolute():
        path = store.FILES_DIR / path
    return path if path.exists() else None


def final_attempt(now: datetime) -> bool:
    """Письмо о несформированной справке — после последней утренней попытки понедельника."""
    return now.weekday() != 0 or now.hour >= 13


def _week(value: str) -> periods.Week:
    try:
        return periods.parse_iso(value) if value else periods.last_complete_week(service.today_msk())
    except (ValueError, TypeError) as err:
        raise HTTPException(status_code=400, detail="Неделя задаётся как ГГГГ-Wнн") from err


def build_router(require_user: Callable, require_admin: Callable, require_import: Callable,
                 notify_admins: Callable[[str, str], None] | None = None) -> APIRouter:
    router = APIRouter(tags=["reports"])

    def check(user) -> None:
        if not registry.allowed(user):
            raise HTTPException(status_code=403, detail="Справки пока доступны только администратору")

    @router.get("/api/reports")
    def overview(type: str = "", user=Depends(require_user)):
        check(user)
        rt = registry.get(type)
        with store.connect() as conn:
            latest = store.latest(conn, rt.code, rt.scope_key)
            pending = store.pending(conn, rt.code, rt.scope_key)
            archive = [store.summary(row) for row in store.archive(conn, rt.code, rt.scope_key)]
            request = store.request_summary(store.open_request(conn, rt.code, rt.scope_key))
        return {
            "types": [{"code": t.code, "title": t.title, "description": t.description}
                      for t in registry.REPORT_TYPES.values()],
            "type": rt.code,
            "current": store.full(latest) if latest else None,
            "pending": store.summary(pending) if pending else None,
            "request": request,
            "archive": archive,
            "schedule": SCHEDULE_TEXT,
            "canGenerate": bool(getattr(user, "isAdmin", False)),
            "expectedWeek": periods.last_complete_week(service.today_msk()).as_dict(),
        }

    @router.get("/api/reports/runs")
    def runs(user=Depends(require_admin)):
        with store.connect() as conn:
            return {"runs": store.runs(conn, 30)}

    @router.get("/api/reports/{issue_id}")
    def issue(issue_id: int, user=Depends(require_user)):
        check(user)
        with store.connect() as conn:
            row = store.get(conn, issue_id)
        if row is None or row["status"] == store.NOT_FORMED:
            raise HTTPException(status_code=404, detail="Выпуск не найден")
        return store.full(row)

    @router.get("/api/reports/{issue_id}/file/{kind}")
    def issue_file(issue_id: int, kind: str, user=Depends(require_user)):
        check(user)
        if kind not in MEDIA:
            raise HTTPException(status_code=404, detail="Нет такого формата")
        with store.connect() as conn:
            row = store.get(conn, issue_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Выпуск не найден")
        path = resolve_file(row["pdf_path"] if kind == "pdf" else row["xlsx_path"])
        if path is None:
            raise HTTPException(status_code=404, detail="Файл выпуска не найден")
        model = store.full(row)["model"] or {"week": {"iso": row["week"]}}
        return FileResponse(path, media_type=MEDIA[kind], filename=service.file_name(model, row["version"], kind))

    @router.post("/api/reports/generate")
    def generate(payload: GenerateRequest, user=Depends(require_admin)):
        """Перевыпуск: сервер справку не считает — ставит запрос для сборщика на компьютере с доступом к ОХД."""
        reason = " ".join(payload.reason.split())
        if len(reason) < 3:
            raise HTTPException(status_code=400, detail="Укажите причину перевыпуска")
        week = _week(payload.week)
        return service.request_reissue(week.iso, reason, created_by=getattr(user, "email", ""),
                                       type_code=payload.type or None)

    @router.post("/api/internal/reports/status", dependencies=[Depends(require_import)])
    def import_status(payload: StatusRequest):
        return service.status(_week(payload.week).iso)

    @router.post("/api/internal/reports/import", dependencies=[Depends(require_import)])
    def import_issue(payload: ImportRequest):
        if len(payload.model_dump_json().encode("utf-8")) > MAX_MODEL_BYTES:
            raise HTTPException(status_code=413, detail="Модель выпуска слишком большая")
        try:
            return service.accept(payload.model, request_id=payload.requestId)
        except service.InvalidModel as err:
            raise HTTPException(status_code=422, detail=f"Выпуск не принят: {err}") from err

    @router.post("/api/internal/reports/not-formed", dependencies=[Depends(require_import)])
    def import_not_formed(payload: NotFormedRequest):
        week = _week(payload.week)
        result = service.not_formed(week.iso, payload.reason, request_id=payload.requestId)
        if notify_admins is not None:
            now = datetime.now(weekly.MSK)
            if final_attempt(now) and not service.already_notified(week.iso, weekly.TYPE_CODE):
                text = (f"Справка по сети за неделю {week.label} не сформирована.\n"
                        f"Причина: {result['reason']}\n\n"
                        "В разделе «Справки» остаётся прошлая справка с пометкой. Справка собирается по витрине ОХД "
                        "на компьютере с доступом к ОХД: когда данные загрузятся и VPN будет подключён, выпуск "
                        "сформируется при следующем запуске.")
                try:
                    notify_admins(f"Справка по сети за {week.label} не сформирована", text)
                    service.mark_notified(week.iso, weekly.TYPE_CODE, result["reason"])
                except Exception as err:  # noqa: BLE001 - письмо не должно ронять приём
                    logger.warning("Report notice not sent: %s", err)
        return result

    return router
