"""Контур «вопрос → SQL → ответ».

Порядок намеренно жёсткий: модель предлагает, валидатор решает, исполнитель
выполняет. При отказе валидатора делается одна попытка перегенерации
с текстом замечания — дальше отказ возвращается пользователю.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import contract, executor, generator, journal, scope as scope_builder
from .validator import Rejected, Scope, validate

MAX_ATTEMPTS = 2

# Этапы конвейера. Названия даются в настоящем времени, пока этап идёт,
# и в прошедшем, когда он закончен: интерфейс показывает и то и другое.
STAGES = {
    "draft": ("Составляю запрос к витрине", "Составил запрос к витрине"),
    "check": ("Проверяю допустимость", "Проверил допустимость"),
    "read": ("Читаю витрину", "Прочитал витрину"),
    "write": ("Формулирую ответ", "Сформулировал ответ"),
}


def _notify(on_stage: Callable[[dict], None] | None, key: str, state: str,
            ms: int | None = None, note: str | None = None) -> None:
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


def ask(question: str, role: str, binding: str | None, actor: str | None = None,
        model: str | None = None,
        on_stage: Callable[[dict], None] | None = None) -> Answer:
    question = (question or "").strip()
    if not question:
        return Answer(ok=False, question="", scope_label="", error="Пустой вопрос")

    scope: Scope = scope_builder.build(role, binding)
    answer = Answer(ok=False, question=question, scope_label=scope.label)

    feedback: str | None = None
    last_rejection: Rejected | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        answer.attempts = attempt
        _notify(on_stage, "draft", "active",
                note="Составляю запрос заново по замечанию проверки" if attempt > 1 else None)
        try:
            produced = generator.generate(question, feedback, model)
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
        answer.notes = checked.notes
        answer.columns = result.columns
        answer.rows = [list(row) for row in result.rows]
        answer.truncated = result.truncated
        answer.sql_ms = result.elapsed_ms
        answer.row_count = len(answer.rows)
        _notify(on_stage, "read", "done", ms=result.elapsed_ms,
                note=f"Прочитал витрину — {answer.row_count} строк")

        _notify(on_stage, "write", "active")
        summary, summary_ms = generator.narrate(
            question, scope.label, answer.columns, answer.rows, model
        )
        answer.summary = summary
        answer.narrate_ms = summary_ms
        _notify(on_stage, "write", "done", ms=summary_ms)
        _log(answer, scope, role, binding, actor, "ok")
        return answer

    if last_rejection is not None:
        answer.error = last_rejection.message
        answer.rule = last_rejection.rule
    _log(answer, scope, role, binding, actor, "rejected")
    return answer


def _log(answer: Answer, scope: Scope, role: str, binding: str | None,
         actor: str | None, verdict: str) -> None:
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
        }
    )
