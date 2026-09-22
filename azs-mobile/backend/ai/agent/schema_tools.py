"""Инструменты схемы: что есть в витрине и что означают колонки.

Источник — каталог и семантический слой, а не information_schema: модель
видит ровно те таблицы, которые разрешены валидатору. Всё, что требует
чтения данных (примеры значений, диапазон дат, допустимые значения
измерений), идёт через тот же валидатор и исполнитель с областью данных.
"""
from __future__ import annotations

import re
from functools import lru_cache

from ..contract import SCHEMA as STAND_SCHEMA
from ..validator import Rejected, validate
from .state import Step
from .tools import ToolContext, ToolError, tool

_HEADER_RE = re.compile(r"^(?P<table>[\w.]+)\s+[—-]\s+(?P<purpose>.+)$")
_COLUMN_RE = re.compile(r"^\s{2,}(?P<name>\w+)\s{2,}(?P<type>[\w()\s,]+?)\s{2,}(?P<desc>.+)$")


@lru_cache(maxsize=4)
def parse_schema(text: str, known: tuple[str, ...]) -> dict[str, dict]:
    """Текст описания витрины → {таблица: {purpose, columns: {имя: {type, description}}}}."""
    tables: dict[str, dict] = {}
    current: str | None = None
    for line in text.splitlines():
        header = _HEADER_RE.match(line.strip()) if line and not line.startswith(" ") else None
        if header:
            name = header.group("table").split(".")[-1].lower()
            if name in known:
                current = name
                tables[current] = {"purpose": header.group("purpose").strip(), "columns": {}}
                continue
            current = None
        if current is None:
            continue
        column = _COLUMN_RE.match(line)
        if column:
            tables[current]["columns"][column.group("name").lower()] = {
                "type": " ".join(column.group("type").split()),
                "description": column.group("desc").strip(),
            }
    return tables


def _schema_text(ctx: ToolContext) -> str:
    return ctx.catalog.description or STAND_SCHEMA


def _tables(ctx: ToolContext) -> dict[str, dict]:
    parsed = parse_schema(_schema_text(ctx), tuple(sorted(ctx.catalog.tables)))
    # Колонки каталога, которых нет в тексте описания, всё равно существуют.
    for table, columns in ctx.catalog.tables.items():
        entry = parsed.setdefault(table, {"purpose": "", "columns": {}})
        for column in sorted(columns):
            entry["columns"].setdefault(column, {"type": "", "description": ""})
    return parsed


def _run(ctx: ToolContext, sql: str, row_limit: int = 100) -> tuple[list[str], list[list]]:
    """Служебный запрос — через валидатор и исполнитель, как любой другой."""
    try:
        checked = validate(sql, ctx.scope, row_limit=row_limit)
    except Rejected as err:
        raise ToolError(f"служебный запрос отклонён проверкой: {err.message}") from err
    result = ctx.run_query(checked.sql, checked.row_limit)
    ctx.sql_ms += result.elapsed_ms
    return result.columns, [list(r) for r in result.rows]


def _facts(ctx: ToolContext) -> tuple[str, str, str]:
    """Таблица фактов, колонка даты и ключ объекта из каталога."""
    facts = ctx.catalog.facts_table or next(iter(ctx.catalog.scoped_tables or ctx.catalog.tables))
    date_col = ctx.catalog.date_column or ctx.semantic.time.get("date_column", "")
    return facts, date_col, ctx.catalog.scope_column


def data_range(ctx: ToolContext) -> dict:
    if ctx._range is not None:
        return ctx._range
    facts, date_col, key = _facts(ctx)
    if not date_col:
        ctx._range = {"table": facts, "min_date": None, "max_date": None, "stations": None}
        return ctx._range
    sql = (f"SELECT MIN({date_col}) AS min_date, MAX({date_col}) AS max_date,"
           f" COUNT(DISTINCT {key}) AS stations FROM {ctx.qualified(facts)}")
    try:
        _, rows = _run(ctx, sql, 1)
        row = rows[0] if rows else [None, None, None]
    except Exception as err:  # noqa: BLE001 - диапазон не должен ронять шаг
        ctx._range = {"table": facts, "min_date": None, "max_date": None, "stations": None,
                      "error": str(err)}
        return ctx._range
    ctx._range = {"table": facts, "min_date": row[0], "max_date": row[1], "stations": row[2],
                  "date_column": date_col}
    return ctx._range


def _step(ctx: ToolContext, kind: str, label: str, purpose: str = "") -> Step:
    step = Step(key=ctx.workspace.next_id("s"), kind=kind, label=label, purpose=purpose)
    ctx.workspace.steps.append(step)
    return step


# --- инструменты -----------------------------------------------------------

@tool(
    "get_schema",
    "Перечень таблиц витрины с назначением, ключами, гранулярностью, колонками и диапазоном дат. "
    "Вызывай один раз в начале, если не уверен в именах колонок.",
    {"type": "object", "properties": {}, "required": []},
)
def get_schema(ctx: ToolContext) -> dict:
    ctx.budget.used_schema += 1
    _step(ctx, "schema", "Посмотрел состав витрины")
    tables = _tables(ctx)
    out = []
    for name, spec in tables.items():
        out.append({
            "table": ctx.qualified(name),
            "purpose": spec["purpose"],
            "scoped": name in ctx.catalog.scoped_tables,
            "columns": [
                {"name": column, **meta} for column, meta in spec["columns"].items()
            ],
        })
    entity = dict(ctx.semantic.entity)
    return {
        "dialect": ctx.catalog.dialect,
        "tables": out,
        "entity": entity,
        "time": ctx.semantic.time,
        "data_range": data_range(ctx),
        "rules": ctx.semantic.rules,
        "peer_groups": ctx.semantic.peer_groups,
    }


@tool(
    "search_schema",
    "Найти таблицы, колонки, показатели и измерения, относящиеся к вопросу или его части. "
    "Возвращает и то, чего в витрине заведомо нет, — об этом надо сказать пользователю прямо.",
    {"type": "object",
     "properties": {"query": {"type": "string", "description": "фрагмент вопроса или название показателя"}},
     "required": ["query"]},
)
def search_schema(ctx: ToolContext, query: str) -> dict:
    ctx.budget.used_schema += 1
    _step(ctx, "schema", f"Искал в витрине: {query[:60]}")
    found = ctx.semantic.find(query)
    tokens = [t for t in re.findall(r"[a-zа-яё0-9_]+", query.lower()) if len(t) > 2]
    columns = []
    for table, spec in _tables(ctx).items():
        for column, meta in spec["columns"].items():
            hay = f"{column} {meta.get('description', '')}".lower()
            score = sum(1 for t in tokens if t in hay)
            if score:
                columns.append({"table": ctx.qualified(table), "column": column, **meta, "score": score})
    columns.sort(key=lambda c: -c["score"])
    for item in found["metrics"]:
        item.pop("aliases", None)
    for item in found["dimensions"]:
        item.pop("aliases", None)
    return {
        "metrics": found["metrics"][:6],
        "dimensions": found["dimensions"][:6],
        "columns": columns[:10],
        "absent": found["absent"],
        "hint": ("Формулы бери из metrics.expr; если нужный показатель не найден и попал в absent — "
                 "не изобретай его, а назови отсутствие в ответе."),
    }


@tool(
    "get_column_metadata",
    "Тип, описание, доля пустых значений, минимум/максимум и примеры значений колонки в области данных пользователя.",
    {"type": "object",
     "properties": {"table": {"type": "string"}, "column": {"type": "string"}},
     "required": ["table", "column"]},
)
def get_column_metadata(ctx: ToolContext, table: str, column: str) -> dict:
    ctx.budget.used_schema += 1
    _step(ctx, "schema", f"Проверил колонку {column}")
    name = table.split(".")[-1].lower().strip()
    col = column.lower().strip()
    tables = _tables(ctx)
    if name not in tables:
        raise ToolError(f"таблицы {table} нет в каталоге; есть: {', '.join(tables)}")
    if col not in tables[name]["columns"]:
        raise ToolError(f"колонки {column} нет в {table}; есть: {', '.join(tables[name]['columns'])}")
    meta = dict(tables[name]["columns"][col])
    qualified = ctx.qualified(name)
    stats_sql = (f"SELECT COUNT(*) AS total, COUNT({col}) AS filled, COUNT(DISTINCT {col}) AS distinct_values,"
                 f" MIN({col}) AS min_value, MAX({col}) AS max_value FROM {qualified}")
    _, rows = _run(ctx, stats_sql, 1)
    total, filled, distinct, low, high = rows[0] if rows else (0, 0, 0, None, None)
    sample_sql = (f"SELECT {col} AS value, COUNT(*) AS n FROM {qualified} WHERE {col} IS NOT NULL"
                  f" GROUP BY {col} ORDER BY n DESC LIMIT 8")
    _, samples = _run(ctx, sample_sql, 8)
    return {
        "table": qualified, "column": col, **meta,
        "rows": total, "filled": filled,
        "null_share_pct": round(100.0 * (total - filled) / total, 1) if total else None,
        "distinct": distinct, "min": low, "max": high,
        "top_values": [{"value": v, "rows": n} for v, n in samples],
    }


@tool(
    "get_metric_definition",
    "Официальная формула показателя из семантического слоя: SQL-выражение, единица, направление "
    "(больше — лучше или меньше — лучше), драйверы для разложения и оговорки. "
    "Используй её вместо собственной формулы.",
    {"type": "object",
     "properties": {"metric": {"type": "string", "description": "ключ или название показателя, например «трафик»"}},
     "required": ["metric"]},
)
def get_metric_definition(ctx: ToolContext, metric: str) -> dict:
    ctx.budget.used_schema += 1
    _step(ctx, "schema", f"Уточнил определение показателя: {metric[:50]}")
    found = ctx.semantic.metric(metric)
    if found is None:
        near = ctx.semantic.find(metric)
        return {
            "found": False,
            "metric": metric,
            "candidates": [{"key": m["key"], "title": m["title"], "unit": m["unit"]} for m in near["metrics"][:5]],
            "absent": near["absent"],
            "hint": "Показателя с таким названием в семантическом слое нет. Если он в absent — "
                    "скажи пользователю, что таких данных в витрине нет; иначе выбери из candidates.",
        }
    facts, date_col, _ = _facts(ctx)
    table = found.table or facts
    from_clause = table if " " in table else ctx.qualified(table)
    drivers = []
    for key in found.drivers:
        driver = ctx.semantic.metrics.get(key)
        if driver:
            drivers.append({"key": driver.key, "title": driver.title, "expr": driver.expr, "unit": driver.unit})
    return {
        "found": True,
        **found.describe(),
        "from": from_clause,
        "sql_example": f'SELECT {found.expr} AS "{found.title}, {found.unit}" FROM {from_clause} WHERE <период>',
        "drivers_detail": drivers,
        "time": ctx.semantic.time,
    }


@tool(
    "get_dimension_values",
    "Допустимые значения измерения (регион, ОНПО, формат…) в области данных пользователя, с числом объектов; "
    "для дат — первая и последняя. Нужен, чтобы подставлять точные написания в фильтры.",
    {"type": "object",
     "properties": {
         "dimension": {"type": "string", "description": "ключ измерения или название колонки"},
         "search": {"type": "string", "description": "подстрока для отбора значений, например «моск»"},
         "limit": {"type": "integer", "description": "сколько значений вернуть, до 100"},
     },
     "required": ["dimension"]},
)
def get_dimension_values(ctx: ToolContext, dimension: str, search: str = "", limit: int = 40) -> dict:
    ctx.budget.used_schema += 1
    _step(ctx, "schema", f"Уточнил значения измерения: {dimension[:40]}")
    dim = ctx.semantic.dimension(dimension)
    if dim is None:
        # Возможно, названа колонка каталога напрямую.
        col = dimension.lower().strip()
        table = next((t for t, cols in ctx.catalog.tables.items() if col in cols), None)
        if table is None:
            raise ToolError(
                f"измерения «{dimension}» нет; есть: " + ", ".join(ctx.semantic.dimensions)
            )
        column, table_name, kind = col, table, "category"
    else:
        column, table_name, kind = dim.column, dim.table, dim.kind
    qualified = ctx.qualified(table_name)
    limit = max(1, min(int(limit or 40), 100))
    if kind == "time":
        _, rows = _run(ctx, f"SELECT MIN({column}) AS first, MAX({column}) AS last,"
                            f" COUNT(DISTINCT {column}) AS points FROM {qualified}", 1)
        first, last, points = rows[0] if rows else (None, None, 0)
        return {"dimension": dimension, "column": column, "table": qualified, "kind": "time",
                "first": first, "last": last, "points": points}
    # Регистр сравнивается в Python: LOWER() в SQLite не понижает кириллицу,
    # а значений у измерения немного — читаем до 500 и отбираем здесь.
    clean = re.sub(r"[^\w\s\-.]", "", search or "").strip().lower().replace("ё", "е")
    entity_key = ctx.catalog.scope_column
    counter = f"COUNT(DISTINCT {entity_key})" if entity_key in ctx.catalog.tables.get(table_name, set()) else "COUNT(*)"
    fetch = 500 if clean else limit
    sql = (f"SELECT {column} AS value, {counter} AS objects FROM {qualified} WHERE {column} IS NOT NULL"
           f" GROUP BY {column} ORDER BY objects DESC LIMIT {fetch}")
    _, rows = _run(ctx, sql, fetch)
    if clean:
        rows = [r for r in rows if clean in str(r[0]).lower().replace("ё", "е")][:limit]
    return {
        "dimension": dimension, "column": column, "table": qualified, "kind": kind,
        "values": [{"value": v, "objects": n} for v, n in rows],
        "note": "Подставляй значения в фильтр точно в таком написании." if rows else
                "Значений не найдено — уточни написание через search или проверь измерение.",
    }


@tool(
    "get_data_range",
    "Первая и последняя дата данных в витрине и число объектов в области данных. "
    "Вызывай перед ответом про период, которого может не быть.",
    {"type": "object", "properties": {}, "required": []},
)
def get_data_range(ctx: ToolContext) -> dict:
    ctx.budget.used_schema += 1
    _step(ctx, "schema", "Проверил диапазон дат витрины")
    return dict(data_range(ctx))
