"""Оркестратор: разбор задачи → цикл инструментов → структурированный ответ.

Последовательность действий выбирает модель; система ограничивает её
инструментами, валидатором, семантическим слоем и бюджетом глубины.
Ни один тип вопроса не зашит как фиксированная цепочка запросов.
"""
from __future__ import annotations

import json
import re
import os
import time
from datetime import date
from typing import Any, Callable

from .. import executor
from ..catalog import CATALOG, Catalog
from ..semantic import SEMANTIC, Semantic
from ..validator import Scope
from . import claims, grounding, memory, prompts, recommend, schema_tools, tools
from .. import textstyle
from .llm import ModelUnavailable, OllamaChat, Reply, extract_json, salvage_json
from .state import DEPTHS, TASK_TYPES, AgentOutcome, Analysis, Budget, Plan, Step, Workspace

MAX_SECONDS = float(os.environ.get("AI_AGENT_MAX_SECONDS", "240"))
# Итог в JSON: рекомендации по шаблону длиннее прежних строк.
FINAL_TOKENS = int(os.environ.get("AI_AGENT_FINAL_TOKENS", "1500"))
MAX_CALLS_PER_TURN = 3
MAX_NUDGES = 1
TOOL_TEXT_LIMIT = int(os.environ.get("AI_AGENT_TOOL_TEXT", "3500"))
EVIDENCE_ROWS = 15

StageListener = Callable[[dict], None] | None


def _emit(on_stage: StageListener, event: dict) -> None:
    if on_stage is None:
        return
    try:
        on_stage(event)
    except Exception:  # noqa: BLE001 - слушатель не роняет ответ
        pass


def _stage_from_step(step: Step, state: str) -> dict:
    event = {"key": step.key, "state": state, "label": step.label, "kind": step.kind}
    if step.ms:
        event["ms"] = step.ms
    return event


# --- разбор задачи -------------------------------------------------------------

def triage(question: str, *, history: list[dict] | None, semantic: Semantic, model,
           today: str, data_range: dict | None, hits: dict | None,
           depth: str = "auto") -> Plan:
    """План анализа: эвристика для очевидных фактов, иначе одна модель-подсказка."""
    question = " ".join((question or "").split())
    followup = bool(history) and memory.is_followup(question)
    if depth == "fast" and not followup:
        return Plan(standalone_question=question, task_type="lookup", depth="fast", source="forced")
    if depth == "auto" and not followup and memory.looks_like_lookup(question):
        return Plan(standalone_question=question, task_type="lookup", depth="fast", source="heuristic",
                    reason="простой факт-вопрос без контекста")

    started = time.monotonic()
    messages = [
        {"role": "system", "content": prompts.TRIAGE_SYSTEM},
        {"role": "user", "content": prompts.triage_user(
            question, today, semantic, memory.history_block(history or []), data_range, hits)},
    ]
    plan = Plan(standalone_question=question, task_type="other", depth="analyze", source="fallback")
    try:
        reply: Reply = model.chat(messages, json_mode=True, max_tokens=700)
        parsed = extract_json(reply.text) or {}
        plan.elapsed_ms = reply.elapsed_ms
    except ModelUnavailable:
        raise
    except Exception:  # noqa: BLE001 - разбор не должен ронять ответ
        parsed = {}
    if isinstance(parsed, dict) and parsed:
        plan.source = "model"
        plan.standalone_question = str(parsed.get("standalone_question") or question).strip() or question
        task = str(parsed.get("task_type") or "other").strip().lower()
        plan.task_type = task if task in TASK_TYPES else "other"
        chosen = str(parsed.get("depth") or "").strip().lower()
        plan.depth = chosen if chosen in DEPTHS else ("fast" if plan.task_type == "lookup" else "analyze")
        plan.steps = [str(s) for s in (parsed.get("steps") or []) if str(s).strip()][:8]
        plan.metrics = [str(m) for m in (parsed.get("metrics") or []) if str(m).strip()][:8]
        plan.period = str(parsed.get("period") or "")
        plan.filters = [str(f) for f in (parsed.get("filters") or []) if str(f).strip()][:8]
        plan.missing = [str(m) for m in (parsed.get("missing") or []) if str(m).strip()][:6]
        plan.reason = str(parsed.get("reason") or "")
    if plan.task_type == "lookup" and plan.depth != "fast" and depth == "auto":
        plan.depth = "fast"
    if plan.task_type in {"diagnose", "anomaly", "opportunity", "whatif"} and plan.depth == "fast":
        plan.depth = "analyze"
    if depth in ("analyze", "deep"):
        plan.depth = depth
        plan.source = "forced" if plan.source != "model" else plan.source
    elif depth == "fast":
        plan.depth = "fast"
    if hits and hits.get("absent"):
        for item in hits["absent"]:
            if item not in plan.missing:
                plan.missing.append(item)
    return plan


# --- цикл инструментов ---------------------------------------------------------

def _analysis_from(payload: dict) -> Analysis:
    def as_list(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        return [_text(v) for v in value if _text(v)]

    def _text(value) -> str:
        if isinstance(value, dict):
            value = value.get("text") or value.get("hypothesis") or value.get("action") or ""
        return " ".join(str(value).split())

    # «Почему» — гипотезы: текст и что их подтвердит (ИИ-25).
    why, checks = [], {}
    raw_why = payload.get("why") or []
    for item in ([raw_why] if isinstance(raw_why, (str, dict)) else raw_why):
        text = _text(item)
        if not text:
            continue
        why.append(text)
        if isinstance(item, dict) and item.get("check"):
            checks[text] = " ".join(str(item["check"]).split())[:300]
    # «Что можно сделать» — рекомендации с полями шаблона (ИИ-16).
    raw_actions = payload.get("actions") or []
    if isinstance(raw_actions, (str, dict)):
        raw_actions = [raw_actions]
    recs = [recommend.normalize(item) for item in raw_actions]
    recs = [rec for rec in recs if rec["action"]]
    return Analysis(
        headline=_text(payload.get("headline") or ""),
        happened=as_list(payload.get("happened")),
        why=why,
        where=as_list(payload.get("where")),
        actions=[rec["action"] for rec in recs],
        limitations=as_list(payload.get("limitations")),
        checks=checks,
        recs=recs,
    )


def _evidence_text(workspace: Workspace) -> str:
    chunks = []
    for rs in workspace.results.values():
        head = f"{rs.id} — {rs.purpose or rs.source} ({rs.row_count} строк{', обрезано' if rs.truncated else ''})"
        if rs.warnings:
            head += "; предупреждения: " + "; ".join(rs.warnings)
        rows = [" | ".join("—" if v is None else str(v) for v in row) for row in rs.rows[:EVIDENCE_ROWS]]
        table = " | ".join(rs.columns) + "\n" + "\n".join(rows)
        if rs.row_count > EVIDENCE_ROWS:
            table += f"\n… ещё {rs.row_count - EVIDENCE_ROWS} строк"
        chunks.append(head + "\n" + table)
    for step in workspace.steps:
        if step.kind == "python" and step.ok and step.output:
            chunks.append(f"Вывод вычисления «{step.purpose or step.label}»:\n{step.output[:1500]}")
    text = "\n\n".join(chunks)
    return text[:9000] + ("\n… (сокращено)" if len(text) > 9000 else "")


def _assistant_message(reply: Reply) -> dict:
    if reply.raw and isinstance(reply.raw.get("message"), dict):
        message = dict(reply.raw["message"])
        message.pop("thinking", None)
        return message
    message: dict[str, Any] = {"role": "assistant", "content": reply.content or ""}
    if reply.tool_calls:
        message["tool_calls"] = [
            {"function": {"name": c.name, "arguments": c.arguments}} for c in reply.tool_calls
        ]
    return message


def _trim(messages: list[dict], keep_recent: int = 6) -> None:
    """Старые результаты инструментов сжимаются до одной строки, чтобы контекст не рос без конца."""
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for index in tool_indexes[:-keep_recent]:
        content = messages[index].get("content") or ""
        if len(content) > 300:
            try:
                data = json.loads(content)
                brief = {k: data[k] for k in ("id", "columns", "row_count", "error") if k in data}
                messages[index]["content"] = json.dumps(brief, ensure_ascii=False) + " (подробности выше по ходу)"
            except json.JSONDecodeError:
                messages[index]["content"] = content[:300] + "…"


def run(question: str, scope: Scope, *, plan: Plan, history: list[dict] | None = None,
        model=None, on_stage: StageListener = None,
        generate_sql: Callable[[str], str] | None = None,
        run_query: Callable[[str, int], executor.Result] | None = None,
        catalog: Catalog | None = None, semantic: Semantic | None = None,
        today: str | None = None, scope_label: str = "", data_range: dict | None = None,
        hits: dict | None = None, limits=None) -> AgentOutcome:
    """Прогон агента в глубине analyze/deep. Режим fast обслуживает pipeline.

    `limits` — пределы роли (backend/ai/limits.py); у администратора их нет.
    """
    catalog = catalog or CATALOG
    semantic = semantic or SEMANTIC
    model = model or OllamaChat()
    today = today or date.today().isoformat()
    budget = Budget.for_depth(plan.depth, limits)
    max_seconds = MAX_SECONDS if limits is None or limits.max_seconds is None else limits.max_seconds
    final_tokens = (limits.final_tokens if limits is not None else None) or FINAL_TOKENS
    workspace = Workspace()
    ctx = tools.ToolContext(
        question=plan.standalone_question or question, scope=scope, budget=budget, workspace=workspace,
        catalog=catalog, semantic=semantic, generate_sql=generate_sql,
        run_query=run_query or executor.run,
        on_step=lambda step, state: _emit(on_stage, _stage_from_step(step, state)),
        python_timeout_s=limits.python_timeout_s if limits is not None else None,
    )
    if data_range is None:
        data_range = schema_tools.data_range(ctx)
    if hits is None:
        hits = semantic.find(plan.standalone_question or question)

    outcome = AgentOutcome(ok=False, plan=plan, analysis=Analysis(), workspace=workspace,
                           model=getattr(model, "model", None))
    # Рекомендации — только по просьбе или когда без них ответ неполон (решение владельца 23.09.2026).
    asked = recommend.requested(question, plan.standalone_question)
    started = time.monotonic()
    tool_specs = [spec.as_ollama() for spec in tools.specs()] + [prompts.FINISH_TOOL]
    messages = [
        {"role": "system", "content": prompts.agent_system(semantic, catalog.dialect)},
        {"role": "user", "content": prompts.agent_user(
            plan.standalone_question or question, plan.as_dict(), scope_label or scope.label, today,
            data_range, hits, budget.remaining(), asked=asked)},
    ]

    finish_payload: dict | None = None
    nudges = 0
    stop_reason = ""
    think_step = Step(key="model", kind="plan", label="Выбираю следующий шаг")

    try:
        while True:
            if budget.exhausted():
                stop_reason = "лимит ходов модели"
                break
            if max_seconds and time.monotonic() - started > max_seconds:
                stop_reason = "лимит времени"
                break
            _emit(on_stage, {"key": "model", "state": "active", "label": think_step.label, "kind": "plan"})
            try:
                reply = model.chat(messages, tools=tool_specs)
            except ModelUnavailable:
                # Ход модели сорвался (например, Ollama не разобрала обрезанный вызов
                # инструмента и ответила 500). Если данные уже собраны — итог пишется
                # отдельным шагом по ним, а не теряется весь ответ.
                if any(rs.rows for rs in workspace.results.values()):
                    stop_reason = "сбой хода модели"
                    break
                raise
            budget.used_turns += 1
            outcome.model_ms += reply.elapsed_ms
            outcome.turns += 1
            if reply.tool_calls:
                messages.append(_assistant_message(reply))
                for call in reply.tool_calls[:MAX_CALLS_PER_TURN]:
                    if call.name == "finish":
                        args = call.arguments
                        if isinstance(args, str):
                            args = salvage_json(args) or {}
                        finish_payload = args if isinstance(args, dict) else {}
                        break
                    result = tools.call(ctx, call.name, call.arguments)
                    messages.append({
                        "role": "tool", "tool_name": call.name,
                        "content": tools.compact(result, TOOL_TEXT_LIMIT),
                    })
                if finish_payload is not None:
                    stop_reason = "finish"
                    break
                _trim(messages)
                continue
            # Нет вызовов: модель либо уже отвечает текстом, либо застряла.
            parsed = salvage_json(reply.text)
            if isinstance(parsed, dict) and ("headline" in parsed or "happened" in parsed):
                finish_payload = parsed
                stop_reason = "finish-text"
                break
            nudges += 1
            messages.append({"role": "assistant", "content": reply.text[:1500]})
            if nudges > MAX_NUDGES:
                # Модель отвечает прозой: итог всё равно формулируется отдельным
                # шагом по собранным данным — это не сбой, а конец сбора.
                stop_reason = "prose"
                break
            messages.append({"role": "user", "content": (
                "Продолжай через инструменты: вызови run_sql для следующего шага плана "
                "или заверши анализ вызовом finish с полями headline, happened, why, where, actions, limitations."
            )})
    except ModelUnavailable as err:
        outcome.error = str(err)
        outcome.rule = "model_unavailable"
        _emit(on_stage, {"key": "model", "state": "failed", "label": "Модель недоступна", "kind": "plan"})
        return outcome

    _emit(on_stage, {"key": "model", "state": "done", "label": "Шаги анализа выполнены",
                     "kind": "plan", "ms": outcome.model_ms})
    outcome.sql_ms = ctx.sql_ms

    # --- финальный ответ -------------------------------------------------
    write_step = Step(key="write", kind="write", label="Формулирую ответ")
    _emit(on_stage, _stage_from_step(write_step, "active"))
    write_started = time.monotonic()
    analysis = _analysis_from(finish_payload) if finish_payload else Analysis()
    evidence = _evidence_text(workspace)
    notes = [w for rs in workspace.results.values() for w in rs.warnings]
    main_result = (finish_payload or {}).get("main_result")
    if analysis.empty() or not analysis.headline:
        analysis, main_result = _finalize(model, outcome, plan, evidence, notes, stop_reason, asked, final_tokens)
    if main_result:
        outcome.frame["mainResult"] = str(main_result)

    report = grounding.check(analysis, workspace)
    if report["unverified"]:
        repaired = _repair(model, outcome, analysis, plan, evidence, notes, report["unverified"], asked, final_tokens)
        if repaired is not None:
            analysis = repaired
            report = grounding.check(analysis, workspace)
    removed: list[str] = []
    if report["unverified"]:
        analysis, removed = grounding.strip_unverified(analysis, workspace)
    outcome.grounding = {"checked": report["checked"], "unverified": report["unverified"], "removed": removed}

    for item in plan.missing:
        text = item.split(":")[0].strip()
        if text and not any(text.lower() in lim.lower() for lim in analysis.limitations):
            analysis.limitations.append(f"В витрине нет данных: {item}.")
    if stop_reason and stop_reason not in ("finish", "finish-text", "prose"):
        analysis.limitations.append(f"Анализ остановлен ({stop_reason}); выводы по собранным данным.")
    # Типы утверждений и правила рекомендаций ставит код, а не модель (ИИ-25, ИИ-16).
    outcome.grounding["claims"] = claims.annotate(analysis, workspace, asked=asked)
    # Единый стиль текста: русские названия, без технических скобок, заголовок всегда есть.
    _polish(analysis, semantic, catalog)

    write_step.ms = int((time.monotonic() - write_started) * 1000)
    write_step.label = "Сформулировал ответ"
    workspace.steps.append(write_step)
    _emit(on_stage, _stage_from_step(write_step, "done"))

    outcome.analysis = analysis
    has_data = any(rs.rows for rs in workspace.results.values())
    outcome.ok = bool(analysis.headline) and (has_data or bool(finish_payload) or bool(analysis.limitations))
    if not outcome.ok:
        outcome.error = "Агент не получил данных для ответа"
        outcome.rule = "agent_no_data"
    main = outcome.main_result
    outcome.frame.update(memory.build_frame(question, plan, analysis, main.sql if main else None))
    return outcome


def _polish(analysis: Analysis, semantic, catalog) -> None:
    def clean(text: str) -> str:
        return textstyle.clean(text, semantic, catalog)

    fallback = analysis.happened[0] if analysis.happened else "Ответ сформирован по данным ниже."
    analysis.headline = textstyle.headline(analysis.headline, fallback, semantic, catalog)
    for name in ("happened", "why", "where", "limitations"):
        setattr(analysis, name, [clean(item) for item in getattr(analysis, name)])
    for rec in analysis.recommendations or []:
        for field_name in ("action", "basis", "effect", "limits"):
            if rec.get(field_name):
                rec[field_name] = clean(rec[field_name])
    if analysis.recommendations is not None:
        analysis.actions = [rec["action"] for rec in analysis.recommendations]
    for marks in (analysis.claims or {}).values():
        for claim in marks:
            if claim.get("check"):
                claim["check"] = clean(claim["check"])


def _finalize(model, outcome: AgentOutcome, plan: Plan, evidence: str, notes: list[str],
              stop_reason: str, asked: bool = False, final_tokens: int = 0) -> tuple[Analysis, str | None]:
    messages = [
        {"role": "system", "content": prompts.FINALIZE_SYSTEM},
        {"role": "user", "content": prompts.finalize_user(
            plan.standalone_question, plan.as_dict(), evidence or "Результатов нет: ни один запрос не вернул данных.", notes,
            asked=asked)},
    ]
    try:
        reply = model.chat(messages, json_mode=True, max_tokens=final_tokens or FINAL_TOKENS)
    except ModelUnavailable as err:
        outcome.error = str(err)
        outcome.rule = "model_unavailable"
        return Analysis(), None
    outcome.model_ms += reply.elapsed_ms
    parsed = salvage_json(reply.text)
    if isinstance(parsed, dict):
        return _analysis_from(parsed), parsed.get("main_result")
    return _analysis_from_prose(reply.text), None


HEADLINE_RE = re.compile(r'"headline"\s*:\s*"((?:[^"\\]|\\.)*)')


def _analysis_from_prose(text: str) -> Analysis:
    """Модель ответила не JSON: заголовок — первая фраза, пункты — следующие.

    Сырой JSON на экран не попадает никогда: если ответ похож на JSON, но не
    разбирается, из него достаётся только заголовок.
    """
    text = (text or "").strip()
    if not text:
        return Analysis()
    if textstyle.looks_like_json(text):
        match = HEADLINE_RE.search(text)
        headline = match.group(1).replace('\\"', '"') if match else ""
        return Analysis(headline=headline)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    return Analysis(headline=sentences[0] if sentences else "", happened=sentences[1:6])


def _repair(model, outcome: AgentOutcome, analysis: Analysis, plan: Plan, evidence: str,
            notes: list[str], unverified: list[str], asked: bool = False, final_tokens: int = 0) -> Analysis | None:
    messages = [
        {"role": "system", "content": prompts.FINALIZE_SYSTEM},
        {"role": "user", "content": prompts.finalize_user(plan.standalone_question, plan.as_dict(), evidence, notes,
                                                          asked=asked)},
        {"role": "assistant", "content": json.dumps(analysis.model_view(), ensure_ascii=False)},
        {"role": "user", "content": prompts.REPAIR_USER.format(numbers=", ".join(unverified[:12]))},
    ]
    try:
        reply = model.chat(messages, json_mode=True, max_tokens=final_tokens or FINAL_TOKENS)
    except ModelUnavailable:
        return None
    outcome.model_ms += reply.elapsed_ms
    parsed = salvage_json(reply.text)
    return _analysis_from(parsed) if isinstance(parsed, dict) else None
