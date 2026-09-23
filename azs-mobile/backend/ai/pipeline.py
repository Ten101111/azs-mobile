"""Контур «вопрос → ответ»: быстрый путь и аналитический агент.

Быстрый путь (FAST) — прежний порядок без изменений: модель предлагает SQL,
валидатор решает, исполнитель выполняет, модель поясняет результат. При
отказе валидатора делается одна попытка перегенерации с текстом замечания.

Остальные глубины (ANALYZE, DEEP) обслуживает агент из `backend.ai.agent`:
разбор задачи, план, серия инструментов (SQL через тот же валидатор, Python
в песочнице, графики), структурированный ответ. Глубину в режиме `auto`
выбирает разбор задачи; простой факт-вопрос уходит на быстрый путь без
единого лишнего обращения к модели.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from . import contract, executor, generator, journal, people as people_resolver, scope as scope_builder
from .validator import Rejected, Scope, validate

MAX_ATTEMPTS = 2
AGENT_ENABLED = (os.environ.get("AI_AGENT_ENABLED") or "1").strip().lower() in {"1", "true", "yes", "on"}
DEPTHS = ("auto", "fast", "analyze", "deep")
# Фабрика модели-оркестратора: подменяется в тестах и сценарном прогоне.
# None — OllamaChat из backend.ai.agent.llm.
MODEL_FACTORY: Callable[[str | None], Any] | None = None

# Этапы конвейера. Названия даются в настоящем времени, пока этап идёт,
# и в прошедшем, когда он закончен: интерфейс показывает и то и другое.
STAGES = {
    "plan": ("Разбираю задачу", "Разобрал задачу"),
    "draft": ("Составляю запрос к витрине", "Составил запрос к витрине"),
    "check": ("Проверяю допустимость", "Проверил допустимость"),
    "read": ("Читаю витрину", "Прочитал витрину"),
    "write": ("Формулирую ответ", "Сформулировал ответ"),
}

DEPTH_TITLES = {"fast": "быстрый ответ", "analyze": "анализ", "deep": "глубокий анализ"}
TASK_TITLES = {
    "lookup": "факт", "compare": "сравнение", "trend": "динамика", "diagnose": "диагностика причин",
    "anomaly": "поиск аномалий", "opportunity": "точки роста", "whatif": "сценарий", "other": "разбор",
}


def _notify(on_stage: Callable[[dict], None] | None, key: str, state: str,
            ms: int | None = None, note: str | None = None, kind: str | None = None) -> None:
    """Сообщить слушателю об этапе.

    Слушатель — это интерфейс, а не часть контура: если он сломается,
    ответ всё равно должен досчитаться, поэтому исключение гасится.
    """
    if on_stage is None:
        return
    running, finished = STAGES.get(key, (key, key))
    event = {
        "key": key,
        "state": state,
        "label": note or (running if state == "active" else finished),
    }
    if ms is not None:
        event["ms"] = int(ms)
    if kind:
        event["kind"] = kind
    try:
        on_stage(event)
    except Exception:  # noqa: BLE001 - слушатель не должен ронять ответ
        pass


@dataclass
class Answer:
    ok: bool
    question: str
    scope_label: str
    sql: str | None = None
    sql_raw: str | None = None
    summary: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    truncated: bool = False
    model: str | None = None
    model_ms: int = 0
    narrate_ms: int = 0
    sql_ms: int = 0
    row_count: int = 0
    attempts: int = 0
    error: str | None = None
    rule: str | None = None
    journal_id: int | None = None
    # Агент: глубина, тип задачи, структурированный вывод, графики, таблицы, шаги.
    depth: str = "fast"
    task_type: str = "lookup"
    analysis: dict | None = None
    charts: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)   # с sql/code — фильтруется в api по роли
    frame: dict = field(default_factory=dict)
    plan: dict | None = None
    grounding: dict | None = None
    plan_ms: int = 0


def ask(question: str, role: str, binding: str | None, actor: str | None = None,
        model: str | None = None,
        on_stage: Callable[[dict], None] | None = None,
        depth: str = "auto", history: list[dict] | None = None) -> Answer:
    question = (question or "").strip()
    if not question:
        return Answer(ok=False, question="", scope_label="", error="Пустой вопрос")

    scope: Scope = scope_builder.build(role, binding)
    depth = (depth or "auto").strip().lower()
    if depth not in DEPTHS:
        depth = "auto"
    history = history or []

    # Кто из людей в вопросе РУ, а кто ТМ — решает код по справочникам, а не модель.
    people = people_resolver.resolve(question, scope)
    context = people.prompt_block() if people else ""
    person_note = people.note() if people else ""

    if not AGENT_ENABLED or (depth == "fast" and not history):
        return _ask_fast(question, question, scope, role, binding, actor, model, on_stage,
                         context=context, person_note=person_note)

    # Разбор задачи: эвристика для очевидных фактов, иначе одна подсказка модели.
    from .agent import loop as agent_loop, schema_tools, tools as agent_tools
    from .agent.llm import ModelUnavailable, OllamaChat
    from .agent.state import Budget

    chat = (MODEL_FACTORY or OllamaChat)(model)
    probe = agent_tools.ToolContext(question=question, scope=scope, budget=Budget.for_depth("analyze"))
    data_range = schema_tools.data_range(probe)
    hits = probe.semantic.find(question)
    if context:
        hits = {**hits, "people": context}
    today = date.today().isoformat()

    _notify(on_stage, "plan", "active", kind="plan")
    try:
        plan = agent_loop.triage(
            question, history=history, semantic=probe.semantic, model=chat, today=today,
            data_range=data_range, hits=hits, depth=depth,
        )
    except ModelUnavailable as err:
        answer = Answer(ok=False, question=question, scope_label=scope.label,
                        error=str(err), rule="model_unavailable", depth=depth)
        _notify(on_stage, "plan", "failed", note="Модель недоступна", kind="plan")
        _log(answer, scope, role, binding, actor, "model_unavailable")
        return answer
    plan_note = f"Разобрал задачу: {TASK_TITLES.get(plan.task_type, plan.task_type)} · {DEPTH_TITLES.get(plan.depth, plan.depth)}"
    _notify(on_stage, "plan", "done", ms=plan.elapsed_ms or None, note=plan_note, kind="plan")

    if plan.depth == "fast":
        answer = _ask_fast(question, plan.standalone_question or question, scope, role, binding,
                           actor, model, on_stage, plan_ms=plan.elapsed_ms,
                           context=context, person_note=person_note)
        answer.plan = plan.as_dict()
        answer.frame.update({"standalone": plan.standalone_question or question,
                             "taskType": plan.task_type, "metrics": plan.metrics,
                             "period": plan.period, "filters": plan.filters})
        return answer

    outcome = agent_loop.run(
        question, scope, plan=plan, history=history, model=chat, on_stage=on_stage,
        generate_sql=lambda q: generator.generate(q, None, model, context=context).sql,
        scope_label=scope.label, today=today, data_range=data_range, hits=hits,
    )
    answer = _from_outcome(question, scope, outcome, plan)
    if person_note:
        answer.notes = [person_note] + list(answer.notes)
        if answer.analysis is not None:
            limits = list(answer.analysis.get("limitations") or [])
            answer.analysis["limitations"] = [person_note] + limits
    _log(answer, scope, role, binding, actor, "ok" if answer.ok else (answer.rule or "agent_failed"))
    return answer


# --- быстрый путь: прежний контур ------------------------------------------

def _ask_fast(question: str, model_question: str, scope: Scope, role: str, binding: str | None,
              actor: str | None, model: str | None, on_stage: Callable[[dict], None] | None,
              plan_ms: int = 0, context: str = "", person_note: str = "") -> Answer:
    answer = Answer(ok=False, question=question, scope_label=scope.label, plan_ms=plan_ms)

    feedback: str | None = None
    last_rejection: Rejected | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        answer.attempts = attempt
        _notify(on_stage, "draft", "active",
                note="Составляю запрос заново по замечанию проверки" if attempt > 1 else None)
        try:
            produced = generator.generate(model_question, feedback, model, context=context)
        except generator.ModelUnavailable as err:
            answer.error = str(err)
            answer.rule = "model_unavailable"
            _notify(on_stage, "draft", "failed", note="Модель недоступна")
            _log(answer, scope, role, binding, actor, "model_unavailable")
            return answer

        answer.sql_raw = produced.sql
        answer.model = produced.model
        answer.model_ms += produced.elapsed_ms
        _notify(on_stage, "draft", "done", ms=produced.elapsed_ms)

        _notify(on_stage, "check", "active")
        try:
            checked = validate(produced.sql, scope)
        except Rejected as rejection:
            last_rejection = rejection
            feedback = rejection.message
            _notify(on_stage, "check", "retry" if attempt < MAX_ATTEMPTS else "failed",
                    note=rejection.message)
            continue
        _notify(on_stage, "check", "done",
                note=f"Проверил допустимость — принято с {attempt}-й попытки" if attempt > 1
                else "Проверил допустимость — запрос разрешён, область данных подставлена")

        _notify(on_stage, "read", "active")
        try:
            result = executor.run(checked.sql, checked.row_limit)
        except executor.ExecutionError as err:
            last_rejection = None
            answer.error = str(err)
            answer.rule = "execution"
            answer.sql = checked.sql
            _notify(on_stage, "read", "failed", note="Витрина не ответила на запрос")
            _log(answer, scope, role, binding, actor, "execution_error")
            return answer

        answer.ok = True
        answer.sql = checked.sql
        answer.notes = ([person_note] if person_note else []) + list(checked.notes)
        answer.columns = result.columns
        answer.rows = [list(row) for row in result.rows]
        answer.truncated = result.truncated
        answer.sql_ms = result.elapsed_ms
        answer.row_count = len(answer.rows)
        _notify(on_stage, "read", "done", ms=result.elapsed_ms,
                note=f"Прочитал витрину — {answer.row_count} строк")

        # Пустой результат — не ноль. Проверка детерминированная, без модели:
        # пользователь должен услышать «данных нет» и доступный диапазон дат.
        no_data = _no_data_note(answer, scope)

        _notify(on_stage, "write", "active")
        if no_data:
            summary, summary_ms = no_data, 0
            answer.notes = list(answer.notes) + [no_data]
        else:
            summary, summary_ms = generator.narrate(
                model_question, scope.label, answer.columns, answer.rows, model
            )
        answer.summary = summary
        answer.narrate_ms = summary_ms
        _notify(on_stage, "write", "done", ms=summary_ms)
        answer.frame = {"question": question, "standalone": model_question,
                        "sql": " ".join(checked.sql.split())[:400],
                        "headline": summary[:240], "columns": list(answer.columns)[:8]}
        _log(answer, scope, role, binding, actor, "ok")
        return answer

    if last_rejection is not None:
        answer.error = last_rejection.message
        answer.rule = last_rejection.rule
    _log(answer, scope, role, binding, actor, "rejected")
    return answer


def _no_data_note(answer: Answer, scope: Scope) -> str:
    """Фраза об отсутствии данных, если запрос ничего не нашёл."""
    rows = answer.rows
    empty = not rows or all(all(v is None for v in row) for row in rows)
    if not empty:
        return ""
    try:
        from .agent import schema_tools, tools as agent_tools
        from .agent.state import Budget
        probe = agent_tools.ToolContext(question=answer.question, scope=scope,
                                        budget=Budget.for_depth("analyze"))
        info = schema_tools.data_range(probe)
    except Exception:  # noqa: BLE001 - подсказка не должна ронять ответ
        info = {}
    if info.get("min_date") and info.get("max_date"):
        return (f"По этому условию данных в витрине нет. Доступен период "
                f"с {info['min_date']} по {info['max_date']}.")
    return "По этому условию данных в витрине нет."


# --- агент → Answer ---------------------------------------------------------

def _from_outcome(question: str, scope: Scope, outcome, plan) -> Answer:
    analysis = outcome.analysis
    answer = Answer(
        ok=outcome.ok, question=question, scope_label=scope.label,
        model=outcome.model, model_ms=outcome.model_ms + (plan.elapsed_ms or 0),
        sql_ms=outcome.sql_ms, attempts=outcome.turns, error=outcome.error, rule=outcome.rule,
        depth=plan.depth, task_type=plan.task_type, plan=plan.as_dict(), plan_ms=plan.elapsed_ms,
        analysis=analysis.as_dict(), grounding=outcome.grounding, frame=dict(outcome.frame),
    )
    answer.summary = analysis.text()
    workspace = outcome.workspace
    answer.charts = list(workspace.charts)
    answer.steps = [step.public(with_code=True) for step in workspace.steps]
    answer.notes = []

    main_id = (outcome.frame.get("mainResult") or "").lower()
    main = workspace.results.get(main_id) if main_id else None
    if main is None or not main.rows:
        main = outcome.main_result
    if main is not None:
        answer.sql = main.sql
        answer.columns = list(main.columns)
        answer.rows = [list(r) for r in main.rows]
        answer.truncated = main.truncated
        answer.row_count = len(answer.rows)
    for rs in workspace.results.values():
        if rs.rows and (main is None or rs.id != main.id):
            answer.tables.append(rs.as_table())
    charted = {c.get("source") for c in answer.charts}
    # Таблицы, уже показанные графиком, в ответ не дублируются.
    answer.tables = [t for t in answer.tables if t["id"] not in charted]
    return answer


def _log(answer: Answer, scope: Scope, role: str, binding: str | None,
         actor: str | None, verdict: str) -> None:
    trace = None
    if answer.depth != "fast" or answer.plan:
        trace = json.dumps({
            "plan": answer.plan, "steps": answer.steps, "grounding": answer.grounding,
            "charts": [{"id": c.get("id"), "type": c.get("type"), "title": c.get("title")} for c in answer.charts],
        }, ensure_ascii=False, default=str)
    answer.journal_id = journal.write(
        {
            "actor": actor,
            "role": role,
            "binding": binding,
            "scope_label": scope.label,
            "question": answer.question,
            "model": answer.model,
            "sql_raw": answer.sql_raw,
            "sql_final": answer.sql,
            "verdict": verdict,
            "rule": answer.rule,
            "message": answer.error,
            "row_count": len(answer.rows),
            "model_ms": answer.model_ms + answer.narrate_ms,
            "sql_ms": answer.sql_ms,
            "prompt_version": contract.prompt_version(),
            "depth": answer.depth,
            "task_type": answer.task_type,
            "trace_json": trace,
        }
    )
