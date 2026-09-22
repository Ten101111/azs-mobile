"""Оркестратор: разбор задачи → цикл инструментов → структурированный ответ.

Последовательность действий выбирает модель; система ограничивает её
инструментами, валидатором, семантическим слоем и бюджетом глубины.
Ни один тип вопроса не зашит как фиксированная цепочка запросов.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from typing import Any, Callable

from .. import executor
from ..catalog import CATALOG, Catalog
from ..semantic import SEMANTIC, Semantic
from ..validator import Scope
from . import grounding, memory, prompts, schema_tools, tools
from .llm import ModelUnavailable, OllamaChat, Reply, extract_json
from .state import DEPTHS, TASK_TYPES, AgentOutcome, Analysis, Budget, Plan, Step, Workspace

MAX_SECONDS = float(os.environ.get("AI_AGENT_MAX_SECONDS", "240"))
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
        return [str(v).strip() for v in value if str(v).strip()]
    return Analysis(
        headline=str(payload.get("headline") or "").strip(),
        happened=as_list(payload.get("happened")),
        why=as_list(payload.get("why")),
        where=as_list(payload.get("where")),
        actions=as_list(payload.get("actions")),
        limitations=as_list(payload.get("limitations")),
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
        hits: dict | None = None) -> AgentOutcome:
    """Прогон агента в глубине analyze/deep. Режим fast обслуживает pipeline."""
    catalog = catalog or CATALOG
    semantic = semantic or SEMANTIC
    model = model or OllamaChat()
    today = today or date.today().isoformat()
    budget = Budget.for_depth(plan.depth)
    workspace = Workspace()
    ctx = tools.ToolContext(
        question=plan.standalone_question or question, scope=scope, budget=budget, workspace=workspace,
        catalog=catalog, semantic=semantic, generate_sql=generate_sql,
        run_query=run_query or executor.run,
        on_step=lambda step, state: _emit(on_stage, _stage_from_step(step, state)),
    )
    if data_range is None:
        data_range = schema_tools.data_range(ctx)
    if hits is None:
        hits = semantic.find(plan.standalone_question or question)

    outcome = AgentOutcome(ok=False, plan=plan, analysis=Analysis(), workspace=workspace,
                           model=getattr(model, "model", None))
    started = time.monotonic()
    tool_specs = [spec.as_ollama() for spec in tools.specs()] + [prompts.FINISH_TOOL]
    messages = [
        {"role": "system", "content": prompts.agent_system(semantic, catalog.dialect)},
        {"role": "user", "content": prompts.agent_user(
            plan.standalone_question or question, plan.as_dict(), scope_label or scope.label, today,
            data_range, hits, budget.remaining())},
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
            if time.monotonic() - started > MAX_SECONDS:
                stop_reason = "лимит времени"
                break
            _emit(on_stage, {"key": "model", "state": "active", "label": think_step.label, "kind": "plan"})
            reply = model.chat(messages, tools=tool_specs)
            budget.used_turns += 1
            outcome.model_ms += reply.elapsed_ms
            outcome.turns += 1
            if reply.tool_calls:
                messages.append(_assistant_message(reply))
                for call in reply.tool_calls[:MAX_CALLS_PER_TURN]:
                    if call.name == "finish":
                        finish_payload = call.arguments if isinstance(call.arguments, dict) else {}
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
            parsed = extract_json(reply.text)
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
        analysis, main_result = _finalize(model, outcome, plan, evidence, notes, stop_reason)
    if main_result:
        outcome.frame["mainResult"] = str(main_result)

    report = grounding.check(analysis, workspace)
    if report["unverified"]:
        repaired = _repair(model, outcome, analysis, plan, evidence, notes, report["unverified"])
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


def _finalize(model, outcome: AgentOutcome, plan: Plan, evidence: str, notes: list[str],
              stop_reason: str) -> tuple[Analysis, str | None]:
    messages = [
        {"role": "system", "content": prompts.FINALIZE_SYSTEM},
        {"role": "user", "content": prompts.finalize_user(
            plan.standalone_question, plan.as_dict(), evidence or "Результатов нет: ни один запрос не вернул данных.", notes)},
    ]
    try:
        reply = model.chat(messages, json_mode=True, max_tokens=1200)
    except ModelUnavailable as err:
        outcome.error = str(err)
        outcome.rule = "model_unavailable"
        return Analysis(), None
    outcome.model_ms += reply.elapsed_ms
    parsed = extract_json(reply.text)
    if isinstance(parsed, dict):
        return _analysis_from(parsed), parsed.get("main_result")
    text = reply.text.strip()
    return (Analysis(headline=text[:400]) if text else Analysis()), None


def _repair(model, outcome: AgentOutcome, analysis: Analysis, plan: Plan, evidence: str,
            notes: list[str], unverified: list[str]) -> Analysis | None:
    messages = [
        {"role": "system", "content": prompts.FINALIZE_SYSTEM},
        {"role": "user", "content": prompts.finalize_user(plan.standalone_question, plan.as_dict(), evidence, notes)},
        {"role": "assistant", "content": json.dumps(analysis.as_dict(), ensure_ascii=False)},
        {"role": "user", "content": prompts.REPAIR_USER.format(numbers=", ".join(unverified[:12]))},
    ]
    try:
        reply = model.chat(messages, json_mode=True, max_tokens=1200)
    except ModelUnavailable:
        return None
    outcome.model_ms += reply.elapsed_ms
    parsed = extract_json(reply.text)
    return _analysis_from(parsed) if isinstance(parsed, dict) else None
