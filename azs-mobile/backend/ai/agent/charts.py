"""create_chart — описание графика для фронтенда, привязанное к данным.

Модель не переписывает числа в спецификацию: она указывает, из какого
результата (rN или pN) и каких колонок строить график, а значения сюда
подставляет код. Так ряд на графике всегда совпадает с тем, что прочитано
из витрины или посчитано в песочнице.

Универсальный формат на выходе:
  { "id", "type": line|bar|stacked_bar|scatter|waterfall|kpi|table|compare,
    "title", "x": [...], "xTitle", "series": [{"name", "values": [...]}],
    "unit", "source", "sourceTitle", "sourceKind", "period" }

ИИ-17 (25.09.2026): числа в спецификацию не передаются вовсе (ключи values/data/…
отклоняются, начало водопада — колонка или число из результата); сравнение
периодов compare выравнивает периоды по одинаковому числу дней; у KPI —
отклонение от базы (план, прошлый год), посчитанное кодом; не больше 6 рядов;
auto_chart — простой график для «Лёгкого» без участия модели.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .state import ResultSet, Step
from .tools import ToolContext, ToolError, tool

CHART_TYPES = ("line", "bar", "stacked_bar", "scatter", "waterfall", "kpi", "table", "compare")
MAX_POINTS = 400
MAX_CATEGORIES = 60
# Больше шести рядов на экране телефона не различить (ИИ-17).
MAX_SERIES = 6
# Ключи, которыми модель могла бы передать числа в обход результата.
RAW_NUMBER_KEYS = ("values", "data", "numbers", "points", "rows")
# Графики с одной осью значений: все ряды должны быть в одной единице (ИИ-17).
ONE_AXIS = ("line", "bar", "stacked_bar", "waterfall")
# Ряды без единицы в названии, отличающиеся по величине в столько раз и больше,
# на одной оси нечитаемы: меньший ряд лежит на нуле.
SCALE_GAP = 100.0
UNIT_ALIASES = {
    "₽": "₽", "руб": "₽", "руб.": "₽", "рублей": "₽", "р": "₽", "р.": "₽",
    "тыс. ₽": "тыс. ₽", "тыс ₽": "тыс. ₽", "тыс. руб": "тыс. ₽", "тыс руб": "тыс. ₽",
    "млн ₽": "млн ₽", "млн руб": "млн ₽", "млн. руб": "млн ₽",
    "л": "л", "литры": "л", "литров": "л", "тыс. л": "тыс. л", "тыс л": "тыс. л",
    "т": "т", "тонн": "т", "тонны": "т", "тыс. т": "тыс. т",
    "шт": "шт", "шт.": "шт",
    "%": "%", "п.п": "п.п.", "п.п.": "п.п.", "п. п.": "п.п.", "п. п": "п.п.",
}


def column_unit(name: str) -> str | None:
    """Единица из названия колонки: «Объём, т» → «т», «Конверсия НТУ, %» → «%»."""
    text = " ".join(str(name or "").split())
    tail = ""
    if "," in text:
        tail = text.rsplit(",", 1)[1]
    elif text.endswith(")") and "(" in text:
        tail = text[text.rindex("(") + 1:-1]
    elif text.endswith("%"):
        tail = "%"
    tail = tail.strip().lower()
    if not tail or len(tail) > 12:
        return None
    return UNIT_ALIASES.get(tail, UNIT_ALIASES.get(tail.rstrip("."), tail))


def _peak(values: list) -> float:
    present = [abs(v) for v in values if isinstance(v, (int, float))]
    return max(present) if present else 0.0


def split_by_unit(series: list[dict]) -> tuple[list[dict], list[dict], str | None]:
    """Оставить ряды в единице главного (первого) ряда; вернуть оставленные, снятые и единицу.

    Ряд с другой известной единицей снимается всегда («л» и «т», «₽» и «%»).
    Ряд без единицы снимается, если он отличается от главного по величине
    в SCALE_GAP раз и больше: на общей оси он неотличим от нуля.
    """
    units = [column_unit(s["column"]) for s in series]
    main_unit = units[0] or next((u for u in units if u), None)
    main_peak = _peak(series[0]["values"]) if series else 0.0
    kept, dropped = [], []
    for item, unit in zip(series, units):
        if not kept:
            kept.append(item)
            continue
        if unit and main_unit and unit != main_unit:
            dropped.append(item)
            continue
        if not (unit and unit == main_unit):
            peak = _peak(item["values"])
            low, high = sorted((peak, main_peak))
            if low > 0 and high / low >= SCALE_GAP:
                dropped.append(item)
                continue
        kept.append(item)
    return kept, dropped, main_unit


MONTHS_SHORT = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?(?:[T ].*)?$")
_RU_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_RU_MONTH = re.compile(r"^(\d{1,2})\.(\d{4})$")


def parse_date(value: Any) -> tuple[date, str] | None:
    """Дата из ячейки: (дата, "day" | "month") или None. Месяц — первым числом."""
    if isinstance(value, datetime):
        return value.date(), "day"
    if isinstance(value, date):
        return value, "day"
    text = str(value or "").strip()
    try:
        match = _ISO_DATE.match(text)
        if match:
            year, month, day = int(match[1]), int(match[2]), match[3]
            return (date(year, month, int(day)), "day") if day else (date(year, month, 1), "month")
        match = _RU_DATE.match(text)
        if match:
            return date(int(match[3]), int(match[2]), int(match[1])), "day"
        match = _RU_MONTH.match(text)
        if match:
            return date(int(match[2]), int(match[1]), 1), "month"
    except ValueError:
        return None
    return None


def _dates(values: list[Any]) -> list[tuple[date, str]] | None:
    """Все непустые значения колонки — даты одного вида; иначе None."""
    parsed = []
    for value in values:
        if value in (None, ""):
            continue
        found = parse_date(value)
        if found is None or (parsed and found[1] != parsed[0][1]):
            return None          # первая же не-дата — не колонка дат (широкие файлы не перебираем)
        parsed.append(found)
    return parsed or None


def _fmt_day(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _fmt_month(value: date) -> str:
    return f"{MONTHS_SHORT[value.month - 1]} {value.year}"


def span_text(values: list[Any]) -> str | None:
    """Период по колонке дат: «01.09.2026 – 25.09.2026» или «янв 2025 – авг 2026»."""
    parsed = _dates(values)
    if not parsed:
        return None
    days = [d for d, _g in parsed]
    fmt = _fmt_month if parsed[0][1] == "month" else _fmt_day
    lo, hi = min(days), max(days)
    return fmt(lo) if lo == hi else f"{fmt(lo)} – {fmt(hi)}"


def result_period(rs: ResultSet, prefer: str | None = None) -> str | None:
    """Период результата — по колонке дат (сначала по оси X графика)."""
    columns = ([prefer] if prefer and prefer in rs.columns else []) + [c for c in rs.columns if c != prefer]
    for column in columns:
        text = span_text(rs.column(column))
        if text:
            return text
    return None


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


def _has_column(rs: ResultSet, name: Any) -> bool:
    try:
        rs.column_index(str(name))
    except KeyError:
        return False
    return True


def _cell(rs: ResultSet, row: list[Any], column: str) -> Any:
    return row[rs.column_index(column)]


def _in_result(rs: ResultSet, number: float) -> bool:
    """Число встречается в результате (с точностью до округления)."""
    for row in rs.rows:
        for value in row:
            present = _number(value)
            if present is not None and abs(present - number) <= max(1e-9, abs(number) * 1e-9):
                return True
    return False


def build(rs: ResultSet, spec: dict, chart_id: str) -> dict:
    kind = (spec.get("type") or "").strip().lower()
    if kind not in CHART_TYPES:
        raise ToolError(f"type должен быть одним из: {', '.join(CHART_TYPES)}")
    raw = [key for key in RAW_NUMBER_KEYS if key in spec]
    if raw:
        # График строится только по результату: числа из спецификации не принимаются (ИИ-17).
        raise ToolError(f"числа в график не передаются ({', '.join(raw)}): укажи колонки результата {rs.id} "
                        "в x и series — значения подставит код")
    title = " ".join(str(spec.get("title") or rs.purpose or "").split())[:120]
    unit = str(spec.get("unit") or "")
    out: dict[str, Any] = {"id": chart_id, "type": kind, "title": title, "source": rs.id, "unit": unit,
                           "sourceTitle": rs.purpose or "", "sourceKind": rs.source}
    period = result_period(rs, spec.get("x"))
    if period:
        out["period"] = period

    if kind == "table":
        out["columns"] = rs.columns
        out["rows"] = rs.rows[:MAX_CATEGORIES]
        return out

    if kind == "kpi":
        return _kpi(rs, spec, out)

    if kind == "compare":
        return _compare(rs, spec, out)

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
                  for g, vals in by_group.items()]
        x_values = labels
        if len(series) > MAX_SERIES:
            # Оставляем ряды с наибольшими значениями: остальные — в таблице и выгрузке.
            total = len(series)
            weight = {id(s): sum(abs(v) for v in s["values"] if v is not None) for s in series}
            keep = {id(s) for s in sorted(series, key=lambda s: -weight[id(s)])[:MAX_SERIES]}
            series = [s for s in series if id(s) in keep]          # порядок рядов — как в данных
            out["note"] = f"Показаны {MAX_SERIES} рядов из {total} с наибольшими значениями; все — в таблице."

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

    if len(series) > MAX_SERIES:
        total = len(series)
        series = series[:MAX_SERIES]
        out["note"] = f"Показаны первые {MAX_SERIES} рядов из {total}: больше на графике не различить."

    if kind in ONE_AXIS and len(series) > 1 and not (group and group in rs.columns):
        series, dropped, axis_unit = split_by_unit(series)
        if dropped:
            names = ", ".join(f"«{d['name']}»" for d in dropped)
            out["dropped"] = [d["name"] for d in dropped]
            unit_note = (f"Не показаны {names}: другая единица измерения или масштаб, "
                         f"чем у «{series[0]['name']}», — на одной оси их не сравнить.")
            out["note"] = f"{out['note']} {unit_note}" if out.get("note") else unit_note
    else:
        axis_unit = column_unit(series[0]["column"]) if series else None
    if axis_unit:
        # Единица оси берётся из данных: подпись модели не может ей противоречить.
        out["unit"] = axis_unit

    limit = MAX_POINTS if kind == "line" else MAX_CATEGORIES
    if len(x_values) > limit:
        x_values = x_values[:limit]
        for item in series:
            item["values"] = item["values"][:limit]
        out["note"] = (out.get("note", "") + " " if out.get("note") else "") + f"Показаны первые {limit} точек."
    out["x"] = _x_labels(x_values)
    out["xTitle"] = str(spec.get("xTitle") or x_name)
    out["series"] = [{"name": s["name"], "values": s["values"]} for s in series]
    if kind == "waterfall":
        # Первый ряд — приращения; итоговая колонка считается здесь, а не моделью.
        values = [v or 0.0 for v in series[0]["values"]]
        start = _start(rs, spec.get("start"))
        out["start"] = start
        out["total"] = (start or 0.0) + sum(values)
    return out


def _x_labels(values: list[Any]) -> list[str]:
    """Подписи оси X: даты — по-русски («мар 2026», «01.09»), остальное как есть."""
    parsed = _dates(values)
    if parsed and len(parsed) == sum(1 for v in values if v not in (None, "")):
        years = {d.year for d, _g in parsed}
        out, found = [], iter(parsed)
        for value in values:
            if value in (None, ""):
                out.append("")
                continue
            day, grain = next(found)
            if grain == "month":
                out.append(_fmt_month(day))
            else:
                out.append(day.strftime("%d.%m") if len(years) == 1 else _fmt_day(day))
        return out
    return [str(v) if v is not None else "" for v in values]


def _start(rs: ResultSet, value: Any) -> float | None:
    """Начало водопада: колонка результата (первая строка) или число, которое в нём есть."""
    if value in (None, ""):
        return None
    if isinstance(value, str) and _has_column(rs, value):
        return _number(_cell(rs, rs.rows[0], value)) if rs.rows else None
    number = _number(value)
    if number is None or not _in_result(rs, number):
        raise ToolError("start: укажи колонку результата с начальным значением или число, которое в нём есть")
    return number


def _kpi(rs: ResultSet, spec: dict, out: dict) -> dict:
    """Карточки чисел первой строки; отклонение от базы (план, прошлый год) считает код."""
    if not rs.rows:
        raise ToolError("для kpi нужен хотя бы один ряд данных")
    columns = spec.get("columns") or _numeric_columns(rs) or rs.columns
    unknown = [c for c in columns if not _has_column(rs, c)]
    base = spec.get("base") if isinstance(spec.get("base"), dict) else {}
    deltas = spec.get("deltas") if isinstance(spec.get("deltas"), dict) else {}
    unknown += [c for c in list(base.values()) + list(deltas.values()) if not _has_column(rs, c)]
    if unknown:
        raise ToolError(f"в {rs.id} нет колонок: {', '.join(map(str, unknown))}; есть: {', '.join(rs.columns)}")
    row = rs.rows[0]
    cards = []
    for column in columns[:MAX_SERIES]:
        card: dict[str, Any] = {"label": column, "value": _cell(rs, row, column)}
        if deltas.get(column):
            card["delta"] = _cell(rs, row, deltas[column])
        if base.get(column):
            base_value = _cell(rs, row, base[column])
            card["base"] = base_value
            card["baseLabel"] = base[column]
            value, reference = _number(card["value"]), _number(base_value)
            if value is not None and reference is not None:
                card.setdefault("delta", value - reference)
                if reference:
                    card["deltaPct"] = round((value / reference - 1) * 100, 1)
        cards.append(card)
    out["cards"] = cards
    return out


def _shift_back(value: date, shift: str) -> date | None:
    try:
        if shift == "month":
            return value.replace(year=value.year - 1, month=12) if value.month == 1 else value.replace(month=value.month - 1)
        return value.replace(year=value.year - 1)
    except ValueError:          # 29 февраля и 31-е число — пары нет
        return None


def _period_label(days: list[date], grain: str) -> str:
    lo, hi = min(days), max(days)
    if grain == "month":
        if lo.year == hi.year:
            return f"{MONTHS_SHORT[lo.month - 1]}–{MONTHS_SHORT[hi.month - 1]} {lo.year}" if lo != hi else _fmt_month(lo)
        return f"{_fmt_month(lo)} – {_fmt_month(hi)}"
    if lo == hi:
        return _fmt_day(lo)
    if (lo.year, lo.month) == (hi.year, hi.month):
        return f"{lo.day}–{hi.day} {MONTHS_SHORT[lo.month - 1]} {lo.year}"
    return f"{lo.strftime('%d.%m')}–{_fmt_day(hi)}"


def _compare(rs: ResultSet, spec: dict, out: dict) -> dict:
    """Два периода на одном графике: текущий и прошлый год (или месяц) по одинаковым дням.

    Ось X — колонка дат (день или месяц). Текущий период — последний год (месяц) в данных,
    прошлый — предыдущий; точки сопоставляются по той же дате год (месяц) назад,
    а дни без пары отбрасываются: сравнение всегда по одинаковому числу дней.
    """
    shift = str(spec.get("shift") or "year").strip().lower()
    if shift not in ("year", "month"):
        raise ToolError("compare: shift — year (год к году) или month (месяц к месяцу)")
    x_name = spec.get("x")
    if not x_name:
        x_name = next((c for c in rs.columns if _dates(rs.column(c))), None)
    if not x_name or not _has_column(rs, x_name):
        raise ToolError("compare: укажи в x колонку дат (день или месяц)")
    parsed = _dates(rs.column(x_name))
    if not parsed:
        raise ToolError(f"compare: в «{x_name}» не даты — нужна колонка дня или месяца")
    grain = parsed[0][1]
    if grain == "month" and shift == "month":
        raise ToolError("compare: помесячные данные сравниваются год к году (shift=year)")
    series_spec = spec.get("series") or [c for c in _numeric_columns(rs) if c != x_name][:1]
    if isinstance(series_spec, str):
        series_spec = [series_spec]
    column = series_spec[0] if series_spec else None
    if isinstance(column, dict):
        column = column.get("column") or column.get("name")
    if not column or not _has_column(rs, column):
        raise ToolError(f"compare: укажи в series одну колонку значений из {rs.id}")
    values: dict[date, float | None] = {}
    for raw_x, raw_v in zip(rs.column(x_name), rs.column(column)):
        found = parse_date(raw_x)
        if found is None:
            continue
        if found[0] in values:
            raise ToolError(f"compare: на одну дату несколько строк в {rs.id} — сначала сгруппируй результат по дате")
        values[found[0]] = _number(raw_v)
    key = (lambda d: d.year) if shift == "year" else (lambda d: (d.year, d.month))
    current_key = max(key(d) for d in values)
    current = sorted(d for d in values if key(d) == current_key)
    pairs = [(d, back) for d in current if (back := _shift_back(d, shift)) is not None and back in values]
    if len(pairs) < 2:
        what = "прошлого года" if shift == "year" else "прошлого месяца"
        raise ToolError(f"compare: в {rs.id} нет данных {what} за те же дни — сравнивать не с чем")
    cur_days = [d for d, _b in pairs]
    prev_days = [b for _d, b in pairs]
    cur_label, prev_label = _period_label(cur_days, grain), _period_label(prev_days, grain)
    if grain == "month":
        labels = [MONTHS_SHORT[d.month - 1] for d in cur_days]
        x_title = "Месяц"
    elif shift == "month":
        labels = [str(d.day) for d in cur_days]
        x_title = "День месяца"
    else:
        labels = [d.strftime("%d.%m") for d in cur_days]
        x_title = "Дата"
    cur_values = [values[d] for d in cur_days]
    prev_values = [values[b] for b in prev_days]
    out.update({
        "x": labels, "xTitle": x_title,
        "series": [{"name": cur_label, "values": cur_values}, {"name": prev_label, "values": prev_values}],
        "shift": shift, "grain": grain,
        "period": f"{cur_label} и {prev_label}",
    })
    axis_unit = column_unit(column)
    if axis_unit:
        out["unit"] = axis_unit
    word, gaps = ("мес.", "месяцы") if grain == "month" else ("дн.", "дни")
    periods = [{"label": cur_label, "points": len(cur_days)}, {"label": prev_label, "points": len(prev_days)}]
    aggregate = str(spec.get("total") or "").strip().lower()
    if aggregate in ("sum", "avg"):
        def total(items: list[float | None]) -> float | None:
            present = [v for v in items if v is not None]
            if not present:
                return None
            return sum(present) if aggregate == "sum" else sum(present) / len(present)
        periods[0]["total"], periods[1]["total"] = total(cur_values), total(prev_values)
        out["aggregate"] = aggregate
        if periods[0]["total"] is not None and periods[1]["total"]:
            out["changePct"] = round((periods[0]["total"] / periods[1]["total"] - 1) * 100, 1)
    out["periods"] = periods
    all_current = sum(1 for d in values if key(d) == current_key)
    all_previous = sum(1 for d in values if key(d) != current_key)
    if all_current != len(pairs) or all_previous != len(pairs):
        out["note"] = (f"Периоды выровнены: по {len(pairs)} {word} в каждом — {gaps} без пары в другом периоде "
                       "не показаны и не входят в итоги.")
    return out


# --- «Лёгкий»: простой график без модели ------------------------------------------------

_ID_COLUMN = re.compile(r"(азс|кссс|ksss|номер|код|\bid\b|инн)", re.IGNORECASE)
_TIME_COLUMN = re.compile(r"(год|месяц|недел|день|дата|квартал)", re.IGNORECASE)


def auto_chart(columns: list[str], rows: list[list[Any]], source: str = "r1", title: str = "") -> dict | None:
    """График к ответу «Лёгкого» (ИИ-17): строится кодом из результата, только когда он очевиден.

    Динамика (колонка дат или периода) — линия; сравнение объектов (одна текстовая колонка,
    до 30 строк) — столбцы. Иначе графика нет: таблица ответа остаётся.
    """
    if not (3 <= len(rows) <= MAX_POINTS) or len(columns) < 2:
        return None
    rs = ResultSet(id=source, columns=list(columns), rows=[list(r) for r in rows], source="sql", purpose=title)
    numeric = _numeric_columns(rs)
    values = [c for c in numeric if not _ID_COLUMN.search(c) and not _TIME_COLUMN.search(c)]
    dated = [c for c in rs.columns if _dates(rs.column(c))]
    texts = [c for c in rs.columns if c not in numeric]
    if dated:
        x, kind = dated[0], "line"
    elif len(texts) == 1:
        x, kind = texts[0], "bar"
    elif not texts and len(numeric) - len(values) == 1:
        x = next(c for c in numeric if c not in values)
        kind = "line" if _TIME_COLUMN.search(x) else "bar"
    else:
        return None
    values = [c for c in values if c != x]
    if values:
        # Только ряды в единице первого: разные единицы на одной оси «Лёгкий» не рисует.
        unit = column_unit(values[0])
        values = [c for c in values if column_unit(c) == unit][:3]
    if not values or (kind == "bar" and len(rows) > 30):
        return None              # длинный список объектов читается таблицей, а не столбцами
    try:
        chart = build(rs, {"type": kind, "x": x, "series": values,
                           "title": title or ", ".join(values)}, "c1")
    except ToolError:
        return None
    chart["auto"] = True
    return chart


@tool(
    "create_chart",
    "Построить график из готового результата rN/pN: line (динамика), bar, stacked_bar (структура), "
    "scatter (связь двух показателей), waterfall (вклад драйверов), kpi (карточки чисел; base — колонка "
    "плана или прошлого года, отклонение посчитает код), table, compare (текущий и прошлый период на одном "
    "графике: x — колонка даты или месяца, series — одна колонка, shift — year или month; дни выравниваются "
    "кодом). Данные берутся из результата по именам колонок — числа сюда не переписывай. "
    "Не больше 6 рядов. Не строй график для одного числа.",
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
         "start": {"type": "string", "description": "waterfall: колонка результата с начальным значением"},
         "columns": {"type": "array", "items": {"type": "string"}, "description": "kpi: какие колонки первой строки показать"},
         "base": {"type": "object", "description": "kpi: {колонка: колонка базы} — план или прошлый год; отклонение считает код"},
         "shift": {"type": "string", "enum": ["year", "month"], "description": "compare: год к году или месяц к месяцу"},
         "total": {"type": "string", "enum": ["sum", "avg"], "description": "compare: итог периода — сумма (выручка, литры, чеки) или среднее (доли, средний чек)"},
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
    hint = "График сохранён и будет показан пользователю; в finish его повторно описывать не нужно."
    if chart.get("dropped"):
        hint += (" Часть рядов снята: у них другая единица измерения. Если они важны — "
                 "построй для них отдельный график.")
    return {"chart": summary, "hint": hint}
