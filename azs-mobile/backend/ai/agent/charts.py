"""create_chart — описание графика для фронтенда, привязанное к данным.

Модель не переписывает числа в спецификацию: она указывает, из какого
результата (rN или pN) и каких колонок строить график, а значения сюда
подставляет код. Так ряд на графике всегда совпадает с тем, что прочитано
из витрины или посчитано в песочнице.

Универсальный формат на выходе:
  { "id", "type": line|bar|stacked_bar|scatter|waterfall|kpi|table,
    "title", "x": [...], "xTitle", "series": [{"name", "values": [...]}],
    "unit", "source" }
"""
from __future__ import annotations

from typing import Any

from .state import ResultSet, Step
from .tools import ToolContext, ToolError, tool

CHART_TYPES = ("line", "bar", "stacked_bar", "scatter", "waterfall", "kpi", "table")
MAX_POINTS = 400
MAX_CATEGORIES = 60


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _column(rs: ResultSet, name: str) -> list[Any]:
    try:
        return rs.column(name)
    except KeyError as err:
        raise ToolError(f"в {rs.id} нет колонки «{name}»; есть: {', '.join(rs.columns)}") from err


def _numeric_columns(rs: ResultSet) -> list[str]:
    out = []
    for index, column in enumerate(rs.columns):
        values = [row[index] for row in rs.rows if row[index] is not None][:20]
        if values and all(_number(v) is not None for v in values):
            out.append(column)
    return out


def build(rs: ResultSet, spec: dict, chart_id: str) -> dict:
    kind = (spec.get("type") or "").strip().lower()
    if kind not in CHART_TYPES:
        raise ToolError(f"type должен быть одним из: {', '.join(CHART_TYPES)}")
    title = " ".join(str(spec.get("title") or rs.purpose or "").split())[:120]
    unit = str(spec.get("unit") or "")
    out: dict[str, Any] = {"id": chart_id, "type": kind, "title": title, "source": rs.id, "unit": unit}

    if kind == "table":
        out["columns"] = rs.columns
        out["rows"] = rs.rows[:MAX_CATEGORIES]
        return out

    if kind == "kpi":
        # Карточки: либо явно перечисленные колонки первой строки, либо все числовые.
        if not rs.rows:
            raise ToolError("для kpi нужен хотя бы один ряд данных")
        columns = spec.get("columns") or _numeric_columns(rs) or rs.columns
        row = rs.rows[0]
        cards = []
        for column in columns[:6]:
            value = row[rs.column_index(column)] if column in rs.columns else None
            card = {"label": column, "value": value}
            cards.append(card)
        deltas = spec.get("deltas") or {}
        for card in cards:
            delta_col = deltas.get(card["label"]) if isinstance(deltas, dict) else None
            if delta_col and delta_col in rs.columns:
                card["delta"] = row[rs.column_index(delta_col)]
        out["cards"] = cards
        return out

    x_name = spec.get("x")
    if not x_name:
        # Первая нечисловая колонка — ось X по умолчанию.
        numeric = set(_numeric_columns(rs))
        x_name = next((c for c in rs.columns if c not in numeric), rs.columns[0])
    x_values = _column(rs, x_name)
    series_spec = spec.get("series") or []
    if isinstance(series_spec, str):
        series_spec = [series_spec]
    if not series_spec:
        series_spec = [c for c in _numeric_columns(rs) if c != x_name][:4]
    if not series_spec:
        raise ToolError("не удалось выбрать числовые колонки для series")
    series = []
    for item in series_spec:
        if isinstance(item, str):
            column, name = item, item
        elif isinstance(item, dict):
            column = item.get("column") or item.get("y") or item.get("name")
            name = item.get("name") or column
        else:
            raise ToolError("series: список имён колонок или объектов {column, name}")
        if not column:
            raise ToolError("series: у элемента нет column")
        values = [_number(v) for v in _column(rs, column)]
        series.append({"name": str(name), "column": column, "values": values})

    group = spec.get("group")
    if group and group in rs.columns and len(series) == 1:
        # Длинный формат: одна колонка значений, ряды по значению group.
        groups = _column(rs, group)
        column = series[0]["column"]
        labels: list[Any] = []
        for x in x_values:
            if x not in labels:
                labels.append(x)
        by_group: dict[Any, dict[Any, float | None]] = {}
        for x, g, v in zip(x_values, groups, _column(rs, column)):
            by_group.setdefault(g, {})[x] = _number(v)
        series = [{"name": str(g), "column": column, "values": [vals.get(x) for x in labels]}
                  for g, vals in list(by_group.items())[:12]]
        x_values = labels

    if kind == "scatter":
        if len(series) < 1:
            raise ToolError("scatter: нужны x и хотя бы одна колонка y")
        label_col = spec.get("label")
        labels = _column(rs, label_col) if label_col and label_col in rs.columns else None
        xs = [_number(v) for v in x_values]
        points = []
        for index, y in enumerate(series[0]["values"]):
            if xs[index] is None or y is None:
                continue
            point = {"x": xs[index], "y": y}
            if labels is not None:
                point["label"] = str(labels[index])
            points.append(point)
        out.update({"xTitle": str(spec.get("xTitle") or x_name), "yTitle": str(spec.get("yTitle") or series[0]["name"]),
                    "points": points[:MAX_POINTS]})
        return out

    limit = MAX_POINTS if kind == "line" else MAX_CATEGORIES
    if len(x_values) > limit:
        x_values = x_values[:limit]
        for item in series:
            item["values"] = item["values"][:limit]
        out["note"] = f"показаны первые {limit} точек"
    out["x"] = [str(v) if v is not None else "" for v in x_values]
    out["xTitle"] = str(spec.get("xTitle") or x_name)
    out["series"] = [{"name": s["name"], "values": s["values"]} for s in series]
    if kind == "waterfall":
        # Первый ряд — приращения; итоговая колонка считается здесь, а не моделью.
        values = [v or 0.0 for v in series[0]["values"]]
        start = _number(spec.get("start"))
        out["start"] = start
        out["total"] = (start or 0.0) + sum(values)
    return out


@tool(
    "create_chart",
    "Построить график из готового результата rN/pN: line (динамика), bar, stacked_bar (структура), "
    "scatter (связь двух показателей), waterfall (вклад драйверов), kpi (карточки чисел), table. "
    "Данные берутся из результата по именам колонок — числа сюда не переписывай. "
    "Не строй график для одного числа.",
    {"type": "object",
     "properties": {
         "source": {"type": "string", "description": "идентификатор результата, например r1 или p1"},
         "type": {"type": "string", "enum": list(CHART_TYPES)},
         "title": {"type": "string"},
         "x": {"type": "string", "description": "колонка оси X (для line/bar/scatter/waterfall)"},
         "series": {"type": "array", "items": {"type": "string"}, "description": "колонки значений"},
         "group": {"type": "string", "description": "колонка, по значениям которой разбить один ряд на несколько"},
         "label": {"type": "string", "description": "scatter: колонка подписи точки"},
         "unit": {"type": "string"},
         "xTitle": {"type": "string"},
         "yTitle": {"type": "string"},
         "start": {"type": "number", "description": "waterfall: начальное значение"},
         "columns": {"type": "array", "items": {"type": "string"}, "description": "kpi: какие колонки первой строки показать"},
     },
     "required": ["source", "type"]},
    kind="chart",
)
def create_chart(ctx: ToolContext, source: str, type: str, **spec) -> dict:
    budget = ctx.budget
    if budget.used_charts >= budget.charts:
        raise ToolError(f"лимит графиков исчерпан ({budget.charts})")
    try:
        rs = ctx.workspace.get(source)
    except KeyError as err:
        raise ToolError(str(err)) from err
    if not rs.rows:
        raise ToolError(f"{rs.id} пуст — график строить не из чего")
    chart_id = ctx.workspace.next_id("c")
    chart = build(rs, {"type": type, **spec}, chart_id)
    budget.used_charts += 1
    ctx.workspace.charts.append(chart)
    step = Step(key=ctx.workspace.next_id("s"), kind="chart", ok=True,
                label=f"Построил график: {chart['title'] or chart['type']}", result_id=rs.id)
    ctx.workspace.steps.append(step)
    ctx.emit(step, "done")
    summary = {k: v for k, v in chart.items() if k not in {"x", "series", "rows", "points"}}
    summary["points"] = len(chart.get("x") or chart.get("points") or chart.get("rows") or [])
    summary["series"] = [s["name"] for s in chart.get("series", [])]
    return {"chart": summary, "hint": "График сохранён и будет показан пользователю; в finish его повторно описывать не нужно."}
