"""Оркестратор: разбор задачи → цикл инструментов → структурированный ответ.

Последовательность действий выбирает модель; система ограничивает её
инструментами, валидатором, семантическим слоем и бюджетом глубины.
Ни один тип вопроса не зашит как фиксированная цепочка запросов.
"""
from __future__ import annotations

import copy
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
from . import claims, file_tools, grounding, memory, planning, prompts, recommend, schema_tools, tools
from .. import textstyle
from .llm import MAX_TOKENS, NUM_CTX, ModelUnavailable, OllamaChat, Reply, extract_json, salvage_json
from .state import DEPTHS, TASK_TYPES, AgentOutcome, Analysis, Budget, Plan, Step, Workspace

MAX_SECONDS = float(os.environ.get("AI_AGENT_MAX_SECONDS", "240"))
# Итог в JSON: рекомендации по шаблону длиннее прежних строк.
FINAL_TOKENS = int(os.environ.get("AI_AGENT_FINAL_TOKENS", "1500"))
MAX_CALLS_PER_TURN = 3
MAX_NUDGES = 1
TOOL_TEXT_LIMIT = int(os.environ.get("AI_AGENT_TOOL_TEXT", "3500"))
EVIDENCE_ROWS = 15

# Контекст модели и кэш Ollama (25.09.2026). Ollama не пересчитывает начало
# переписки, если оно не изменилось с прошлого хода, — читает только новое.
# Прежде каждый ход сжимался ещё один старый результат в середине переписки,
# и модель на каждом шаге перечитывала контекст с этого места (≈10 тыс. токенов
# на шаг). Теперь переписка только дописывается, а старые результаты сжимаются
# разом, когда она подходит к пределу контекста: кэш сбрасывается один раз.
CHARS_PER_TOKEN = float(os.environ.get("AI_CHARS_PER_TOKEN", "2.5"))   # с запасом: цифры — по токену
TRIM_SHARE = float(os.environ.get("AI_CONTEXT_TRIM_SHARE", "0.75"))    # доля контекста до сжатия
KEEP_RECENT_RESULTS = 2
BRIEF_MARK = " (подробности выше по ходу)"
# 28.09.2026: каждое сжатие заставляет Ollama перечитать переписку (в журнале — 16–36 с на
# 15–17 тыс. токенов). Раньше после сжатия переписка оставалась у самого предела и сжималась
# почти на каждом ходе (6 раз за ответ). Теперь сжимается до TRIM_TARGET_SHARE контекста —
# следующее сжатие нескоро; знаков в токене — по факту прошлого хода (prompt_eval_count).
TRIM_TARGET_SHARE = float(os.environ.get("AI_CONTEXT_TRIM_TARGET", "0.5"))
ARG_KEEP_CHARS = 200
# Повторы (28.09.2026): модель повторяла один и тот же расчёт до 11 раз подряд (≈4 мин).
# Тот же вызов с теми же аргументами не выполняется; после MAX_REPEATS повторов сбор
# заканчивается и итог пишется по собранным данным. Ошибку можно повторить один раз.
MAX_REPEATS = int(os.environ.get("AI_AGENT_MAX_REPEATS", "2"))
REPEAT_IGNORED_ARGS = ("subtask", "purpose")

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
           depth: str = "auto", plan_fields: bool = False) -> Plan:
    """План анализа: эвристика для очевидных фактов, иначе одна модель-подсказка."""
    question = " ".join((question or "").split())
    followup = bool(history) and memory.is_followup(question)
    if depth == "fast" and not followup:
        return Plan(standalone_question=question, task_type="lookup", depth="fast", source="forced")
    if depth == "auto" and not followup and memory.looks_like_lookup(question):
        return Plan(standalone_question=question, task_type="lookup", depth="fast", source="heuristic",
                    reason="простой факт-вопрос без контекста")

    started = time.monotonic()
    # ИИ-23: в режиме планирования тот же разбор даёт и поля карточки плана.
    system = prompts.TRIAGE_SYSTEM + (planning.PLAN_FIELDS if plan_fields else "")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompts.triage_user(
            question, today, semantic, memory.history_block(history or []), data_range, hits)},
    ]
    plan = Plan(standalone_question=question, task_type="other", depth="analyze", source="fallback")
    try:
        reply: Reply = model.chat(messages, json_mode=True, max_tokens=1200 if plan_fields else 700)
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
        if plan_fields:
            plan.extra = {key: parsed.get(key) for key in
                          ("goal", "expected_result", "subtasks", "risks", "success_criteria") if parsed.get(key)}
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


def context_chars(messages: list[dict], tool_specs: list[dict] | None = None) -> int:
    """Размер переписки с моделью в знаках — вместе с описанием инструментов."""
    size = len(json.dumps(tool_specs, ensure_ascii=False)) if tool_specs else 0
    for message in messages:
        size += len(message.get("content") or "")
        if message.get("tool_calls"):
            size += len(json.dumps(message["tool_calls"], ensure_ascii=False, default=str))
    return size


def context_limit_chars(reply_tokens: int | None = None, chars_per_token: float | None = None) -> int:
    """Сколько знаков переписки помещается в контекст с запасом на ответ хода.

    `chars_per_token` — сколько знаков в токене у этой переписки по факту прошлого хода;
    без замера — CHARS_PER_TOKEN с запасом (цифры и JSON — почти по токену на знак).
    """
    reply = reply_tokens or MAX_TOKENS
    ratio = chars_per_token or CHARS_PER_TOKEN
    return max(4000, int((NUM_CTX * TRIM_SHARE - reply) * ratio))


def context_target_chars(limit_chars: int) -> int:
    """До скольких знаков сжимать переписку: с запасом, чтобы следующее сжатие было нескоро."""
    return int(limit_chars * TRIM_TARGET_SHARE / TRIM_SHARE)


def _prompt_tokens(reply: Reply) -> int:
    """Размер контекста хода в токенах, как его посчитала Ollama (0 — неизвестно)."""
    try:
        return int(((reply.raw or {}).get("prompt_eval_count")) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _brief(content: str) -> str:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content[:300] + "…"
    if not isinstance(data, dict):
        return content[:300] + "…"
    brief = {k: data[k] for k in ("id", "columns", "row_count", "error") if k in data}
    return json.dumps(brief, ensure_ascii=False) + BRIEF_MARK


def _brief_results(messages: list[dict], keep_recent: int) -> bool:
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    old = tool_indexes[:-keep_recent] if keep_recent else tool_indexes
    changed = False
    for index in old:
        content = messages[index].get("content") or ""
        if len(content) <= 300 or content.endswith(BRIEF_MARK):
            continue
        brief = _brief(content)
        if brief != content:
            messages[index]["content"] = brief
            changed = True
    return changed


def _brief_arguments(messages: list[dict], keep_recent: int = 2) -> bool:
    """Длинные аргументы старых вызовов (код Python, SQL) — укоротить: результат уже в переписке."""
    assistant = [m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]
    changed = False
    for message in assistant[:-keep_recent] if keep_recent else assistant:
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            args = function.get("arguments") if isinstance(function, dict) else None
            if not isinstance(args, dict):
                continue
            for name, value in list(args.items()):
                if isinstance(value, str) and len(value) > ARG_KEEP_CHARS * 2 and not value.endswith(BRIEF_MARK):
                    args[name] = value[:ARG_KEEP_CHARS] + "…" + BRIEF_MARK
                    changed = True
    return changed


def _trim(messages: list[dict], *, limit_chars: int, tool_specs: list[dict] | None = None,
          keep_recent: int = KEEP_RECENT_RESULTS, target_chars: int | None = None) -> bool:
    """Сжать переписку — только когда она подходит к пределу, и сразу с запасом.

    Сначала старые результаты инструментов (кроме `keep_recent` последних); если переписка
    всё ещё больше `target_chars` — все результаты, кроме последнего, затем длинные
    аргументы старых вызовов. Начало переписки меняется одним разом: каждое изменение
    заставляет Ollama перечитать контекст с этого места. True — переписка изменилась.
    """
    if context_chars(messages, tool_specs) <= limit_chars:
        return False
    target = limit_chars if target_chars is None else min(target_chars, limit_chars)
    changed = _brief_results(messages, keep_recent)
    if keep_recent > 1 and context_chars(messages, tool_specs) > target:
        changed = _brief_results(messages, 1) or changed
    if context_chars(messages, tool_specs) > target:
        changed = _brief_arguments(messages) or changed
    return changed


def _call_key(name: str, arguments) -> str | None:
    """Отпечаток вызова инструмента: тот же инструмент с теми же аргументами — тот же результат."""
    if name == "finish":
        return None
    args = arguments
    if isinstance(args, str):
        args = salvage_json(args) or {"_": args}
    if not isinstance(args, dict):
        args = {"_": args}
    clean = {}
    for key, value in args.items():
        if key in REPEAT_IGNORED_ARGS:
            continue
        clean[key] = " ".join(value.split()).rstrip(";").strip() if isinstance(value, str) else value
    return name + json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)


def _repeat_reply(seen: dict) -> dict:
    """Ответ модели на повтор: результат уже есть — чем воспользоваться и что делать дальше."""
    what = "Этот вызов с теми же аргументами уже выполнен"
    if seen.get("error"):
        what += f" и дважды вернул ошибку: {str(seen['error'])[:300]}. Исправь запрос или выбери другой шаг"
    else:
        ids = ", ".join(seen.get("ids") or [])
        what += (f": результат {ids} уже в переписке" if ids else ": его вывод уже в переписке выше")
        what += ". Повтор даст то же самое — используй готовый результат, сделай следующий шаг плана"
    return {"error": "повтор", "message": what + " или вызови finish, если данных достаточно."}


def run(question: str, scope: Scope, *, plan: Plan, history: list[dict] | None = None,
        model=None, on_stage: StageListener = None,
        generate_sql: Callable[[str], str] | None = None,
        run_query: Callable[[str, int], executor.Result] | None = None,
        catalog: Catalog | None = None, semantic: Semantic | None = None,
        today: str | None = None, scope_label: str = "", data_range: dict | None = None,
        hits: dict | None = None, limits=None,
        should_stop: Callable[[], bool] | None = None,
        files: list[dict] | None = None, plan_card: dict | None = None) -> AgentOutcome:
    """Прогон агента в глубине analyze/deep. Режим fast обслуживает pipeline.

    `limits` — пределы роли (backend/ai/limits.py); у администратора их нет.
    `should_stop` — человек остановил вопрос: сбор прерывается перед следующим
    ходом модели, итог не пишется (ИИ-03).
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
    # ИИ-07 / ИИ-11: таблицы файлов — наборы fN, текст — части tN для find_in_files и read_file.
    if files:
        file_tools.register(ctx, files)
    if data_range is None:
        data_range = schema_tools.data_range(ctx)
    if hits is None:
        hits = semantic.find(plan.standalone_question or question)

    outcome = AgentOutcome(ok=False, plan=plan, analysis=Analysis(), workspace=workspace,
                           model=getattr(model, "model", None))
    # Рекомендации — только по просьбе или когда без них ответ неполон (решение владельца 23.09.2026).
    asked = recommend.requested(question, plan.standalone_question)
    started = time.monotonic()
    # Инструменты файлов — только когда файлы есть: иначе лишние токены в каждом ходе.
    tool_specs = [spec.as_ollama() for spec in tools.specs()
                  if files or spec.name not in file_tools.TOOL_NAMES] + [prompts.FINISH_TOOL]
    live = copy.deepcopy(plan_card) if plan_card else None
    if live:
        tool_specs = _plan_specs(tool_specs)
    reply_tokens = getattr(model, "max_tokens", None)
    chars_per_token: float | None = None      # по факту хода: знаков переписки на токен Ollama
    done_calls: dict[str, dict] = {}          # отпечаток вызова → результат (id, ошибка, сколько раз)
    repeats = 0
    messages = [
        {"role": "system", "content": prompts.agent_system(semantic, catalog.dialect)},
        {"role": "user", "content": prompts.agent_user(
            plan.standalone_question or question, plan.as_dict(), scope_label or scope.label, today,
            data_range, hits, budget.remaining(), asked=asked,
            plan_block=planning.prompt_block(live) if live else "")},
    ]

    finish_payload: dict | None = None
    nudges = 0
    stop_reason = ""
    think_step = Step(key="model", kind="plan", label="Выбираю следующий шаг")

    try:
        while True:
            if should_stop is not None and should_stop():
                stop_reason = "cancelled"
                break
            if budget.exhausted():
                stop_reason = "лимит ходов модели"
                break
            if max_seconds and time.monotonic() - started > max_seconds:
                stop_reason = "лимит времени"
                break
            _emit(on_stage, {"key": "model", "state": "active", "label": think_step.label, "kind": "plan"})
            sent_chars = context_chars(messages, tool_specs)
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
            prompt_tokens = _prompt_tokens(reply)
            if prompt_tokens > 0 and sent_chars > 0:
                chars_per_token = min(6.0, max(1.5, sent_chars / prompt_tokens))
            if reply.tool_calls:
                messages.append(_assistant_message(reply))
                for call in reply.tool_calls[:MAX_CALLS_PER_TURN]:
                    if call.name == "finish":
                        args = call.arguments
                        if isinstance(args, str):
                            args = salvage_json(args) or {}
                        finish_payload = args if isinstance(args, dict) else {}
                        break
                    key = _call_key(call.name, call.arguments)
                    seen = done_calls.get(key) if key else None
                    if seen is not None and not (seen.get("error") and seen["count"] < 2):
                        # Повтор: не выполняем — отвечаем, где готовый результат (28.09.2026).
                        repeats += 1
                        outcome.repeats += 1
                        messages.append({"role": "tool", "tool_name": call.name,
                                         "content": json.dumps(_repeat_reply(seen), ensure_ascii=False)})
                        if repeats >= MAX_REPEATS:
                            break
                        continue
                    explicit = None
                    if live and isinstance(call.arguments, dict):
                        explicit = planning.resolve(live, call.arguments.get("subtask"))
                        if explicit:
                            _plan_event(on_stage, live, explicit, "active")
                    before = len(workspace.steps)
                    result = tools.call(ctx, call.name, call.arguments)
                    if key:
                        new_ids = [s.result_id for s in workspace.steps[before:] if s.result_id]
                        error = result.get("error") if isinstance(result, dict) else None
                        done_calls[key] = {"count": (seen or {}).get("count", 0) + 1, "ids": new_ids,
                                           "error": error}
                    if live:
                        _plan_steps(on_stage, live, workspace.steps[before:], explicit)
                    messages.append({
                        "role": "tool", "tool_name": call.name,
                        "content": tools.compact(result, TOOL_TEXT_LIMIT),
                    })
                if finish_payload is not None:
                    stop_reason = "finish"
                    break
                if repeats >= MAX_REPEATS:
                    stop_reason = "повтор шагов"
                    break
                limit_chars = context_limit_chars(reply_tokens, chars_per_token)
                if _trim(messages, limit_chars=limit_chars, tool_specs=tool_specs,
                         target_chars=context_target_chars(limit_chars)):
                    outcome.context_trims += 1
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

    outcome.sql_ms = ctx.sql_ms
    outcome.stop_reason = stop_reason
    outcome.tool_calls = budget.used_sql + budget.used_python + budget.used_charts + budget.used_schema
    if stop_reason == "cancelled":
        outcome.error = "Запрос остановлен"
        outcome.rule = "cancelled"
        _emit(on_stage, {"key": "model", "state": "failed", "label": "Запрос остановлен", "kind": "plan"})
        return outcome
    _emit(on_stage, {"key": "model", "state": "done", "label": "Шаги анализа выполнены",
                     "kind": "plan", "ms": outcome.model_ms})

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
    if stop_reason == "лимит времени" and max_seconds and max_seconds > 0:
        analysis.limitations.append(
            f"Анализ остановлен: лимит времени уровня — {_minutes(max_seconds)}; выводы — по собранным данным.")
    elif stop_reason == "повтор шагов":
        analysis.limitations.append(
            "Сбор данных закончен досрочно: ИИ повторял уже сделанный шаг; выводы — по собранным данным.")
    elif stop_reason and stop_reason not in ("finish", "finish-text", "prose"):
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
    if live:
        payload = finish_payload or {}
        raw = payload.get("deviations") if isinstance(payload.get("deviations"), list) else []
        outcome.deviations = [d for d in raw if isinstance(d, dict)][:10]
    has_data = any(rs.rows for rs in workspace.results.values())
    outcome.ok = bool(analysis.headline) and (has_data or bool(finish_payload) or bool(analysis.limitations))
    if not outcome.ok:
        outcome.error = "Агент не получил данных для ответа"
        outcome.rule = "agent_no_data"
    main = outcome.main_result
    outcome.frame.update(memory.build_frame(question, plan, analysis, main.sql if main else None))
    return outcome


def _plan_specs(tool_specs: list[dict]) -> list[dict]:
    """ИИ-23: в режиме плана инструменты принимают номер пункта, а finish — отклонения от плана."""
    out = []
    for spec in tool_specs:
        spec = copy.deepcopy(spec)
        function = spec.get("function") or {}
        props = function.get("parameters", {}).setdefault("properties", {})
        if function.get("name") == "finish":
            props["deviations"] = {
                "type": "array", "description": "пункты плана, которые не выполнены или выполнены иначе, с причиной",
                "items": {"type": "object", "properties": {
                    "subtask": {"type": "string", "description": "номер пункта"},
                    "reason": {"type": "string", "description": "почему, до 20 слов"}}}}
        elif function.get("name") in PLAN_TOOLS:
            props["subtask"] = {"type": "string", "description": "номер пункта утверждённого плана"}
        out.append(spec)
    return out


PLAN_TOOLS = ("run_sql", "run_python", "create_chart", "read_file", "find_in_files")


def _plan_event(on_stage: StageListener, card: dict, sid: str, state: str) -> None:
    subtask = next((s for s in card["subtasks"] if s["id"] == sid), None)
    if subtask is None:
        return
    _emit(on_stage, {"key": f"plan-{sid}", "state": state, "kind": "subtask", "subtask": sid,
                     "label": subtask["title"]})


def _plan_steps(on_stage: StageListener, card: dict, steps: list[Step], explicit: str | None) -> None:
    """Привязать новые шаги агента к пунктам плана и отметить прогресс."""
    for step in steps:
        sid = planning.attribute(card, step.kind, explicit)
        if not sid:
            continue
        step.subtask = sid
        subtask = next(s for s in card["subtasks"] if s["id"] == sid)
        if step.ok:
            subtask["status"] = "done"
            _plan_event(on_stage, card, sid, "done")
        elif subtask.get("status") != "done":
            subtask["status"] = "active"


def _minutes(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds % 60 == 0:
        return f"{seconds // 60} мин"
    return f"{seconds} с"


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
