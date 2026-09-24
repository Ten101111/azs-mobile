"""run_sql и draft_sql — чтение витрины через прежний валидатор и исполнитель.

Порядок тот же, что в контуре FAST: модель предлагает SQL, валидатор решает
(allowlist, только SELECT, область данных, LIMIT), исполнитель выполняет.
Сверху — проверки качества результата (`checks`), которые возвращаются
модели вместе с данными.
"""
from __future__ import annotations

import time

from .. import executor, textstyle
from ..validator import Rejected, validate
from . import checks, schema_tools
from .state import ResultSet, Step
from .tools import ToolContext, ToolError, tool


def _short(purpose: str, fallback: str) -> str:
    text = " ".join((purpose or "").split())
    return (text[:90] + "…") if len(text) > 90 else (text or fallback)


@tool(
    "run_sql",
    "Выполнить один SELECT к витрине и получить результат как набор строк rN. "
    "Используй формулы из get_metric_definition, ограничивай период, агрегируй в SQL. "
    "Область данных пользователя подставляется системой — не добавляй её сам.",
    {"type": "object",
     "properties": {
         "sql": {"type": "string", "description": "один SELECT или WITH … SELECT, без точки с запятой"},
         "purpose": {"type": "string", "description": "зачем этот запрос, коротко и по-русски — это увидит пользователь"},
     },
     "required": ["sql", "purpose"]},
    kind="sql",
)
def run_sql(ctx: ToolContext, sql: str, purpose: str = "") -> dict:
    budget = ctx.budget
    if budget.used_sql >= budget.sql_calls:
        raise ToolError(
            f"лимит запросов к витрине исчерпан ({budget.sql_calls}); "
            "сформулируй ответ по уже полученным результатам через finish"
        )
    budget.used_sql += 1
    label = _short(purpose, "Читаю витрину")
    step = Step(key=ctx.workspace.next_id("s"), kind="sql", label=f"Читаю витрину: {label}",
                purpose=purpose)
    ctx.workspace.steps.append(step)
    ctx.emit(step, "active")
    started = time.monotonic()
    try:
        checked = validate(sql, ctx.scope, row_limit=budget.row_limit)
    except Rejected as err:
        step.ok = False
        step.error = err.message
        step.sql = sql
        step.ms = int((time.monotonic() - started) * 1000)
        step.label = f"Проверка отклонила запрос: {label}"
        ctx.emit(step, "failed")
        return {
            "error": f"запрос отклонён проверкой ({err.rule}): {err.message}",
            "rule": err.rule,
            "hint": "Исправь запрос: только таблицы и колонки каталога, один SELECT, без служебных функций.",
        }
    step.sql = checked.sql
    try:
        result = ctx.run_query(checked.sql, checked.row_limit)
    except executor.ExecutionError as err:
        step.ok = False
        step.error = str(err)
        step.ms = int((time.monotonic() - started) * 1000)
        step.label = f"Витрина не ответила: {label}"
        ctx.emit(step, "failed")
        return {"error": f"витрина не выполнила запрос: {err}",
                "hint": "Проверь синтаксис под диалект и имена колонок; упрости запрос."}
    ctx.sql_ms += result.elapsed_ms
    rows = [list(r) for r in result.rows]
    # Заголовки — по-русски, даже если модель не дала колонкам псевдонимы:
    # «vd_per_client» → «ВД на клиента, руб/чек». Модель дальше видит те же имена.
    columns = textstyle.rename_columns(list(result.columns), ctx.semantic, ctx.catalog)
    # Валидатор сам дописывает LIMIT, поэтому исполнитель не видит «лишней»
    # строки: ровно лимит строк — почти наверняка обрезка.
    truncated = result.truncated or len(rows) >= checked.row_limit
    warnings = checks.inspect(columns, rows, truncated, checked.row_limit,
                              _range_pair(ctx))
    rs = ResultSet(
        id=ctx.workspace.next_id("r"), columns=columns, rows=rows,
        source="sql", purpose=purpose, sql=checked.sql, truncated=truncated,
        warnings=warnings, elapsed_ms=result.elapsed_ms,
    )
    ctx.workspace.add(rs)
    step.ms = int((time.monotonic() - started) * 1000)
    step.rows = rs.row_count
    step.result_id = rs.id
    step.warnings = warnings
    step.label = f"Прочитал витрину: {label} — {rs.row_count} строк"
    ctx.emit(step, "done")
    out = rs.preview(limit=12 if rs.row_count > 12 else 12)
    out["notes"] = checked.notes
    if rs.row_count > 12:
        out["hint"] = (f"Показаны первые 12 строк из {rs.row_count}; для расчётов по всем строкам "
                       f"используй run_python с inputs=['{rs.id}'] или агрегируй в SQL.")
    return out


def _range_pair(ctx: ToolContext):
    info = schema_tools.data_range(ctx)
    return (info.get("min_date"), info.get("max_date"))


@tool(
    "draft_sql",
    "Черновик SQL от базового генератора «вопрос → SQL» по короткому вопросу на русском. "
    "Годится для простых выборок; полученный текст проверь и выполни через run_sql.",
    {"type": "object",
     "properties": {"question": {"type": "string", "description": "вопрос одним предложением с периодом и объектом"}},
     "required": ["question"]},
)
def draft_sql(ctx: ToolContext, question: str) -> dict:
    if ctx.generate_sql is None:
        raise ToolError("базовый генератор недоступен в этом окружении — напиши SQL сам")
    ctx.budget.used_schema += 1
    started = time.monotonic()
    try:
        sql = ctx.generate_sql(question)
    except Exception as err:  # noqa: BLE001
        raise ToolError(f"генератор не ответил: {err}") from err
    return {"sql": sql, "elapsed_ms": int((time.monotonic() - started) * 1000),
            "hint": "Это черновик: проверь период, формулу и выполни через run_sql."}
