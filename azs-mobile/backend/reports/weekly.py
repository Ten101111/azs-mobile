"""СП-02. Еженедельная справка по сети: расчёт модели выпуска.

Источник — только витрина ОХД: dm.data_for_ai_analytic_part_1 (факты по АЗС за
день) и dm.data_for_ai_analytic_part_2 (планы и оценки сервиса по АЗС за день). Решение
владельца 24.09.2026: база KPI платформы и стенд для справок не годятся —
`Source` отказывается строить выпуск по любому другому каталогу. Витрина видна
только с компьютера владельца под VPN, поэтому справку собирает он
(`backend/reports/publish.py`), а сервер лишь показывает готовый выпуск.

Цифры — определения плиток Свода (`backend/summary.py`, только плитки ОХД),
запросы — через тот же `validate()` с областью данных, что Свод и ИИ. Поэтому
справка по сети совпадает со Сводом ОХД за тот же период, а справки ОНПО и
территорий (СП-08) посчитает этот же код с другой областью.

Правила (постановка — блок H бэклога развития ИИ-ассистента):
- неделя пн–вс; сравнения — с прошлой неделей и с неделей −364 дня;
- абсолютные значения — по всей сети, изменения — по сопоставимой базе:
  объекты с данными за все 7 дней в обоих периодах;
- для долей изменение в процентных пунктах;
- зоны внимания — только объекты с 7/7 днями в обеих неделях, пороги Р-15;
  плюс АЗС с негативом в приложении за неделю (СП-07);
- сервис — оценки клиентов и жалобы из part_2, без цели и статуса, пока цели
  нет в витрине (решение владельца 24.09.2026);
- результат — JSON модели выпуска: экран, PDF и XLSX рисуются из него
  и ничего не пересчитывают.
"""
from __future__ import annotations

import os
import statistics
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .. import summary
from ..ai import executor
from ..ai.catalog import CATALOG, Catalog
from ..ai.validator import Scope, validate
from . import fmt, periods

TYPE_CODE = "network_weekly"
FORMULAS_VERSION = "2026-09-24.2"
# Витрина ОХД, по которой только и строится справка (решение владельца 24.09.2026).
MART_FACTS, MART_PLANS = "data_for_ai_analytic_part_1", "data_for_ai_analytic_part_2"
MART_COLUMNS = ("npo", "num_azs", "region_name")
METRIC_CODES = ("fuel_volume", "revenue_total", "ntu_revenue", "ntu_vd", "checks_total",
                "fuel_checks", "ntu_checks", "ntu_avg_check", "conversion")
PLAN_TITLE = "План месяца и требуемый темп"
PLAN_BLOCK = ("fuel_plan", "ntu_plan")
PENDING_BLOCKS = (
    (PLAN_BLOCK, PLAN_TITLE),
    (("ntu_vd",), "Валовой доход НТУ"),
)
CHECKS_COLUMN = "cnt_cheq"  # чеки всего — знаменатель качества сервиса
MSK = timezone(timedelta(hours=3))
ROW_LIMIT = 20_000
NO_ONPO = "Без ОНПО"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Пороги зон внимания и полноты — решение владельца Р-15 (23.09.2026). Значения
# попадают в паспорт выпуска; меняются переменными окружения без правки кода.
THRESHOLDS = {
    "fuelDropPct": _env_float("REPORT_FUEL_DROP_PCT", 20),
    "noSalesDays": int(_env_float("REPORT_NO_SALES_DAYS", 2)),
    "topDrops": int(_env_float("REPORT_TOP_DROPS", 10)),
    "topLeaders": int(_env_float("REPORT_TOP_LEADERS", 5)),
    "completeSharePct": _env_float("REPORT_COMPLETE_SHARE_PCT", 95),
    # Объекты с малой базой (меньше этой доли медианы сети) не ранжируются:
    # на маленьком объёме любое колебание выглядит как «падение на 60 %».
    "minBaseShareOfMedian": _env_float("REPORT_MIN_BASE_SHARE", 0.2),
    # Сервис в зоне внимания (решение владельца 24.09.2026): АЗС с наибольшим
    # числом оценок «1» и «2» за неделю — не меньше порога, не больше списка.
    "negativeMin": int(_env_float("REPORT_NEGATIVE_MIN", 2)),
    "topNegative": int(_env_float("REPORT_TOP_NEGATIVE", 10)),
}


class ReportError(Exception):
    pass


@dataclass
class Source:
    """Витрина и её словарь: факты, ключ объекта, дата, ОНПО и подписи объектов."""

    catalog: Catalog
    run: Callable[[str, int], executor.Result]
    scope: Scope

    def __post_init__(self):
        cat = self.catalog
        if cat.facts_table != MART_FACTS:
            raise ReportError(
                f"Справка строится только по витрине ОХД dm.{MART_FACTS}, а действующий каталог — "
                f"«{cat.facts_table or 'не задан'}». Сборка — на компьютере с доступом к ОХД (VPN): "
                "AI_CATALOG=data/ai_catalog.dwh.json, AI_DB_BACKEND=postgres.")
        facts_columns = cat.tables.get(cat.facts_table, set())
        missing = [c for c in MART_COLUMNS if c not in facts_columns]
        if missing:
            raise ReportError(f"В витрине нет столбцов {', '.join(missing)} — справку не из чего собрать")
        schema = f"{cat.schema}." if cat.schema else ""
        self.facts = f"{schema}{cat.facts_table}"
        self.plans = f"{schema}{cat.plans_table}" if cat.plans_table == MART_PLANS else ""
        self.plan_columns = cat.tables.get(cat.plans_table, set()) if self.plans else set()
        self.key = f"f.{cat.scope_column}"
        self.date = f"f.{cat.date_column}"
        self.join = ""
        self.onpo, self.number, self.region = "f.npo", "f.num_azs", "f.region_name"
        self.tiles = self._tiles()
        fuel = self.tiles.get("fuel_volume")
        if fuel is None:
            raise ReportError("В витрине нет реализации топлива — справку не из чего считать")
        self.fuel_column = f"f.{fuel.needs[0]}"
        self.fuel_unit = fuel.unit

    def _tiles(self) -> dict[str, summary.Tile]:
        columns = self.catalog.column_universe()
        chosen: dict[str, summary.Tile] = {}
        # Только плитки ОХД: определения стенда (STAND_TILES) справке не нужны.
        for tile in summary.TILES:
            if tile.code in chosen or tile.plans:
                continue
            if all(column in columns for column in tile.needs):
                chosen[tile.code] = tile
        return chosen

    def lit(self, day: date) -> str:
        return f"'{day.isoformat()}'" if self.catalog.dialect == "sqlite" else f"DATE '{day.isoformat()}'"

    def within(self, week: periods.Week) -> str:
        return f"{self.date} BETWEEN {self.lit(week.start)} AND {self.lit(week.end)}"

    def span(self, start: date, end: date) -> str:
        return f"{self.date} BETWEEN {self.lit(start)} AND {self.lit(end)}"

    def query(self, sql: str, row_limit: int = ROW_LIMIT) -> list[dict]:
        checked = validate(sql, self.scope, row_limit=row_limit)
        result = self.run(checked.sql, checked.row_limit)
        return [dict(zip(result.columns, row)) for row in result.rows]


def _tile_selects(tiles: list[summary.Tile]) -> str:
    return ",\n       ".join(
        f'ROUND({t.expression}, {t.decimals}) AS "{t.code}"' if t.decimals else f'ROUND({t.expression}) AS "{t.code}"'
        for t in tiles
    )


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def change(now, was, share: bool) -> float | None:
    """Изменение: для долей — в п. п., для остального — в % к базе."""
    now, was = _num(now), _num(was)
    if now is None or was is None:
        return None
    if share:
        return round(now - was, 1)
    if was == 0:
        return None
    return round((now / was - 1) * 100, 1)


# --- запросы ----------------------------------------------------------------

def _totals(src: Source, tiles, parts: dict[str, periods.Week]) -> dict[str, dict]:
    """Итоги сети по нескольким периодам одним запросом (все объекты)."""
    cases = " ".join(f"WHEN {src.within(w)} THEN '{name}'" for name, w in parts.items())
    where = " OR ".join(f"({src.within(w)})" for w in parts.values())
    sql = (f'SELECT CASE {cases} END AS "part", COUNT(DISTINCT {src.key}) AS "stations",\n'
           f"       {_tile_selects(tiles)}\nFROM {src.facts} AS f\nWHERE {where}\nGROUP BY 1")
    return {row["part"]: row for row in src.query(sql)}


def _comparable(src: Source, tiles, a: periods.Week, b: periods.Week, by_onpo: bool) -> dict:
    """Значения двух периодов по объектам с 7/7 днями в обоих (сопоставимая база)."""
    base = (f"WITH d AS (\n"
            f"  SELECT {src.key} AS k,\n"
            f"         COUNT(DISTINCT CASE WHEN {src.within(a)} THEN {src.date} END) AS da,\n"
            f"         COUNT(DISTINCT CASE WHEN {src.within(b)} THEN {src.date} END) AS db\n"
            f"  FROM {src.facts} AS f\n"
            f"  WHERE ({src.within(a)}) OR ({src.within(b)})\n"
            f"  GROUP BY {src.key}\n)\n")
    group = f', {src.onpo} AS "onpo"' if by_onpo else ""
    sql = (base +
           f"SELECT CASE WHEN {src.within(a)} THEN 'a' ELSE 'b' END AS \"part\"{group},\n"
           f"       {_tile_selects(tiles)}\n"
           f"FROM {src.facts} AS f {src.join if by_onpo else ''}\n"
           f"WHERE (({src.within(a)}) OR ({src.within(b)}))\n"
           f"  AND {src.key} IN (SELECT k FROM d WHERE da = 7 AND db = 7)\n"
           f"GROUP BY 1{', 2' if by_onpo else ''}")
    out: dict = {}
    for row in src.query(sql):
        key = (row["part"], (row.get("onpo") or NO_ONPO)) if by_onpo else row["part"]
        out[key] = row
    return out


def _onpo_week(src: Source, tiles, week: periods.Week) -> list[dict]:
    sql = (f'SELECT {src.onpo} AS "onpo", COUNT(DISTINCT {src.key}) AS "stations",\n'
           f"       {_tile_selects(tiles)}\n"
           f"FROM {src.facts} AS f {src.join}\nWHERE {src.within(week)}\nGROUP BY 1")
    return src.query(sql)


def _daily(src: Source, codes: list[str], start: date, end: date, ly_start: date, ly_end: date) -> dict[str, dict]:
    exprs = ",\n       ".join(f'{src.tiles[c].expression} AS "{c}"' for c in codes)
    sql = (f'SELECT {src.date} AS "day",\n       {exprs}\nFROM {src.facts} AS f\n'
           f"WHERE ({src.span(start, end)}) OR ({src.span(ly_start, ly_end)})\nGROUP BY 1")
    return {str(row["day"])[:10]: row for row in src.query(sql, 1000)}


def _stations(src: Source, week, prev, year) -> list[dict]:
    fuel = src.fuel_column
    sql = (f'SELECT {src.key} AS "key", MIN({src.number}) AS "number", MIN({src.region}) AS "region",\n'
           f'       MIN({src.onpo}) AS "onpo",\n'
           f'       COUNT(DISTINCT CASE WHEN {src.within(week)} THEN {src.date} END) AS "days_w",\n'
           f'       COUNT(DISTINCT CASE WHEN {src.within(prev)} THEN {src.date} END) AS "days_p",\n'
           f'       COUNT(DISTINCT CASE WHEN {src.within(year)} THEN {src.date} END) AS "days_y",\n'
           f'       COUNT(DISTINCT CASE WHEN {src.within(week)} AND {fuel} > 0 THEN {src.date} END) AS "sales_w",\n'
           f'       COUNT(DISTINCT CASE WHEN {src.within(prev)} AND {fuel} > 0 THEN {src.date} END) AS "sales_p",\n'
           f'       SUM(CASE WHEN {src.within(week)} THEN {fuel} END) AS "fuel_w",\n'
           f'       SUM(CASE WHEN {src.within(prev)} THEN {fuel} END) AS "fuel_p"{_checks_selects(src, week, prev, year)}\n'
           f"FROM {src.facts} AS f {src.join}\n"
           f"WHERE ({src.within(week)}) OR ({src.within(prev)}) OR ({src.within(year)})\n"
           f"GROUP BY {src.key}")
    return src.query(sql)


def _checks_selects(src: Source, week, prev, year) -> str:
    """Чеки по объектам за три недели — знаменатель качества сервиса; нет столбца — пусто."""
    if CHECKS_COLUMN not in src.catalog.tables.get(src.catalog.facts_table, set()):
        return ""
    column = f"f.{CHECKS_COLUMN}"
    return "".join(f',\n       SUM(CASE WHEN {src.within(w)} THEN {column} END) AS "checks_{name}"'
                   for name, w in (("w", week), ("p", prev), ("y", year)))


def latest_date(src: Source) -> date | None:
    rows = src.query(f'SELECT MAX({src.date}) AS "latest" FROM {src.facts} AS f', 1)
    raw = rows[0]["latest"] if rows else None
    if raw is None:
        return None
    return raw if isinstance(raw, date) and not isinstance(raw, datetime) else date.fromisoformat(str(raw)[:10])


# --- полнота (СП-03) ----------------------------------------------------------

def completeness(stations: list[dict]) -> dict:
    """Доля объектов, работавших всю прошлую неделю, у которых есть все 7 дней отчётной."""
    base = [s for s in stations if int(s.get("days_p") or 0) == 7]
    full = [s for s in base if int(s.get("days_w") or 0) == 7]
    share = round(100.0 * len(full) / len(base), 1) if base else 0.0
    return {"base": len(base), "complete": len(full), "sharePct": share,
            "thresholdPct": THRESHOLDS["completeSharePct"], "ok": bool(base) and share >= THRESHOLDS["completeSharePct"]}


# --- сборка модели ------------------------------------------------------------

def _ident(value) -> str:
    """Номер или КССС объекта строкой: 7005, а не «7005.0» (драйверы отдают числа по-разному)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value if value is not None else "").strip()


def _label(station: dict) -> str:
    number = _ident(station.get("number") or station.get("key"))
    return f"АЗС № {number}" if number else "АЗС"


def _attention(stations: list[dict]) -> dict:
    both = [s for s in stations if int(s.get("days_w") or 0) == 7 and int(s.get("days_p") or 0) == 7]
    prev_volumes = [float(s["fuel_p"]) for s in both if _num(s.get("fuel_p"))]
    median = statistics.median(prev_volumes) if prev_volumes else 0.0
    min_base = median * THRESHOLDS["minBaseShareOfMedian"]
    ranked = []
    for s in both:
        was, now = _num(s.get("fuel_p")), _num(s.get("fuel_w"))
        if not was or now is None or was < min_base:
            continue
        ranked.append({"key": _ident(s["key"]), "label": _label(s), "region": s.get("region") or "",
                       "onpo": s.get("onpo") or NO_ONPO, "fuel": now, "fuelPrev": was,
                       "delta": round((now / was - 1) * 100, 1)})
    drops_all = [r for r in ranked if r["delta"] <= -THRESHOLDS["fuelDropPct"]]
    drops = sorted(drops_all, key=lambda r: r["delta"])[: THRESHOLDS["topDrops"]]
    leaders = sorted((r for r in ranked if r["delta"] > 0), key=lambda r: -r["delta"])[: THRESHOLDS["topLeaders"]]
    no_sales = []
    for s in both:
        idle = 7 - int(s.get("sales_w") or 0)
        if idle >= THRESHOLDS["noSalesDays"] and int(s.get("sales_p") or 0) >= 6:
            no_sales.append({"key": _ident(s["key"]), "label": _label(s), "region": s.get("region") or "",
                             "onpo": s.get("onpo") or NO_ONPO, "idleDays": idle})
    no_sales.sort(key=lambda r: (-r["idleDays"], r["label"]))
    return {"drops": drops, "dropsTotal": len(drops_all), "dropKeys": [r["key"] for r in drops_all],
            "noSales": no_sales, "leaders": leaders, "minBase": round(min_base, 1)}


def _metrics(src, tiles, totals, nn, yy) -> list[dict]:
    rows = []
    for tile in tiles:
        share = tile.unit == "%"
        rows.append({
            "code": tile.code, "title": tile.title, "unit": tile.unit, "decimals": tile.decimals,
            "isShare": share,
            "value": _num(totals.get("w", {}).get(tile.code)),
            "prev": _num(totals.get("p", {}).get(tile.code)),
            "deltaPrev": change(nn.get("a", {}).get(tile.code), nn.get("b", {}).get(tile.code), share),
            "lastYear": _num(totals.get("y", {}).get(tile.code)),
            "deltaYear": change(yy.get("a", {}).get(tile.code), yy.get("b", {}).get(tile.code), share),
        })
    return rows


def _onpo_rows(src, week_rows, nn, yy) -> list[dict]:
    out = []
    for row in week_rows:
        name = row.get("onpo") or NO_ONPO
        entry = {"name": name, "stations": int(row.get("stations") or 0)}
        for code, key in (("fuel_volume", "fuel"), ("ntu_revenue", "ntu")):
            if code not in src.tiles:
                continue
            entry[key] = _num(row.get(code))
            entry[f"{key}DeltaPrev"] = change(nn.get(("a", name), {}).get(code), nn.get(("b", name), {}).get(code), False)
            entry[f"{key}DeltaYear"] = change(yy.get(("a", name), {}).get(code), yy.get(("b", name), {}).get(code), False)
        if "conversion" in src.tiles:
            entry["conversion"] = _num(row.get("conversion"))
        out.append(entry)
    out.sort(key=lambda r: -(r.get("fuel") or 0))
    return out


def _dynamics(src, week: periods.Week) -> dict:
    weeks = periods.trailing(week, 8)
    ly_weeks = [periods.last_year(w) for w in weeks]
    codes = [c for c in ("fuel_volume", "ntu_revenue") if c in src.tiles]
    daily = _daily(src, codes, weeks[0].start, weeks[-1].end, ly_weeks[0].start, ly_weeks[-1].end)

    def weekly(ws, code):
        values = []
        for w in ws:
            days = [daily.get(d.isoformat()) for d in w.days()]
            present = [_num(d.get(code)) for d in days if d and _num(d.get(code)) is not None]
            values.append(round(sum(present), 1) if present else None)
        return values

    charts = []
    for code in codes:
        tile = src.tiles[code]
        charts.append({
            "code": code, "title": f"{tile.title} по неделям, {fmt.unit(tile.unit)}", "unit": tile.unit,
            "x": [w.short for w in weeks],
            "series": [{"name": str(weeks[-1].end.year), "values": weekly(weeks, code)},
                       {"name": f"{ly_weeks[-1].end.year}, те же недели", "values": weekly(ly_weeks, code)}],
        })
    return {"weeks": [w.as_dict() for w in weeks], "charts": charts}


# --- план месяца (СП-07, решение владельца 24.09.2026) ------------------------
# План в витрине — по АЗС на каждый день (dm.data_for_ai_analytic_part_2). План
# месяца — сумма дней месяца; выполнение считается по объектам, у которых план на
# месяц есть: факт и план — по одним и тем же АЗС. Отставание — % плана минус %
# прошедших дней (БТ, «светофор»): не больше 3 п. п. — норма, 3–8 — внимание,
# больше 8 — критично; сервиса в витрине справки пока нет, статус — только по НТУ.
PLAN_METRICS = (
    # код, название, единица, знаков, столбцы факта, выражение факта, выражение плана
    ("fuel", "Реализация топлива", "т", 1, ("sum_weight",), "f.sum_weight",
     "COALESCE(p.plan_weights_b2c, 0) + COALESCE(p.plan_weights_b2b, 0)", ("plan_weights_b2c", "plan_weights_b2b")),
    ("ntu", "Выручка НТУ", "руб", 0, ("sum_receipt_netto_ntu",), "f.sum_receipt_netto_ntu",
     "COALESCE(p.plan_ntu_revenue, 0)", ("plan_ntu_revenue",)),
    ("vd", "Валовой доход НТУ", "руб", 0, ("vd_ntu",), "f.vd_ntu",
     "COALESCE(p.plan_ntu_vd, 0)", ("plan_ntu_vd",)),
)
PLAN_STATUS = ((3.0, "норма"), (8.0, "внимание"))


def plan_status(gap: float | None) -> str | None:
    if gap is None:
        return None
    lag = -gap
    for limit, word in PLAN_STATUS:
        if lag <= limit:
            return word
    return "критично"


def _plan_metrics(src: Source) -> list[tuple]:
    facts = src.catalog.tables.get(src.catalog.facts_table, set())
    return [m for m in PLAN_METRICS if all(c in facts for c in m[4]) and all(c in src.plan_columns for c in m[7])]


def _plan_month(src: Source, metrics: list[tuple], start: date, end: date, to_date: date) -> dict | None:
    pkey = f"p.{src.catalog.scope_column_of(src.catalog.plans_table)}"
    pdate = f"p.{src.catalog.date_column}"
    plan_sums = ",\n       ".join(
        f'SUM(CASE WHEN {pdate} <= {src.lit(to_date)} THEN {plan} END) AS "{code}_td",\n'
        f'       SUM({plan}) AS "{code}_m"' for code, *_, plan, _cols in metrics)
    plans = src.query(f'SELECT {pkey} AS "key", COUNT(DISTINCT {pdate}) AS "days",\n       {plan_sums}\n'
                      f"FROM {src.plans} AS p\nWHERE {pdate} BETWEEN {src.lit(start)} AND {src.lit(end)}\n"
                      f"GROUP BY {pkey}")
    # В той же витрине лежат оценки сервиса: строка без плана — не план.
    plans = [r for r in plans if any((_num(r.get(f"{code}_m")) or 0) > 0 for code, *_ in metrics)]
    if not plans:
        return None
    any_plan = " + ".join(f"({plan})" for code, *_, plan, _cols in metrics)
    covered = src.query(f'SELECT COUNT(DISTINCT CASE WHEN {any_plan} > 0 THEN {pdate} END) AS "days"\n'
                        f"FROM {src.plans} AS p\nWHERE {pdate} BETWEEN {src.lit(start)} AND {src.lit(end)}", 1)
    fact_sums = ",\n       ".join(f'SUM({expr}) AS "{code}"' for code, _t, _u, _d, _c, expr, *_ in metrics)
    facts = src.query(f'SELECT {src.key} AS "key", MIN({src.onpo}) AS "onpo",\n       {fact_sums}\n'
                      f"FROM {src.facts} AS f\nWHERE {src.span(start, to_date)}\nGROUP BY {src.key}")
    by_key = {_ident(r["key"]): r for r in facts}
    total_days = (end - start).days + 1
    passed = (to_date - start).days + 1
    closed = to_date >= end
    full_plan = int((covered[0]["days"] if covered else 0) or 0) >= total_days
    days_pct = round(100.0 * passed / total_days, 1)

    def summarize(rows: list[dict]) -> dict:
        out = {}
        for code, *_ in metrics:
            with_plan = [r for r in rows if (_num(r.get(f"{code}_m")) or 0) > 0]
            fact = sum(_num(by_key.get(_ident(r["key"]), {}).get(code)) or 0 for r in with_plan)
            plan_m = sum(_num(r.get(f"{code}_m")) or 0 for r in with_plan) if full_plan else None
            plan_td = sum(_num(r.get(f"{code}_td")) or 0 for r in with_plan)
            out[code] = {"fact": fact, "planMonth": plan_m, "planToDate": plan_td, "stations": len(with_plan)}
        return out

    network = summarize(plans)
    rows = []
    for code, title, unit, decimals, *_ in metrics:
        v = network[code]
        pct_m = round(100.0 * v["fact"] / v["planMonth"], 1) if v["planMonth"] else None
        pct_td = round(100.0 * v["fact"] / v["planToDate"], 1) if v["planToDate"] else None
        gap = round(pct_m - (100.0 if closed else days_pct), 1) if pct_m is not None else None
        left = total_days - passed
        rows.append({
            "code": code, "title": title, "unit": unit, "decimals": decimals, "stations": v["stations"],
            "fact": round(v["fact"], decimals), "planMonth": round(v["planMonth"], decimals) if v["planMonth"] else None,
            "planToDate": round(v["planToDate"], decimals) if v["planToDate"] else None,
            "pctMonth": pct_m, "pctToDate": pct_td, "gapPp": gap,
            "needPerDay": round((v["planMonth"] - v["fact"]) / left, decimals)
            if v["planMonth"] and left > 0 and not closed else None,
            "pacePerDay": round(v["fact"] / passed, decimals) if passed else None,
            "status": plan_status(gap) if code == "ntu" else None,
        })
    onpo_rows = []
    groups: dict[str, list[dict]] = {}
    for r in plans:
        name = (by_key.get(_ident(r["key"]), {}).get("onpo") or NO_ONPO)
        groups.setdefault(name, []).append(r)
    for name, members in groups.items():
        part = summarize(members)
        entry = {"name": name}
        for code, *_ in metrics:
            v = part[code]
            base = v["planMonth"] if full_plan else v["planToDate"]
            pct = round(100.0 * v["fact"] / base, 1) if base else None
            entry[f"{code}Pct"] = pct
            entry[f"{code}Gap"] = round(pct - (100.0 if closed or not full_plan else days_pct), 1) if pct is not None else None
        entry["status"] = plan_status(entry.get("ntuGap")) if full_plan else None
        onpo_rows.append(entry)
    onpo_rows.sort(key=lambda e: (e.get("ntuGap") is None, e.get("ntuGap") or 0))
    return {"month": start.strftime("%Y-%m"), "label": periods.month_label(start),
            "labelGen": periods.month_label(start, "gen"), "from": start.isoformat(), "to": to_date.isoformat(),
            "end": end.isoformat(), "closed": closed, "daysPassed": passed, "daysTotal": total_days,
            "daysPct": 100.0 if closed else days_pct, "planComplete": full_plan, "rows": rows, "onpo": onpo_rows}


def _plan(src: Source, week: periods.Week) -> tuple[dict | None, str]:
    """Блок плана по месяцам недели; вторым — причина, если блока нет."""
    if not src.plans:
        return None, f"в каталоге нет витрины планов dm.{MART_PLANS}"
    metrics = _plan_metrics(src)
    if not metrics:
        return None, "в витрине планов нет столбцов плана по топливу, НТУ и ВД НТУ"
    months = []
    for start, end in periods.months_of(week):
        month = _plan_month(src, metrics, start, end, min(end, week.end))
        if month:
            months.append(month)
    if not months:
        return None, f"в витрине ОХД нет плана на {periods.month_label(week.end)}"
    note = ""
    if any(not m["planComplete"] and not m["closed"] for m in months):
        note = "план на месяц загружен не на все дни — выполнение показано к плану на дату, темп не считается"
    return {"months": months, "note": note}, ""


def _plan_headline(plan: dict | None) -> list[str]:
    if not plan:
        return []
    out = []
    for month in plan["months"]:
        rows = {r["code"]: r for r in month["rows"]}
        ntu, fuel = rows.get("ntu"), rows.get("fuel")
        if month["closed"]:
            parts = [f"по {what} — {fmt.number(r['pctMonth'], 1)} %"
                     for what, r in (("выручке НТУ", ntu), ("топливу", fuel)) if r and r["pctMonth"] is not None]
            if parts:
                out.append(f"Итоги {month['labelGen']}: план выполнен {' и '.join(parts)}.")
            continue
        if ntu and ntu["pctMonth"] is not None:
            text = (f"План {month['labelGen']} по выручке НТУ выполнен на {fmt.number(ntu['pctMonth'], 1)} % "
                    f"при {fmt.number(month['daysPct'], 1)} % прошедших дней ({fmt.delta(ntu['gapPp'], share=True)})")
            if ntu["needPerDay"] is not None and ntu["needPerDay"] > 0:
                text += f"; чтобы выполнить план, нужно {fmt.compact(ntu['needPerDay'], 'руб')} в день"
            out.append(text + ".")
        elif ntu and ntu["pctToDate"] is not None:
            out.append(f"План {month['labelGen']} по выручке НТУ на дату выполнен на {fmt.number(ntu['pctToDate'], 1)} %.")
        if fuel and fuel["pctMonth"] is not None:
            out.append(f"План по топливу — {fmt.number(fuel['pctMonth'], 1)} % ({fmt.delta(fuel['gapPp'], share=True)}).")
    return out


# --- сервис (СП-07, решение владельца 24.09.2026) -----------------------------
# Оценки клиентов в мобильном приложении (1–5), негатив по категориям и жалобы ЕГЛ
# лежат в той же dm.data_for_ai_analytic_part_2 — по АЗС за день. Средняя оценка
# считается по всем оценкам, без правил методики (категории по роли, исключения),
# поэтому это не официальный уровень сервиса. Цели в витрине нет — статуса
# «выполнен» нет; он появится, когда цель придёт в витрине. Качество сервиса —
# (негатив + жалобы ЕГЛ) на 100 тыс. чеков, меньше — лучше. Неделя — к прошлой
# неделе и к году: значения по всей сети, изменения по сопоставимой базе, как вся
# справка; плюс итог с начала месяца — методика итожит сервис за месяц.
SERVICE_TITLE = "Сервис: оценки в приложении и жалобы"
RATING_COLUMNS = ("all_rate", "rate_cnt_1", "rate_cnt_2", "rate_cnt_3", "rate_cnt_4", "rate_cnt_5")
COMPLAINTS_COLUMN = "cnt_num_compl"
NEGATIVE_CATEGORIES = (
    ("rate_pers_act_azs_cnt", "Обслуживание на кассе"),
    ("rate_clear_azs_cnt", "Чистота АЗС"),
    ("rate_tech_azs_cnt", "Техническое состояние"),
    ("rate_refueller_azs_cnt", "Работа заправщика"),
    ("rate_asort_azs_cnt", "Ассортимент и качество НТУ"),
    ("rate_app_mob_azs_cnt", "Мобильное приложение"),
    ("rate_loy_card_azs_cnt", "Программа лояльности"),
    ("rate_other_azs_cnt", "Другое"),
)
SERVICE_METRICS = (
    # код, название, единица, знаков, изменение: abs — разность, pct — %, pp — п. п.
    ("avg", "Средняя оценка в приложении", "", 3, "abs"),
    ("ratings", "Оценок в приложении", "шт", 0, "pct"),
    ("negative", "Негативные оценки («1» и «2»)", "шт", 0, "abs"),
    ("negativeShare", "Доля негативных оценок", "%", 2, "pp"),
    ("complaints", "Жалобы ЕГЛ", "шт", 0, "abs"),
    ("quality", "Качество сервиса, на 100 тыс. чеков", "", 2, "abs"),
)
SERVICE_RULE = ("Сервис — оценки клиентов в мобильном приложении (1–5) и жалобы ЕГЛ из витрины ОХД. "
                "Средняя оценка — по всем оценкам, без правил методики по категориям и ролям, поэтому это не "
                "официальный уровень сервиса; цели в витрине нет — статуса «выполнен» нет. Негатив — оценки «1» и «2». "
                "Качество сервиса — (негатив + жалобы ЕГЛ) на 100 тыс. чеков, меньше — лучше.")


def _service_values(sums: dict) -> dict:
    """Показатели сервиса из сумм; нет ни одной оценки — нет данных (а не нули)."""
    ratings = sums.get("ratings") or 0.0
    if ratings <= 0:
        return {code: None for code, *_ in SERVICE_METRICS}
    negative = (sums.get("r1") or 0.0) + (sums.get("r2") or 0.0)
    complaints = sums.get("complaints")
    checks = sums.get("checks") or 0.0
    total = sum(k * (sums.get(f"r{k}") or 0.0) for k in range(1, 6))
    return {
        "avg": round(total / ratings, 3),
        "ratings": int(round(ratings)),
        "negative": int(round(negative)),
        "negativeShare": round(100.0 * negative / ratings, 2),
        "complaints": int(round(complaints)) if complaints is not None else None,
        "quality": round(100000.0 * (negative + (complaints or 0.0)) / checks, 2) if checks else None,
    }


def _service_delta(kind: str, decimals: int, now, was):
    if now is None or was is None:
        return None
    if kind == "pct":
        return change(now, was, False)
    return round(float(now) - float(was), decimals)


def _service_sums_sql(src: Source) -> list[str]:
    sums = ['SUM(p.all_rate) AS "ratings"'] + [f'SUM(p.rate_cnt_{k}) AS "r{k}"' for k in range(1, 6)]
    if COMPLAINTS_COLUMN in src.plan_columns:
        sums.append(f'SUM(p.{COMPLAINTS_COLUMN}) AS "complaints"')
    sums += [f'SUM(p.{column}) AS "{column}"' for column, _ in NEGATIVE_CATEGORIES if column in src.plan_columns]
    return sums


def _service_rows(src: Source, parts: dict[str, periods.Week]) -> list[dict]:
    """Суммы оценок по объектам за каждую из недель — одним запросом."""
    pkey = f"p.{src.catalog.scope_column_of(src.catalog.plans_table)}"
    pdate = f"p.{src.catalog.date_column}"
    within = {name: f"{pdate} BETWEEN {src.lit(w.start)} AND {src.lit(w.end)}" for name, w in parts.items()}
    cases = " ".join(f"WHEN {cond} THEN '{name}'" for name, cond in within.items())
    where = " OR ".join(f"({cond})" for cond in within.values())
    sql = (f'SELECT {pkey} AS "key", CASE {cases} END AS "part",\n       ' + ",\n       ".join(_service_sums_sql(src))
           + f"\nFROM {src.plans} AS p\nWHERE {where}\nGROUP BY 1, 2")
    return src.query(sql)


def _service_span(src: Source, start: date, end: date) -> dict:
    """Итог сервиса по сети за отрезок дней (с начала месяца)."""
    pdate = f"p.{src.catalog.date_column}"
    rows = src.query(f"SELECT " + ",\n       ".join(_service_sums_sql(src)) +
                     f"\nFROM {src.plans} AS p\nWHERE {pdate} BETWEEN {src.lit(start)} AND {src.lit(end)}", 1)
    sums = {k: _num(v) for k, v in (rows[0] if rows else {}).items()}
    if CHECKS_COLUMN in src.catalog.tables.get(src.catalog.facts_table, set()):
        checks = src.query(f'SELECT SUM(f.{CHECKS_COLUMN}) AS "checks" FROM {src.facts} AS f '
                           f"WHERE {src.span(start, end)}", 1)
        sums["checks"] = _num(checks[0]["checks"]) if checks else None
    return _service_values(sums)


def _add(sums: dict, row: dict) -> None:
    for k, v in row.items():
        if k not in ("key", "part"):
            sums[k] = (sums.get(k) or 0.0) + (_num(v) or 0.0)


def _service(src: Source, stations: list[dict], week: periods.Week, prev: periods.Week,
             year: periods.Week) -> tuple[dict | None, str]:
    """Блок сервиса и АЗС с негативом; вторым — причина, если блока нет."""
    if not src.plans:
        return None, f"в каталоге нет витрины dm.{MART_PLANS} с оценками клиентов"
    if not all(c in src.plan_columns for c in RATING_COLUMNS):
        return None, "в витрине нет оценок клиентов в приложении"
    by_part: dict[str, dict[str, dict]] = {"w": {}, "p": {}, "y": {}}
    for row in _service_rows(src, {"w": week, "p": prev, "y": year}):
        by_part.setdefault(row["part"], {})[_ident(row["key"])] = row
    if not sum(_num(r.get("ratings")) or 0 for r in by_part["w"].values()):
        return None, f"в витрине нет оценок клиентов за неделю {week.label}"
    info = {_ident(s["key"]): s for s in stations}
    has_checks = CHECKS_COLUMN in src.catalog.tables.get(src.catalog.facts_table, set())

    def total(part: str, keys: set[str] | None = None) -> dict:
        sums: dict = {}
        for key, row in by_part.get(part, {}).items():
            if keys is None or key in keys:
                _add(sums, row)
        if has_checks:
            sums["checks"] = sum(_num(s.get(f"checks_{part}")) or 0 for k, s in info.items()
                                 if keys is None or k in keys)
        return _service_values(sums)

    def full(a: str, b: str) -> set[str]:
        return {k for k, s in info.items() if int(s.get(f"days_{a}") or 0) == 7 and int(s.get(f"days_{b}") or 0) == 7}

    nn, yy = full("w", "p"), full("w", "y")
    now, was, last = total("w"), total("p"), total("y")
    nn_a, nn_b, yy_a, yy_b = total("w", nn), total("p", nn), total("w", yy), total("y", yy)
    metrics = []
    for code, title, unit, decimals, kind in SERVICE_METRICS:
        if now[code] is None and code in ("complaints", "quality"):
            continue  # нет столбца жалоб или чеков — строки нет
        metrics.append({"code": code, "title": title, "unit": unit, "decimals": decimals, "deltaKind": kind,
                        "value": now[code], "prev": was[code], "lastYear": last[code],
                        "deltaPrev": _service_delta(kind, decimals, nn_a[code], nn_b[code]),
                        "deltaYear": _service_delta(kind, decimals, yy_a[code], yy_b[code])})

    week_sums: dict = {}
    prev_sums: dict = {}
    for row in by_part["w"].values():
        _add(week_sums, row)
    for row in by_part["p"].values():
        _add(prev_sums, row)
    categories = []
    for column, title in NEGATIVE_CATEGORIES:
        if column not in src.plan_columns:
            continue
        count = int(round(week_sums.get(column) or 0))
        before = int(round(prev_sums.get(column) or 0)) if was["avg"] is not None else None
        if count or before:
            categories.append({"code": column, "title": title, "week": count, "prev": before})
    categories.sort(key=lambda c: -c["week"])

    groups: dict[str, set[str]] = {}
    for key, row in by_part["w"].items():
        if (_num(row.get("ratings")) or 0) > 0:
            groups.setdefault((info.get(key) or {}).get("onpo") or NO_ONPO, set()).add(key)
    onpo = []
    for name, keys in groups.items():
        v = total("w", keys)
        a, b = total("w", keys & nn), total("p", keys & nn)
        onpo.append({"name": name, "stations": len(keys), **v,
                     "avgDeltaPrev": _service_delta("abs", 3, a["avg"], b["avg"])})
    onpo.sort(key=lambda r: (r["avg"] is None, r["avg"] if r["avg"] is not None else 0, r["name"]))

    flagged = []
    for key, row in by_part["w"].items():
        negative = int(round((_num(row.get("r1")) or 0) + (_num(row.get("r2")) or 0)))
        if negative < THRESHOLDS["negativeMin"]:
            continue
        station = info.get(key) or {"key": key}
        top = max(((int(round(_num(row.get(c)) or 0)), t) for c, t in NEGATIVE_CATEGORIES if c in row),
                  default=(0, ""))
        v = _service_values({**{k: _num(row.get(k)) for k in ("ratings", "r1", "r2", "r3", "r4", "r5", "complaints")},
                             "checks": _num(station.get("checks_w"))})
        flagged.append({"key": key, "label": _label(station), "region": station.get("region") or "",
                        "onpo": station.get("onpo") or NO_ONPO, "negative": negative, "ratings": v["ratings"],
                        "avg": v["avg"], "complaints": v["complaints"], "category": top[1] if top[0] > 0 else ""})
    flagged.sort(key=lambda r: (-r["negative"], r["avg"] if r["avg"] is not None else 5.0, r["label"]))

    months = []
    for start, end in periods.months_of(week):
        to_date = min(end, week.end)
        v = _service_span(src, start, to_date)
        if v["avg"] is None:
            continue
        months.append({"month": start.strftime("%Y-%m"), "label": periods.month_label(start),
                       "labelGen": periods.month_label(start, "gen"), "from": start.isoformat(),
                       "to": to_date.isoformat(), "closed": to_date >= end,
                       "daysPassed": (to_date - start).days + 1, "daysTotal": (end - start).days + 1, **v})
    return {"metrics": metrics, "months": months, "onpo": onpo, "categories": categories,
            "negative": flagged[: THRESHOLDS["topNegative"]], "negativeTotal": len(flagged),
            "negativeKeys": [r["key"] for r in flagged]}, ""


def _service_headline(service: dict | None) -> list[str]:
    if not service:
        return []
    rows = {r["code"]: r for r in service["metrics"]}
    avg = rows.get("avg")
    if not avg or avg["value"] is None:
        return []
    text = f"Средняя оценка в приложении за неделю — {fmt.number(avg['value'], 3)}"
    if avg["deltaPrev"] is not None:
        text += f" ({fmt.signed(avg['deltaPrev'], 3)} к прошлой неделе)"
    parts = [f"{what} — {fmt.number(rows[code]['value'])}" for code, what in
             (("negative", "негативных оценок"), ("complaints", "жалоб ЕГЛ"))
             if rows.get(code) and rows[code]["value"] is not None]
    return [text + (f"; {', '.join(parts)}" if parts else "") + "."]


def zone_keys(attention: dict) -> set[str]:
    """Объекты зоны внимания: падение топлива, дни без продаж и негатив в приложении."""
    return (set(attention.get("dropKeys") or [r["key"] for r in attention.get("drops", [])])
            | {r["key"] for r in attention.get("noSales", [])}
            | set(attention.get("negativeKeys") or []))


def _headline(metrics, onpo, attention, week, holidays) -> list[str]:
    by_code = {m["code"]: m for m in metrics}
    out = []
    for code, what in (("fuel_volume", "Реализация топлива"), ("ntu_revenue", "Выручка НТУ")):
        m = by_code.get(code)
        if not m or m["value"] is None:
            continue
        amount = fmt.compact(m["value"], m["unit"]) if m["unit"] == "руб" else fmt.value(m["value"], m["unit"], m["decimals"])
        parts = []
        if m["deltaPrev"] is not None:
            parts.append(f"{fmt.delta(m['deltaPrev'])} к прошлой неделе")
        if m["deltaYear"] is not None:
            parts.append(f"{fmt.delta(m['deltaYear'])} к той же неделе прошлого года")
        tail = f": {' и '.join(parts)}" if parts else ""
        out.append(f"{what} за неделю — {amount}{tail}.")
    ranked = [r for r in onpo if r.get("fuelDeltaPrev") is not None and r["name"] != NO_ONPO]
    if len(ranked) >= 2:
        best = max(ranked, key=lambda r: r["fuelDeltaPrev"])
        worst = min(ranked, key=lambda r: r["fuelDeltaPrev"])
        if best["name"] != worst["name"]:
            out.append(f"По реализации топлива к прошлой неделе лучше всех ОНПО «{best['name']}» — "
                       f"{fmt.delta(best['fuelDeltaPrev'])}, слабее всех «{worst['name']}» — {fmt.delta(worst['fuelDeltaPrev'])}.")
    zone = len(zone_keys(attention))
    if zone:
        text = (f"В зоне внимания {zone} {_objects(zone)}: падение топлива на "
                f"{fmt.number(THRESHOLDS['fuelDropPct'])} % и больше — {attention['dropsTotal']}, "
                f"без продаж {fmt.days(THRESHOLDS['noSalesDays'])} и больше — {len(attention['noSales'])}")
        if "negativeTotal" in attention:
            text += (f", {fmt.number(THRESHOLDS['negativeMin'])} и больше негативных оценок в приложении — "
                     f"{attention['negativeTotal']}")
        out.append(text + ".")
    else:
        out.append("Объектов в зоне внимания нет.")
    if holidays:
        names = ", ".join(sorted({h["name"] for h in holidays}))
        out.append(f"В сравниваемых неделях есть праздничные дни ({names}) — сравнение условно.")
    return out


def _insert_plan(lines: list[str], plan_lines: list[str]) -> list[str]:
    """Фразы плана — после топлива и НТУ, перед ОНПО и зоной внимания."""
    head = 2 if len(lines) >= 2 else len(lines)
    return lines[:head] + plan_lines + lines[head:]


def _objects(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "объект"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "объекта"
    return "объектов"


def build(week: periods.Week, *, catalog: Catalog | None = None,
          run: Callable[[str, int], executor.Result] | None = None,
          scope: Scope | None = None, now: datetime | None = None,
          require_complete: bool = True) -> dict:
    """Модель выпуска справки по сети за неделю `week`.

    При неполных данных (последний день витрины раньше воскресенья или доля
    объектов с 7/7 днями ниже порога) — ReportError с причиной: публиковать
    по неполным данным нельзя (Р-15).
    """
    started = time.monotonic()
    src = Source(catalog or CATALOG, run or executor.run, scope or Scope.all_network())
    prev, year = periods.previous(week), periods.last_year(week)
    latest = latest_date(src)
    if latest is None:
        raise ReportError("В витрине нет данных")
    if latest < week.end:
        raise ReportError(f"Нет данных за {week.end:%d.%m.%Y}: последний день в витрине — {latest:%d.%m.%Y}")
    stations = _stations(src, week, prev, year)
    complete = completeness(stations)
    if require_complete and not complete["ok"]:
        raise ReportError(
            f"Данные неполные: все 7 дней есть у {fmt.number(complete['sharePct'], 1)} % объектов "
            f"при пороге {fmt.number(complete['thresholdPct'])} %")

    tiles = [src.tiles[c] for c in METRIC_CODES if c in src.tiles]
    totals = _totals(src, tiles, {"w": week, "p": prev, "y": year})
    nn = _comparable(src, tiles, week, prev, by_onpo=False)
    yy = _comparable(src, tiles, week, year, by_onpo=False)
    onpo_tiles = [src.tiles[c] for c in ("fuel_volume", "ntu_revenue", "conversion") if c in src.tiles]
    onpo_week = _onpo_week(src, onpo_tiles, week)
    onpo_nn = _comparable(src, onpo_tiles, week, prev, by_onpo=True)
    onpo_yy = _comparable(src, onpo_tiles, week, year, by_onpo=True)

    metrics = _metrics(src, tiles, totals, nn, yy)
    onpo = _onpo_rows(src, onpo_week, onpo_nn, onpo_yy)
    plan, plan_missing = _plan(src, week)
    service, service_missing = _service(src, stations, week, prev, year)
    attention = _attention(stations)
    if service:
        attention.update(negative=service.pop("negative"), negativeTotal=service.pop("negativeTotal"),
                         negativeKeys=service.pop("negativeKeys"))
    dynamics = _dynamics(src, week)
    holidays = week.holidays() + prev.holidays() + year.holidays()

    both_nn = sum(1 for s in stations if int(s.get("days_w") or 0) == 7 and int(s.get("days_p") or 0) == 7)
    both_yy = sum(1 for s in stations if int(s.get("days_w") or 0) == 7 and int(s.get("days_y") or 0) == 7)
    seen_nn = sum(1 for s in stations if int(s.get("days_w") or 0) or int(s.get("days_p") or 0))
    seen_yy = sum(1 for s in stations if int(s.get("days_w") or 0) or int(s.get("days_y") or 0))
    pending = [title for codes, title in PENDING_BLOCKS
               if codes != PLAN_BLOCK and not all(c in src.tiles for c in codes)]
    if service is None:
        pending.insert(0, SERVICE_TITLE)
    if plan is None:
        pending.insert(0, PLAN_TITLE)
    generated = (now or datetime.now(timezone.utc)).astimezone(MSK)
    run_id = uuid.uuid4().hex[:12]
    fuel_tile = src.tiles["fuel_volume"]

    return {
        "type": TYPE_CODE,
        "title": "Справка по сети",
        "level": "network",
        "scopeKey": "network",
        "scopeLabel": "Вся сеть",
        "week": week.as_dict(),
        "compare": {"prev": prev.as_dict(), "lastYear": year.as_dict()},
        "headline": _insert_plan(_headline(metrics, onpo, attention, week, holidays),
                                 _plan_headline(plan) + _service_headline(service)),
        "plan": plan,
        "service": service,
        "metrics": metrics,
        "dynamics": dynamics,
        "onpo": onpo,
        "onpoTotal": {
            "stations": int(totals.get("w", {}).get("stations") or 0),
            "fuel": next((m["value"] for m in metrics if m["code"] == "fuel_volume"), None),
            "fuelDeltaPrev": next((m["deltaPrev"] for m in metrics if m["code"] == "fuel_volume"), None),
            "fuelDeltaYear": next((m["deltaYear"] for m in metrics if m["code"] == "fuel_volume"), None),
            "ntu": next((m["value"] for m in metrics if m["code"] == "ntu_revenue"), None),
            "ntuDeltaPrev": next((m["deltaPrev"] for m in metrics if m["code"] == "ntu_revenue"), None),
            "ntuDeltaYear": next((m["deltaYear"] for m in metrics if m["code"] == "ntu_revenue"), None),
            "conversion": next((m["value"] for m in metrics if m["code"] == "conversion"), None),
        },
        "fuelUnit": fuel_tile.unit,
        "attention": attention,
        "pending": pending,
        "passport": {
            "week": week.as_dict(), "prev": prev.as_dict(), "lastYear": year.as_dict(),
            "stationsWeek": int(totals.get("w", {}).get("stations") or 0),
            "comparablePrev": both_nn, "excludedPrev": seen_nn - both_nn,
            "comparableYear": both_yy, "excludedYear": seen_yy - both_yy,
            "completeness": complete,
            "latestDate": latest.isoformat(),
            "source": f"Витрина ОХД: dm.{MART_FACTS} (факты по АЗС за день)"
                      + (f", dm.{MART_PLANS} (планы и оценки сервиса)" if src.plans else ""),
            "fuelUnitNote": f"Реализация топлива — в {'тоннах' if fuel_tile.unit == 'т' else 'литрах'}",
            "formulasVersion": FORMULAS_VERSION,
            "runId": run_id,
            "holidays": holidays,
            "planNote": (plan or {}).get("note") or (f"Блока плана нет: {plan_missing}." if plan is None else ""),
            "serviceNote": f"Блока сервиса нет: {service_missing}." if service is None else "",
            "thresholds": dict(THRESHOLDS),
            "generatedAt": generated.isoformat(timespec="seconds"),
            "durationMs": int((time.monotonic() - started) * 1000),
            "confidential": True,
            "rules": [
                "Неделя — с понедельника по воскресенье; прошлый год — та же неделя со сдвигом на 364 дня, дни недели совпадают.",
                "Значения — по всей сети; изменения — по сопоставимой базе: объекты с данными за все 7 дней в обоих периодах.",
                "Для долей изменение — в процентных пунктах.",
                "Зоны внимания — только объекты с 7/7 днями в обеих неделях; объекты с малой базой не ранжируются.",
                "План месяца — сумма дневных планов витрины; выполнение — по объектам, у которых есть план на месяц; "
                "отставание — % плана минус % прошедших дней: до 3 п. п. — норма, 3–8 — внимание, больше 8 — критично "
                "(статус — по выручке НТУ, без учёта сервиса).",
                SERVICE_RULE,
            ],
        },
    }
