"""СП-02. Еженедельная справка по сети: расчёт модели выпуска.

Источник — только витрина ОХД: dm.data_for_ai_analytic_part_1 (факты по АЗС за
день) и dm.data_for_ai_analytic_part_2 (планы по АЗС за день). Решение
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
FORMULAS_VERSION = "2026-09-24.1"
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
    (("service_avg", "service_quality"), "Уровень и качество сервиса"),
)
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
           f'       SUM(CASE WHEN {src.within(prev)} THEN {fuel} END) AS "fuel_p"\n'
           f"FROM {src.facts} AS f {src.join}\n"
           f"WHERE ({src.within(week)}) OR ({src.within(prev)}) OR ({src.within(year)})\n"
           f"GROUP BY {src.key}")
    return src.query(sql)


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
    if not plans:
        return None
    covered = src.query(f'SELECT COUNT(DISTINCT {pdate}) AS "days" FROM {src.plans} AS p\n'
                        f"WHERE {pdate} BETWEEN {src.lit(start)} AND {src.lit(end)}", 1)
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
    zone = len(set(attention["dropKeys"]) | {r["key"] for r in attention["noSales"]})
    if zone:
        out.append(f"В зоне внимания {zone} {_objects(zone)}: падение топлива на "
                   f"{fmt.number(THRESHOLDS['fuelDropPct'])} % и больше — {attention['dropsTotal']}, "
                   f"без продаж {fmt.days(THRESHOLDS['noSalesDays'])} и больше — {len(attention['noSales'])}.")
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
    attention = _attention(stations)
    dynamics = _dynamics(src, week)
    holidays = week.holidays() + prev.holidays() + year.holidays()

    both_nn = sum(1 for s in stations if int(s.get("days_w") or 0) == 7 and int(s.get("days_p") or 0) == 7)
    both_yy = sum(1 for s in stations if int(s.get("days_w") or 0) == 7 and int(s.get("days_y") or 0) == 7)
    seen_nn = sum(1 for s in stations if int(s.get("days_w") or 0) or int(s.get("days_p") or 0))
    seen_yy = sum(1 for s in stations if int(s.get("days_w") or 0) or int(s.get("days_y") or 0))
    pending = [title for codes, title in PENDING_BLOCKS
               if codes != PLAN_BLOCK and not all(c in src.tiles for c in codes)]
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
        "headline": _insert_plan(_headline(metrics, onpo, attention, week, holidays), _plan_headline(plan)),
        "plan": plan,
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
                      + (f", dm.{MART_PLANS} (планы)" if src.plans else ""),
            "fuelUnitNote": f"Реализация топлива — в {'тоннах' if fuel_tile.unit == 'т' else 'литрах'}",
            "formulasVersion": FORMULAS_VERSION,
            "runId": run_id,
            "holidays": holidays,
            "planNote": (plan or {}).get("note") or (f"Блока плана нет: {plan_missing}." if plan is None else ""),
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
            ],
        },
    }
