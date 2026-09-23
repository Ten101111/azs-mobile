"""HTTP-контур демонстрации ИИ.

Раздел закрыт двумя условиями сразу: признаком AI_DEMO_ENABLED и правами
администратора. Выбор роли в интерфейсе — демонстрационный переключатель
области данных, а не ролевая модель продукта: она описана в БТ отдельно.
"""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from . import contract
from . import dialogs as dialog_store
from . import executor, generator, journal, pipeline, quality, quality_export
from .. import roles as role_model
from .scope import REFERENCE_DB, UNRESTRICTED_ROLES
from . import dwh_scope


def demo_enabled() -> bool:
    return (os.environ.get("AI_DEMO_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    role: str = Field(default="admin", max_length=40)
    binding: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=120)
    dialogId: int | None = None
    # auto — глубину выбирает разбор задачи; fast/analyze/deep — принудительно.
    depth: str = Field(default="auto", max_length=10)


class DialogRequest(BaseModel):
    title: str = Field(default="", max_length=dialog_store.TITLE_LIMIT)


class PinRequest(BaseModel):
    pinned: bool = True


class ReviewRequest(BaseModel):
    status: str = Field(default="", max_length=20)
    owner: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=2000)
    inGolden: bool | None = None


class FeedbackRequest(BaseModel):
    messageId: int
    rating: int = Field(ge=dialog_store.MIN_RATING, le=dialog_store.MAX_RATING)
    comment: str = Field(default="", max_length=dialog_store.COMMENT_LIMIT)


class AskResponse(BaseModel):
    ok: bool
    question: str
    scopeLabel: str
    summary: str = ""
    sql: str | None = None
    sqlRaw: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    notes: list[str] = []
    truncated: bool = False
    model: str | None = None
    modelMs: int = 0
    narrateMs: int = 0
    sqlMs: int = 0
    rowCount: int = 0
    attempts: int = 0
    error: str | None = None
    rule: str | None = None
    dialogId: int | None = None
    messageId: int | None = None
    dialogTitle: str = ""
    # Агент: глубина, тип задачи, структурированный вывод, графики, таблицы, шаги.
    depth: str = "fast"
    taskType: str = "lookup"
    analysis: dict | None = None
    charts: list[dict] = []
    tables: list[dict] = []
    steps: list[dict] = []
    frame: dict = {}
    plan: dict | None = None
    grounding: dict | None = None
    planMs: int = 0


class Identity(BaseModel):
    role: str
    roleTitle: str
    binding: str | None = None
    stations: int


class StatusResponse(BaseModel):
    enabled: bool
    modelAvailable: bool
    model: str
    installedModels: list[str]
    backend: str
    identities: list[Identity]
    mayImpersonate: bool = False
    ownRole: str = ""
    ownRoleTitle: str = ""
    ownScopeLabel: str = ""
    maySeeSql: bool = False
    commentRequiredUpTo: int = dialog_store.COMMENT_REQUIRED_UPTO
    ownName: str = ""
    ownEmail: str = ""
    agentEnabled: bool = False
    depths: list[dict] = []


DEPTH_OPTIONS = [
    {"code": "auto", "title": "Авто", "hint": "глубину выбирает разбор задачи"},
    {"code": "fast", "title": "Быстро", "hint": "один запрос и короткий ответ"},
    {"code": "analyze", "title": "Анализ", "hint": "сравнения, динамика, несколько запросов"},
    {"code": "deep", "title": "Глубокий анализ", "hint": "причины, аномалии, сценарии: серия запросов, Python, графики"},
]


def _strip_code(steps: list[dict]) -> list[dict]:
    """Шаги без SQL и кода — для ролей, которым текст запроса не показывается."""
    return [{k: v for k, v in step.items() if k not in {"sql", "code", "output"}} for step in steps]


def _response(answer, show_sql: bool) -> "AskResponse":
    steps = list(getattr(answer, "steps", []) or [])
    return AskResponse(
        ok=answer.ok,
        question=answer.question,
        scopeLabel=answer.scope_label,
        summary=answer.summary,
        # Запрос уходит на клиент только тем ролям, которым он разрешён:
        # прятать его вёрсткой недостаточно — он был бы виден в трафике.
        sql=answer.sql if show_sql else None,
        sqlRaw=answer.sql_raw if show_sql else None,
        columns=answer.columns,
        rows=answer.rows,
        notes=answer.notes,
        truncated=answer.truncated,
        model=answer.model,
        modelMs=answer.model_ms,
        narrateMs=answer.narrate_ms,
        sqlMs=answer.sql_ms,
        rowCount=len(answer.rows),
        attempts=answer.attempts,
        error=answer.error,
        rule=answer.rule,
        depth=getattr(answer, "depth", "fast"),
        taskType=getattr(answer, "task_type", "lookup"),
        analysis=getattr(answer, "analysis", None),
        charts=list(getattr(answer, "charts", []) or []),
        tables=list(getattr(answer, "tables", []) or []),
        steps=steps if show_sql else _strip_code(steps),
        frame=dict(getattr(answer, "frame", {}) or {}),
        plan=getattr(answer, "plan", None),
        grounding=getattr(answer, "grounding", None),
        planMs=int(getattr(answer, "plan_ms", 0) or 0),
    )


ROLE_TITLES = {
    "admin": "Администратор — вся сеть",
    "aup_npo": "АУП общества",
    "regional_manager": "Руководитель управления",
    "territory_manager": "Территориальный менеджер",
}

# Просмотр от чужого имени доступен только администратору: рядовой
# пользователь всегда спрашивает от себя, роль берётся из учётной записи.
def _may_impersonate(user) -> bool:
    return bool(getattr(user, "isAdmin", False))


# Текст запроса показывается администратору и субадминистратору; остальным
# ролям происхождение числа объясняет блок «Откуда число» (решение владельца
# от 21.09.2026, уточняет БТ-П3).
SQL_ROLES = {"admin", "subadmin"}


def _may_see_sql(user) -> bool:
    return bool(getattr(user, "isAdmin", False)) or (getattr(user, "role", "") or "") in SQL_ROLES


def _identities(limit_per_role: int = 12) -> list[Identity]:
    items = [Identity(role="admin", roleTitle=ROLE_TITLES["admin"], binding=None, stations=0)]
    if dwh_scope.enabled():
        # ИИ живёт в ДВХ: «от имени» предлагаем тех, кто есть в справочниках ОХД.
        items.extend(
            Identity(role=role, roleTitle=ROLE_TITLES[role], binding=binding, stations=count)
            for role, binding, count in dwh_scope.identities(limit_per_role)
        )
        return items
    if not Path(REFERENCE_DB).exists():
        return items
    conn = sqlite3.connect(f"file:{REFERENCE_DB}?mode=ro", uri=True)
    try:
        for role, column in (
            ("aup_npo", "npo"),
            ("regional_manager", "regional_manager"),
            ("territory_manager", "territory_manager"),
        ):
            rows = conn.execute(
                f"SELECT {column} AS binding, COUNT(*) AS n FROM stations "
                f"WHERE {column} IS NOT NULL AND is_active = 1 "
                f"GROUP BY {column} ORDER BY n DESC LIMIT ?",
                (limit_per_role,),
            ).fetchall()
            items.extend(
                Identity(
                    role=role,
                    roleTitle=ROLE_TITLES[role],
                    binding=str(binding),
                    stations=int(count),
                )
                for binding, count in rows
            )
    finally:
        conn.close()
    return items


def build_router(require_admin: Callable, require_user: Callable | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/ai", tags=["ai"])
    gate = require_user or require_admin

    def guard(user=Depends(gate)):
        """Раздел доступен при включённом признаке и роли с правом диалога."""
        if not demo_enabled():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Раздел ИИ отключён (AI_DEMO_ENABLED)",
            )
        if not (getattr(user, "isAdmin", False) or getattr(user, "aiDialog", False)):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Диалог с ИИ недоступен для вашей роли",
            )
        return user

    @router.get("/status", response_model=StatusResponse)
    def ai_status(user=Depends(guard)):
        models = generator.installed_models()
        impersonate = _may_impersonate(user)
        return StatusResponse(
            enabled=True,
            modelAvailable=generator.available(),
            model=generator.MODEL,
            installedModels=models,
            backend=executor.BACKEND,
            identities=_identities() if impersonate else [],
            mayImpersonate=impersonate,
            ownRole=getattr(user, "role", "") or "",
            ownRoleTitle=getattr(user, "roleTitle", "") or "",
            ownScopeLabel=getattr(user, "scopeLabel", "") or "",
            maySeeSql=_may_see_sql(user),
            ownName=getattr(user, "name", "") or "",
            ownEmail=getattr(user, "email", "") or "",
            agentEnabled=pipeline.AGENT_ENABLED,
            depths=DEPTH_OPTIONS if pipeline.AGENT_ENABLED else [],
        )

    def _identity(payload: AskRequest, user) -> tuple[str, str | None, str | None]:
        """Роль, привязка и подпись для журнала.

        По умолчанию человек спрашивает от себя. Подменить роль может только
        администратор — и это попадает в журнал вместе с его почтой.
        """
        role = (getattr(user, "role", "") or "").strip()
        binding = (getattr(user, "roleBinding", "") or "").strip() or None
        actor = getattr(user, "email", None)

        asked_role = payload.role.strip().lower()
        if asked_role and _may_impersonate(user):
            role = asked_role
            binding = (payload.binding or "").strip() or None
            if role not in UNRESTRICTED_ROLES and not binding:
                raise HTTPException(status_code=400, detail="Для выбранной роли нужна привязка")
            actor = f"{actor} (от имени: {role} {binding or ''})".strip()

        if not role:
            raise HTTPException(
                status_code=403,
                detail="Роль не назначена — обратитесь к администратору",
            )
        return role, binding, actor

    def _history(payload: AskRequest, user) -> list[dict]:
        """Прошлые ходы того же диалога — только своего: чужой диалог даёт 404."""
        if payload.dialogId is None:
            return []
        user_id = getattr(user, "id", None)
        if user_id is None:
            return []
        try:
            turns = dialog_store.messages(int(payload.dialogId), int(user_id))
        except dialog_store.NotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        history = []
        for turn in turns[-8:]:
            answer = turn.get("answer") or {}
            history.append({"question": turn.get("question", ""), "answer": answer,
                            "frame": answer.get("frame") or {}})
        return history

    def _depth(payload: AskRequest) -> str:
        depth = (payload.depth or "auto").strip().lower()
        if depth not in pipeline.DEPTHS:
            raise HTTPException(status_code=400, detail="depth: auto, fast, analyze или deep")
        return depth

    def _persist(response: AskResponse, answer, payload: AskRequest, user) -> AskResponse:
        """Положить вопрос и ответ в диалог владельца."""
        user_id = getattr(user, "id", None)
        if user_id is None:
            return response
        dialog_id = payload.dialogId
        try:
            if dialog_id is None:
                dialog_id = dialog_store.create_dialog(int(user_id))["id"]
            stored = dialog_store.append_message(
                int(dialog_id), int(user_id), answer.question,
                response.model_dump(), answer.journal_id,
            )
        except dialog_store.NotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        response.dialogId = stored["dialogId"]
        response.messageId = stored["id"]
        response.dialogTitle = stored["title"]
        return response

    @router.post("/ask", response_model=AskResponse)
    def ai_ask(payload: AskRequest, user=Depends(guard)):
        role, binding, actor = _identity(payload, user)
        depth = _depth(payload)
        history = _history(payload, user)
        try:
            answer = pipeline.ask(
                payload.question, role, binding, actor,
                model=(payload.model or "").strip() or None,
                depth=depth, history=history,
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err

        response = _response(answer, _may_see_sql(user))
        return _persist(response, answer, payload, user)

    @router.post("/ask/stream")
    def ai_ask_stream(payload: AskRequest, user=Depends(guard)):
        """Тот же ответ, но этапы уходят на клиент по мере прохождения.

        Проверка прав и роли делается до начала потока: отказ должен приходить
        обычным кодом ответа, а не первым событием внутри уже открытого потока.
        """
        role, binding, actor = _identity(payload, user)
        show_sql = _may_see_sql(user)
        model = (payload.model or "").strip() or None
        depth = _depth(payload)
        history = _history(payload, user)

        events: queue.Queue = queue.Queue()
        FINISHED = object()

        def worker() -> None:
            try:
                answer = pipeline.ask(
                    payload.question, role, binding, actor, model=model,
                    on_stage=lambda event: events.put(("stage", event)),
                    depth=depth, history=history,
                )
                response = _response(answer, show_sql)
                events.put(("answer", _persist(response, answer, payload, user).model_dump()))
            except HTTPException as err:
                events.put(("failed", {"detail": err.detail}))
            except Exception as err:  # noqa: BLE001 - до клиента должна дойти причина
                events.put(("failed", {"detail": str(err) or "Не удалось получить ответ"}))
            finally:
                events.put((FINISHED, None))

        threading.Thread(target=worker, name="ai-ask-stream", daemon=True).start()

        def frames():
            while True:
                kind, data = events.get()
                if kind is FINISHED:
                    break
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                # Nginx иначе копит ответ в буфере, и все этапы приезжают разом.
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/dialogs")
    def ai_dialogs(user=Depends(guard)):
        return {"dialogs": dialog_store.list_dialogs(int(user.id))}

    @router.post("/dialogs")
    def ai_dialog_create(payload: DialogRequest, user=Depends(guard)):
        return dialog_store.create_dialog(int(user.id), payload.title)

    @router.patch("/dialogs/{dialog_id}")
    def ai_dialog_rename(dialog_id: int, payload: DialogRequest, user=Depends(guard)):
        try:
            return dialog_store.rename_dialog(dialog_id, int(user.id), payload.title)
        except dialog_store.Invalid as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        except dialog_store.NotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @router.post("/dialogs/{dialog_id}/pin")
    def ai_dialog_pin(dialog_id: int, payload: PinRequest, user=Depends(guard)):
        try:
            return dialog_store.set_pinned(dialog_id, int(user.id), payload.pinned)
        except dialog_store.NotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @router.delete("/dialogs/{dialog_id}")
    def ai_dialog_delete(dialog_id: int, user=Depends(guard)):
        try:
            dialog_store.delete_dialog(dialog_id, int(user.id))
        except dialog_store.NotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        return {"deleted": dialog_id}

    @router.get("/dialogs/{dialog_id}/messages")
    def ai_dialog_messages(dialog_id: int, user=Depends(guard)):
        try:
            return {"messages": dialog_store.messages(dialog_id, int(user.id))}
        except dialog_store.NotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @router.post("/feedback")
    def ai_feedback(payload: FeedbackRequest, user=Depends(guard)):
        try:
            return dialog_store.save_feedback(
                payload.messageId, int(user.id), payload.rating, payload.comment
            )
        except dialog_store.Invalid as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        except dialog_store.NotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @router.get("/quality")
    def ai_quality(days: int = 30, limit: int = 200, rating: int | None = None,
                   role: str = "", verdict: str = "", status: str = "",
                   promptVersion: str = "", model: str = "",
                   _admin=Depends(require_admin)):
        """Раздел «Качество ответов»: сводка, лента, срез по версиям.

        Переписки здесь нет — только метаданные, оценка, комментарий и текст
        запроса к витрине. Это модель приватности ИБ-4 (БТ-КК8).
        """
        return {
            "summary": quality.summary(days),
            "entries": quality.entries(
                days=days, rating=rating, role=role, verdict=verdict,
                status=status, prompt_version=promptVersion, model=model,
                limit=limit,
            ),
            "versions": quality.versions(),
            "statuses": [
                {"code": code, "title": quality.STATUS_TITLES[code]}
                for code in quality.STATUSES
            ],
            "promptVersion": contract.prompt_version(),
        }

    @router.post("/quality/{message_id}/review")
    def ai_quality_review(message_id: int, payload: ReviewRequest,
                          admin=Depends(require_admin)):
        try:
            return quality.set_review(
                message_id,
                status=payload.status,
                owner=payload.owner if payload.owner is not None else getattr(admin, "name", None),
                note=payload.note,
                in_golden=payload.inGolden,
            )
        except quality.Invalid as err:
            raise HTTPException(status_code=400, detail=str(err)) from err

    @router.get("/quality/export")
    def ai_quality_export(days: int = 30, _admin=Depends(require_admin)):
        """Выгрузка сводки и ленты оценок в Excel (БТ-КК9)."""
        blob = quality_export.build(days)
        stamp = time.strftime("%Y-%m-%d")
        return Response(
            content=blob,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition":
                    f'attachment; filename="ai-quality-{stamp}.xlsx"',
            },
        )

    @router.get("/journal")
    def ai_journal(limit: int = 50, _admin=Depends(require_admin)):
        return {"entries": journal.recent(max(1, min(limit, 200)))}

    return router
