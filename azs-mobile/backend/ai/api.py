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
from urllib.parse import quote
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from . import contract
from . import limits as ai_limits
from . import dialogs as dialog_store
from . import executor, generator, journal, pipeline, quality, quality_export
from .. import roles as role_model
from .scope import REFERENCE_DB, UNRESTRICTED_ROLES
from .scope import from_user as scope_from_user
from . import dwh_scope
from . import examples as ai_examples
from . import export as ai_export_mod
from . import quotas
from . import limit_settings
from . import retention
from .catalog import CATALOG


def demo_enabled() -> bool:
    return (os.environ.get("AI_DEMO_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


class AskRequest(BaseModel):
    # Предел по роли (ИИ-03: 2000, у администратора 4000) проверяет quotas.check_question;
    # здесь — общий потолок, чтобы не принимать мегабайты.
    question: str = Field(min_length=1, max_length=quotas.MAX_QUESTION_CHARS)
    role: str = Field(default="admin", max_length=40)
    binding: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=120)
    dialogId: int | None = None
    # auto — глубину выбирает разбор задачи; fast/analyze/deep — принудительно.
    depth: str = Field(default="auto", max_length=10)


class LimitChange(BaseModel):
    group: str = Field(max_length=20)
    param: str = Field(max_length=40)
    value: Any = None


class LimitsSave(BaseModel):
    changes: list[LimitChange] = Field(default_factory=list, max_length=200)


class DialogRequest(BaseModel):
    title: str = Field(default="", max_length=dialog_store.TITLE_LIMIT)


class PinRequest(BaseModel):
    pinned: bool = True


class MoveRequest(BaseModel):
    folderId: int | None = None


class ArchiveRequest(BaseModel):
    archived: bool = True


class ArchiveOldestRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=200)


class FolderRequest(BaseModel):
    title: str = Field(default="", max_length=dialog_store.FOLDER_TITLE_LIMIT)


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
    totalMs: int = 0
    depthRequested: str = "auto"
    # Счётчики лимитов после ответа (ИИ-03): остаток «Высокого» на сегодня.
    quota: dict | None = None


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
    # Роли «от имени» ещё считаются по ОХД — интерфейс заберёт их из /api/ai/identities.
    identitiesPending: bool = False
    mayImpersonate: bool = False
    ownRole: str = ""
    ownRoleTitle: str = ""
    ownScopeLabel: str = ""
    maySeeSql: bool = False
    commentRequiredUpTo: int = dialog_store.COMMENT_REQUIRED_UPTO
    ownName: str = ""
    ownEmail: str = ""
    # Приветствие под орбом (ИИ-08): «Спросите о 42 объектах…» или «по всей сети».
    ownStations: int = 0
    ownUnrestricted: bool = False
    # Примеры вопросов по роли (ИИ-08); по всем ролям — только тому, кто спрашивает «от имени».
    examples: list[dict] = []
    examplesByRole: dict[str, list[dict]] = {}
    agentEnabled: bool = False
    depths: list[dict] = []
    # Лимиты человека (ИИ-03): квота «Высокого», одновременные вопросы, длина вопроса.
    limits: dict = {}


# Уровни глубины (ИИ-20, 23.09.2026). Коды прежние — fast, analyze, deep:
# их хранят журнал, диалоги и API. Меняются только подписи для людей.
# «limit» — ориентир, пока в журнале мало ответов этого уровня.
DEPTH_OPTIONS = [
    {"code": "auto", "title": "Авто",
     "hint": "уровень выбирает ИИ по вопросу",
     "about": "Факт — «Лёгкий», сравнение и динамика — «Средний», причины и сценарии — «Высокий». "
              "Выбранный уровень виден в ответе.",
     "limit": ""},
    {"code": "fast", "title": "Лёгкий",
     "hint": "одна цифра или факт",
     "about": "Один запрос к витрине и короткий ответ, без графиков. Например: «Выручка НТУ за август».",
     "limit": "обычно быстрее всего"},
    {"code": "analyze", "title": "Средний",
     "hint": "сравнения и динамика",
     "about": "Несколько запросов, расчёты и до двух графиков. Например: «Сравни конверсию по ОНПО с прошлым годом».",
     "limit": ""},
    {"code": "deep", "title": "Высокий",
     "hint": "причины, отклонения, сценарии",
     "about": "Серия запросов, расчёты и до трёх графиков — дольше всего. Например: «Почему упала выручка НТУ на 58-123».",
     "limit": ""},
]
TIMING_MIN_SAMPLES = 5


def _seconds_label(ms: int) -> str:
    seconds = max(1, round(ms / 1000))
    if seconds < 60:
        return f"{seconds} с"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} мин {rest} с" if rest else f"{minutes} мин"


def depth_options(unlimited: bool = False, role: str | None = None, usage: dict | None = None) -> list[dict]:
    """Уровни с ориентиром времени: медиана из журнала за неделю, иначе предел.

    `unlimited` — у роли нет пределов (администратор): вместо «до 4 мин» — «без ограничения времени».
    `role` — чей предел времени показывать (ИИ-03: «Средний» 2 мин, «Высокий» 4 мин).
    `usage` — квота «Высокого» (quotas.usage): остаток на сегодня; при нуле уровень серый с причиной.
    """
    try:
        timings = journal.depth_timings(days=7)
    except Exception:  # noqa: BLE001 - без журнала ориентир берётся из пределов
        timings = {}
    out = []
    for option in DEPTH_OPTIONS:
        item = dict(option)
        if option["code"] in ("analyze", "deep") and not item["limit"]:
            item["limit"] = f"до {_seconds_label(int(quotas.value(role, 'seconds_' + option['code'])) * 1000)}"
        stat = timings.get(option["code"])
        if stat and stat["count"] >= TIMING_MIN_SAMPLES:
            item["typical"] = f"обычно около {_seconds_label(stat['median_ms'])}"
            item["samples"] = stat["count"]
        else:
            item["typical"] = item["limit"]
            item["samples"] = stat["count"] if stat else 0
        if unlimited and option["code"] in ("analyze", "deep") and not (stat and stat["count"] >= TIMING_MIN_SAMPLES):
            item["typical"] = "без ограничения времени"
        quota = (usage or {}).get("deep") if option["code"] == "deep" else None
        if quota:
            item["quota"] = quota
            if quota["left"] <= 0:
                item["disabled"] = True
                # Текст для плашки «Лимит: …» в меню уровней (src/note.jsx).
                item["reason"] = (f"{quota['limit']} из {quota['limit']} на сегодня. "
                                  f"Счётчик обнулится в {usage.get('resets', '00:00 МСК')}.")
            else:
                item["quotaNote"] = f"осталось {quota['left']} из {quota['limit']} на сегодня"
        out.append(item)
    return out


def _strip_code(steps: list[dict]) -> list[dict]:
    """Шаги без SQL и кода — для ролей, которым текст запроса не показывается."""
    return [{k: v for k, v in step.items() if k not in {"sql", "code", "output"}} for step in steps]


def _public_grounding(grounding: dict | None, show_sql: bool) -> dict | None:
    """Снятые рекомендации (ИИ-16): текст — только тем, кому виден SQL; остальным — причины."""
    if not grounding or show_sql or not isinstance(grounding.get("claims"), dict):
        return grounding
    claims = dict(grounding["claims"])
    claims["withheld"] = [{"reason": item.get("reason", "")} for item in claims.get("withheld") or []]
    return {**grounding, "claims": claims}


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
        # В рамке ответа есть начало SQL (для памяти диалога) — уходит только тем, кому SQL виден.
        frame={k: v for k, v in (getattr(answer, "frame", {}) or {}).items() if show_sql or k != "sql"},
        plan=getattr(answer, "plan", None),
        grounding=_public_grounding(getattr(answer, "grounding", None), show_sql),
        planMs=int(getattr(answer, "plan_ms", 0) or 0),
        totalMs=int(getattr(answer, "total_ms", 0) or 0),
        depthRequested=getattr(answer, "depth_requested", "auto") or "auto",
    )


ROLE_TITLES = {
    "admin": "Администратор — вся сеть",
    "aup_npo": "АУП общества",
    "regional_manager": "Руководитель управления",
    "territory_manager": "Территориальный менеджер",
}

# Просмотр от чужого имени доступен только администратору: рядовой
# пользователь всегда спрашивает от себя, роль берётся из учётной записи.
def _own_scope(user) -> tuple[bool, int]:
    """Вся ли сеть у пользователя и сколько в его области объектов — для приветствия (ИИ-08).

    Считается там же, где область для ответов: на витрине ОХД — по её справочникам.
    Приветствие не должно ломать экран: неизвестная роль или недоступная витрина — (False, 0).
    """
    try:
        if dwh_scope.enabled():
            # Не ждать витрину: область из кэша, иначе считается в фоне, а приветствие — без числа.
            scope = dwh_scope.cached_build(getattr(user, "role", "") or "", getattr(user, "roleBinding", "") or "")
            if scope is None:
                return False, 0
        else:
            scope = scope_from_user(user)
    except Exception:  # noqa: BLE001 - без числа приветствие просто короче
        return False, 0
    return bool(scope.unrestricted), len(scope.ksss)


def _may_impersonate(user) -> bool:
    return bool(getattr(user, "isAdmin", False))


# Текст запроса показывается администратору и субадминистратору; остальным
# ролям происхождение числа объясняет блок «Откуда число» (решение владельца
# от 21.09.2026, уточняет БТ-П3).
SQL_ROLES = {"admin", "subadmin"}
# Сбой модели или витрины — не вина человека: «Высокий» не засчитывается (ИИ-03).
SYSTEM_FAILURES = {"model_unavailable", "execution"}


def _may_see_sql(user) -> bool:
    return bool(getattr(user, "isAdmin", False)) or (getattr(user, "role", "") or "") in SQL_ROLES


def _identities(limit_per_role: int = 12, wait: bool = True) -> tuple[list[Identity], bool]:
    """Роли для «от имени» и признак «ещё считаются» (витрина ОХД отвечает в фоне)."""
    items = [Identity(role="admin", roleTitle=ROLE_TITLES["admin"], binding=None, stations=0)]
    if dwh_scope.enabled():
        # ИИ живёт в ДВХ: «от имени» предлагаем тех, кто есть в справочниках ОХД.
        # Статус раздела их не ждёт: при выключенном VPN три запроса к ОХД занимали 30 с,
        # и всё это время раздел «ИИ-аналитик» не появлялся в меню.
        found = dwh_scope.identities(limit_per_role, wait=wait)
        if found is None:
            return items, True
        items.extend(
            Identity(role=role, roleTitle=ROLE_TITLES[role], binding=binding, stations=count)
            for role, binding, count in found
        )
        return items, False
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
    return items, False


def _alert_text(alert: dict) -> tuple[str, str]:
    day = ".".join(reversed(str(alert.get("day") or "").split("-")))
    who = alert.get("actor") or "пользователь"
    subject = f"ИИ-аналитик: {who} — больше {alert.get('threshold')} вопросов за день"
    text = (f"{day} пользователь {who} задал {alert.get('questions')} вопросов ИИ-аналитику при пороге "
            f"{alert.get('threshold')} (решение №12).\n\nПодробности — «Админ» → «Качество ИИ» → «Нагрузка за 7 дней». "
            "Порог и получателя можно поменять на вкладке «Лимиты ИИ».")
    return subject, text


def build_router(require_admin: Callable, require_user: Callable | None = None,
                 notify: Callable[..., None] | None = None) -> APIRouter:
    """`notify(subject, text, to)` — служебное письмо (main.py); без него алерты видны только в «Качестве ИИ»."""
    router = APIRouter(prefix="/api/ai", tags=["ai"])
    gate = require_user or require_admin
    if notify is not None:
        def _sink(alert: dict) -> None:
            subject, text = _alert_text(alert)
            notify(subject, text, str(quotas.general("alert_recipient") or ""))

        quotas.alert_sink = _sink

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
        own_unrestricted, own_stations = _own_scope(user)
        identities, pending = _identities(wait=False) if impersonate else ([], False)
        return StatusResponse(
            enabled=True,
            modelAvailable=generator.available(),
            model=generator.MODEL,
            installedModels=models,
            backend=executor.BACKEND,
            identities=identities,
            identitiesPending=pending,
            mayImpersonate=impersonate,
            ownRole=getattr(user, "role", "") or "",
            ownRoleTitle=getattr(user, "roleTitle", "") or "",
            ownScopeLabel=getattr(user, "scopeLabel", "") or "",
            maySeeSql=_may_see_sql(user),
            ownName=getattr(user, "name", "") or "",
            ownEmail=getattr(user, "email", "") or "",
            ownStations=own_stations,
            ownUnrestricted=own_unrestricted,
            examples=ai_examples.for_role(getattr(user, "role", "")),
            examplesByRole=ai_examples.by_role() if impersonate else {},
            agentEnabled=pipeline.AGENT_ENABLED,
            depths=_depths_for(user) if pipeline.AGENT_ENABLED else [],
            limits=_usage(user),
        )

    @router.get("/identities")
    def ai_identities(user=Depends(guard)):
        """Роли для «от имени» — отдельно от статуса: может ждать витрину ОХД (до таймаута подключения)."""
        if not _may_impersonate(user):
            raise HTTPException(status_code=403, detail="Выбор роли «от имени» — только администратору")
        items, _pending = _identities(wait=True)
        return {"identities": [item.model_dump() for item in items]}

    def _own_role(user) -> str:
        """Роль человека для квот: у администратора — admin, даже когда он спрашивает «от имени»."""
        if getattr(user, "isAdmin", False) and not (getattr(user, "role", "") or "").strip():
            return "admin"
        return (getattr(user, "role", "") or "").strip()

    def _usage(user) -> dict:
        try:
            return quotas.usage(user, _own_role(user))
        except Exception:  # noqa: BLE001 - счётчики не должны ломать раздел
            return {}

    def _depths_for(user) -> list[dict]:
        role = _own_role(user)
        return depth_options(ai_limits.for_role(role).unlimited, role=role, usage=_usage(user))

    @router.get("/limits")
    def ai_limits_view(user=Depends(guard)):
        """Счётчики для поля вопроса — обновляются после каждого ответа."""
        return {"limits": _usage(user),
                "depths": _depths_for(user) if pipeline.AGENT_ENABLED else []}

    @router.post("/runs/{run_id}/cancel")
    def ai_run_cancel(run_id: int, user=Depends(guard)):
        """Остановить свой идущий вопрос (ИИ-03). Чужой или законченный — 404."""
        if not quotas.RUNS.cancel(run_id, user):
            raise HTTPException(status_code=404, detail="Вопрос уже закончен или не найден")
        return {"cancelled": run_id}

    def _admit(payload: AskRequest, user, depth: str, actor: str | None) -> "quotas.Run":
        """Принять вопрос по лимитам человека: длина, два одновременных, квота «Высокого»."""
        role = _own_role(user)
        try:
            quotas.RUNS.check_question(user, role, payload.question, depth, actor)
            return quotas.RUNS.start(user, role, depth, actor)
        except quotas.Refused as err:
            headers = {"X-AI-Limit": err.reason}
            if err.suggest:
                headers["X-AI-Suggest-Depth"] = err.suggest
            code = 400 if err.reason == "question_chars" else 429
            raise HTTPException(status_code=code, detail=err.message, headers=headers) from err

    def _close(run: "quotas.Run", answer=None, failed: bool = False) -> None:
        """Записать исход запуска. Сбой модели или витрины «Высокий» не засчитывает."""
        if answer is None or failed:
            quotas.RUNS.finish(run, "failed", refund=True)
            return
        rule = getattr(answer, "rule", None)
        if rule == "cancelled":
            status_ = "cancelled"
        elif answer.ok:
            status_ = "ok"
        else:
            status_ = "failed" if rule in SYSTEM_FAILURES else "refused"
        quotas.RUNS.finish(run, status_, depth=getattr(answer, "depth", None),
                           journal_id=getattr(answer, "journal_id", None), refund=rule in SYSTEM_FAILURES)

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
        # Память диалога — по роли (ИИ-02): сколько прошлых ходов видит модель.
        memory = int(quotas.value(_own_role(user), "dialog_memory"))
        for turn in turns[-memory:]:
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
                # Лимит активных проверен до вопроса (_can_start); готовый ответ не теряем.
                dialog_id = dialog_store.create_dialog(int(user_id), role=_own_role(user), check=False)["id"]
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
        _can_start(payload, user)
        run = _admit(payload, user, depth, actor)
        answer = None
        try:
            answer = pipeline.ask(
                payload.question, role, binding, actor,
                model=(payload.model or "").strip() or None,
                depth=depth, history=history, control=quotas.Control(run),
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        finally:
            _close(run, answer)

        response = _response(answer, _may_see_sql(user))
        response.quota = _usage(user)
        if getattr(answer, "rule", None) == "cancelled":
            return response
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
        # Лимиты проверяются до потока: отказ приходит обычным кодом 429 / 409 с причиной.
        _can_start(payload, user)
        run = _admit(payload, user, depth, actor)

        events: queue.Queue = queue.Queue()
        FINISHED = object()
        # Номер запуска — первым событием: по нему интерфейс может остановить вопрос.
        events.put(("run", {"runId": run.id}))

        def worker() -> None:
            answer = None
            try:
                answer = pipeline.ask(
                    payload.question, role, binding, actor, model=model,
                    on_stage=lambda event: events.put(("stage", event)),
                    depth=depth, history=history, control=quotas.Control(run),
                )
                _close(run, answer)
                if getattr(answer, "rule", None) == "cancelled":
                    # Остановленный вопрос в диалог не пишется: человек от него отказался.
                    events.put(("failed", {"detail": "Запрос остановлен", "cancelled": True}))
                    return
                response = _response(answer, show_sql)
                response.quota = _usage(user)
                events.put(("answer", _persist(response, answer, payload, user).model_dump()))
            except HTTPException as err:
                events.put(("failed", {"detail": err.detail}))
            except Exception as err:  # noqa: BLE001 - до клиента должна дойти причина
                events.put(("failed", {"detail": str(err) or "Не удалось получить ответ"}))
            finally:
                if answer is None:
                    _close(run, None, failed=True)
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

    def _limit(err: "dialog_store.Limit") -> HTTPException:
        headers = {"X-AI-Limit": err.param}
        if err.action:
            headers["X-AI-Action"] = err.action
        return HTTPException(status_code=409, detail=err.message, headers=headers)

    def _dialog_call(fn, *args, **kwargs):
        """Операции с диалогами и папками: чужое — 404, неверное — 400, лимит роли — 409."""
        try:
            return fn(*args, **kwargs)
        except dialog_store.Limit as err:
            raise _limit(err) from err
        except dialog_store.Invalid as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        except dialog_store.NotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    def _can_start(payload: AskRequest, user) -> None:
        """Первый вопрос открывает новый диалог — лимит активных проверяется до модели (ИИ-02)."""
        if payload.dialogId is None and getattr(user, "id", None) is not None:
            _dialog_call(dialog_store.can_start, int(user.id), _own_role(user))

    @router.get("/dialogs")
    def ai_dialogs(user=Depends(guard)):
        role = _own_role(user)
        dialogs = dialog_store.list_dialogs(int(user.id), role=role)
        active = sum(1 for d in dialogs if not d["archived"])
        return {
            "dialogs": dialogs,
            "folders": dialog_store.list_folders(int(user.id)),
            "limits": {
                "activeDialogs": quotas.value(role, "active_dialogs"),
                "active": active,
                "pinnedDialogs": int(quotas.value(role, "pinned_dialogs")),
                "folders": int(quotas.value(role, "folders")),
                "historyDays": int(quotas.value(role, "history_days")),
                "warnDays": dialog_store.WARN_DAYS,
            },
        }

    @router.post("/dialogs")
    def ai_dialog_create(payload: DialogRequest, user=Depends(guard)):
        return _dialog_call(dialog_store.create_dialog, int(user.id), payload.title, role=_own_role(user))

    @router.post("/dialogs/archive-oldest")
    def ai_dialogs_archive_oldest(payload: ArchiveOldestRequest, user=Depends(guard)):
        return {"archived": dialog_store.archive_oldest(int(user.id), payload.count)}

    @router.post("/dialogs/{dialog_id}/move")
    def ai_dialog_move(dialog_id: int, payload: MoveRequest, user=Depends(guard)):
        return _dialog_call(dialog_store.move_dialog, dialog_id, int(user.id), payload.folderId)

    @router.post("/dialogs/{dialog_id}/archive")
    def ai_dialog_archive(dialog_id: int, payload: ArchiveRequest, user=Depends(guard)):
        return _dialog_call(dialog_store.set_archived, dialog_id, int(user.id), payload.archived,
                            role=_own_role(user))

    @router.post("/folders")
    def ai_folder_create(payload: FolderRequest, user=Depends(guard)):
        return _dialog_call(dialog_store.create_folder, int(user.id), payload.title, role=_own_role(user))

    @router.patch("/folders/{folder_id}")
    def ai_folder_rename(folder_id: int, payload: FolderRequest, user=Depends(guard)):
        return _dialog_call(dialog_store.rename_folder, folder_id, int(user.id), payload.title)

    @router.delete("/folders/{folder_id}")
    def ai_folder_delete(folder_id: int, dialogs: str = "move", user=Depends(guard)):
        return _dialog_call(dialog_store.delete_folder, folder_id, int(user.id), dialogs)

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
        return _dialog_call(dialog_store.set_pinned, dialog_id, int(user.id), payload.pinned, role=_own_role(user))

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
            # Нагрузка за неделю (ИИ-03, решение №12): алерты и упоры в лимиты.
            "load": _load_report(),
        }

    # --- «Лимиты ИИ» (ИИ-26): только администратор -----------------------------
    def _limits_state() -> dict:
        state = limit_settings.state(7)
        try:
            state["storage"] = retention.storage_report()
        except Exception:  # noqa: BLE001 - отчёт о хранении не должен ломать вкладку
            state["storage"] = None
        return state

    @router.get("/admin/limits")
    def ai_limits_settings(_admin=Depends(require_admin)):
        return _limits_state()

    @router.put("/admin/limits")
    def ai_limits_save(payload: LimitsSave, admin=Depends(require_admin)):
        try:
            changed = limit_settings.save([c.model_dump() for c in payload.changes], getattr(admin, "email", None))
        except limit_settings.Invalid as err:
            raise HTTPException(status_code=400, detail="Не сохранено: " + "; ".join(err.errors)) from err
        return {"changed": changed, **_limits_state()}

    @router.post("/admin/limits/reset")
    def ai_limits_reset(admin=Depends(require_admin)):
        return {"reset": limit_settings.reset(getattr(admin, "email", None)), **_limits_state()}

    def _load_report() -> dict:
        try:
            return quotas.load_report(7)
        except Exception:  # noqa: BLE001 - сводка нагрузки не должна ломать раздел
            return {}

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

    @router.get("/messages/{message_id}/export")
    def ai_export(message_id: int, part: str = "main", format: str = "xlsx", user=Depends(guard)):
        """ИИ-12: таблица или данные графика из своего ответа — XLSX или CSV с паспортом."""
        fmt = (format or "").strip().lower()
        if fmt not in ai_export_mod.FORMATS:
            raise HTTPException(status_code=400, detail="format: xlsx или csv")
        user_id = getattr(user, "id", None)
        if user_id is None:
            raise HTTPException(status_code=404, detail="Ответ не найден")
        try:
            message = dialog_store.message(int(message_id), int(user_id))
            table = ai_export_mod.part_of(message["answer"], (part or "main").strip())
        except dialog_store.NotFound as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        except ai_export_mod.NotExportable as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        show_sql = _may_see_sql(user)
        journal_row = message["journal"]
        role_code = journal_row.get("role") or getattr(user, "role", "") or ""
        spec = role_model.ROLES.get(role_code)
        passport = ai_export_mod.passport(
            message=message, table=table, journal_row=journal_row, user=user, show_sql=show_sql,
            source_label=("Витрина ОХД (DWH ЛИКАРД)" if executor.BACKEND == "postgres"
                          else "Демонстрационный стенд (SQLite)"),
            dialect=getattr(CATALOG, "dialect", "sqlite") or "sqlite",
            catalog_text=ai_export_mod.catalog_version(CATALOG),
            role_title=spec.title if spec else (getattr(user, "roleTitle", "") or role_code or "—"),
        )
        if not show_sql:
            table = dict(table, sql="")
        blob = (ai_export_mod.build_xlsx(table, passport) if fmt == "xlsx"
                else ai_export_mod.build_csv(table, passport))
        dialog_store.record_export(message["id"], int(user_id), message["journalId"], part, fmt, len(table["rows"]))
        name = ai_export_mod.file_name(message["id"], table["title"], fmt)
        ascii_name = f"ai-answer-{message['id']}.{fmt}"
        return Response(content=blob, media_type=ai_export_mod.FORMATS[fmt], headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store",
        })

    @router.get("/journal")
    def ai_journal(limit: int = 50, _admin=Depends(require_admin)):
        return {"entries": journal.recent(max(1, min(limit, 200)))}

    return router
