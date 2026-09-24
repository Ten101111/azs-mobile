"""Синтетическая витрина ОХД для тестов справок: те же таблицы и столбцы, что dm.data_for_ai_analytic_part_1/2.

Справка строится только по витрине ОХД (решение владельца 24.09.2026), поэтому
тесты гоняют тот же путь, что и боевой: каталог в диалекте PostgreSQL со схемой
dm, валидатор с проекцией таблиц, а исполнитель переводит проверенный SQL в
SQLite (sqlglot) и выполняет его на файле, подключённом как схема dm.

36 объектов в трёх ОНПО, дни с августа 2025 по 20 сентября 2026. Особые объекты:
  7005 — падение топлива на 40 % в отчётной неделе;
  7007 — три дня без продаж в отчётной неделе;
  7009 — нет данных за два дня отчётной недели (не входит в сопоставимую базу);
  7012 — открыт в 2026 году (нет прошлого года).
Планы (part_2) — на каждый день месяца, включая будущие дни, до конца сентября 2026:
топливо — на 2 % выше обычного дня, выручка НТУ — на 5 %, ВД НТУ — на 5 %.
Оценки сервиса (part_2) — на каждый день с данными: 10–14 оценок, из них две «4» и
одна «3», остальные «5». Негатив («1» и «2») в отчётной неделе: 7005 — четыре
(касса — 3, чистота — 1) и жалоба ЕГЛ, 7020 — две (техсостояние), 7030 — одна
(другое, ниже порога); в прошлой неделе — одна у 7005 (касса); в той же неделе
прошлого года — одна у 7003 (чистота).
"""
from __future__ import annotations

import pathlib
import sqlite3
import time
from calendar import monthrange
from datetime import date, timedelta
from unittest import mock

import sqlglot

from backend.ai import validator
from backend.ai.catalog import Catalog
from backend.ai.executor import Result
from backend.reports import periods

REPORT_WEEK = periods.Week(date(2026, 9, 14), date(2026, 9, 20))
ONPO = ("Север", "Юг", "Центр")
FACTS, PLANS = "data_for_ai_analytic_part_1", "data_for_ai_analytic_part_2"

FACT_COLUMNS = (
    "ksss_azs_code", "num_azs", "npo", "region_name", "account_date", "sum_weight", "sum_volume",
    "sum_weight_b2c", "sum_volume_b2c", "sum_weight_b2b", "sum_volume_b2b", "sum_weight_ab", "sum_volume_ab",
    "sum_weight_dt", "sum_volume_dt", "cnt_cheq", "cnt_cheq_b2b", "cnt_cheq_b2c", "cnt_cheq_tu", "cnt_cheq_ntu",
    "cnt_complex_cheq_tu_ntu", "sum_quant_ntu_retail", "sum_ball_out", "cnt_cheq_kl", "sum_receipt_netto_tu",
    "sum_receipt_netto_tu_b2b", "sum_receipt_netto_tu_b2c", "sum_receipt_netto_ntu", "vd_ntu",
    "sum_quant_ntu_balance", "vd_cafe", "vd_prod", "vd_neprod", "sum_quant_cafe", "sum_quant_prod",
    "sum_quant_neprod", "vd_category_4", "sum_quant_category_4",
)
PLAN_COLUMNS = (
    "ksss_azs_code", "account_date", "plan_weights_b2c", "plan_weights_b2b", "plan_ntu_revenue", "plan_ntu_vd",
    "cnt_num_compl", "all_rate", "rate_cnt_1", "rate_cnt_2", "rate_cnt_3", "rate_cnt_4", "rate_cnt_5",
    "all_negative_rate_category", "rate_pers_act_azs_cnt", "rate_clear_azs_cnt", "rate_asort_azs_cnt",
    "rate_tech_azs_cnt", "rate_app_mob_azs_cnt", "rate_other_azs_cnt", "rate_loy_card_azs_cnt",
    "rate_refueller_azs_cnt", "sum_costs_to_cover", "sum_opex",
)


def mart_catalog() -> Catalog:
    """Каталог в форме боевого: PostgreSQL, схема dm, обе витрины с проекцией столбцов."""
    return Catalog(
        tables={FACTS: set(FACT_COLUMNS), PLANS: set(PLAN_COLUMNS)},
        scoped_tables={FACTS, PLANS}, scope_column="ksss_azs_code", scope_value_type="number",
        facts_table=FACTS, plans_table=PLANS, date_column="account_date", dialect="postgres", schema="dm",
        projected_tables={FACTS, PLANS}, column_order={FACTS: list(FACT_COLUMNS), PLANS: list(PLAN_COLUMNS)},
        source="тестовая витрина ОХД",
    )


PREV_WEEK = periods.previous(REPORT_WEEK)
LAST_YEAR_WEEK = periods.last_year(REPORT_WEEK)
# (объект, неделя, день недели) → (оценка «1» или «2», категория негатива)
NEGATIVE = {
    (7005, "w", 0): (1, "rate_pers_act_azs_cnt"), (7005, "w", 1): (1, "rate_pers_act_azs_cnt"),
    (7005, "w", 2): (1, "rate_pers_act_azs_cnt"), (7005, "w", 3): (2, "rate_clear_azs_cnt"),
    (7020, "w", 0): (2, "rate_tech_azs_cnt"), (7020, "w", 1): (2, "rate_tech_azs_cnt"),
    (7030, "w", 2): (1, "rate_other_azs_cnt"),
    (7005, "p", 0): (1, "rate_pers_act_azs_cnt"),
    (7003, "y", 1): (2, "rate_clear_azs_cnt"),
}
COMPLAINTS = {(7005, "w", 4): 1}


def _ratings(key: int, i: int, day: date) -> dict:
    part = next((name for name, w in (("w", REPORT_WEEK), ("p", PREV_WEEK), ("y", LAST_YEAR_WEEK))
                 if w.start <= day <= w.end), "")
    total = 10 + i % 5
    row = {"all_rate": total, "rate_cnt_5": total - 3, "rate_cnt_4": 2, "rate_cnt_3": 1,
           "rate_cnt_2": 0, "rate_cnt_1": 0, "all_negative_rate_category": 0,
           "cnt_num_compl": COMPLAINTS.get((key, part, day.weekday()), 0)}
    bad = NEGATIVE.get((key, part, day.weekday()))
    if bad:
        grade, category = bad
        row["rate_cnt_5"] -= 1
        row[f"rate_cnt_{grade}"] += 1
        row[category] = 1
        row["all_negative_rate_category"] = 1
    return row


def _day_fuel(i: int, day: date) -> float:
    return 3000 + 100 * i + (400 if day.weekday() >= 5 else 0)


def build(folder: pathlib.Path, *, last_day: date = date(2026, 9, 20), sparse: bool = False,
          plan_until: date | None = date(2026, 9, 30), ratings: bool = True) -> pathlib.Path:
    """Файл SQLite с таблицами витрины; `plan_until=None` — планов нет вовсе, `ratings=False` — оценок нет."""
    path = folder / "dm.sqlite3"
    conn = sqlite3.connect(path)
    def kind(column: str) -> str:
        if column in ("npo", "region_name", "account_date"):
            return " TEXT"
        return " INTEGER" if column in ("ksss_azs_code", "num_azs") else " REAL"

    conn.execute(f"CREATE TABLE {FACTS} ({', '.join(c + kind(c) for c in FACT_COLUMNS)})")
    conn.execute(f"CREATE TABLE {PLANS} ({', '.join(c + (' TEXT' if c == 'account_date' else ' REAL') for c in PLAN_COLUMNS)})")
    facts, plans = [], []
    day = date(2025, 8, 1)
    plan_end = plan_until or date(2000, 1, 1)
    while day <= max(last_day, plan_end):
        for i in range(1, 37):
            key = 7000 + i
            if key == 7012 and day < date(2026, 1, 1):
                continue
            usual = _day_fuel(i, day)
            part2 = {"ksss_azs_code": key, "account_date": day.isoformat()}
            if day <= plan_end and day >= date(2026, 8, 1):
                ntu_usual = usual / 40 * 0.3 * 350
                part2.update({"plan_weights_b2c": usual * 1.02 * 0.9, "plan_weights_b2b": usual * 1.02 * 0.1,
                              "plan_ntu_revenue": ntu_usual * 1.05, "plan_ntu_vd": ntu_usual * 0.3 * 1.05})
            if day <= last_day and ratings:
                part2.update(_ratings(key, i, day))
            if len(part2) > 2:
                plans.append(part2)
            if day > last_day:
                continue
            in_week = REPORT_WEEK.start <= day <= REPORT_WEEK.end
            if key == 7009 and in_week and day.weekday() in (2, 3):
                continue
            if sparse and in_week and i % 5 == 0 and day.weekday() == 6:
                continue
            fuel = usual
            if key == 7005 and in_week:
                fuel *= 0.6
            if key == 7007 and in_week and day.weekday() in (0, 1, 2):
                fuel = 0
            checks = fuel / 40
            ntu_checks = checks * 0.3
            ntu = ntu_checks * 350
            facts.append({"ksss_azs_code": key, "num_azs": 10000 + i, "npo": ONPO[(i - 1) % 3],
                          "region_name": f"Регион {i % 4}", "account_date": day.isoformat(),
                          "sum_weight": fuel, "sum_volume": fuel * 1300, "cnt_cheq": checks,
                          "cnt_cheq_tu": checks - ntu_checks, "cnt_cheq_ntu": ntu_checks,
                          "sum_receipt_netto_tu": fuel * 60, "sum_receipt_netto_ntu": ntu, "vd_ntu": ntu * 0.3,
                          "sum_quant_ntu_retail": ntu_checks * 2})
        day += timedelta(days=1)
    for table, columns, rows in ((FACTS, FACT_COLUMNS, facts), (PLANS, PLAN_COLUMNS, plans)):
        conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})",
                         [tuple(r.get(c, 0 if c not in ('npo', 'region_name') else '') for c in columns) for r in rows])
    conn.commit()
    conn.close()
    return path


def runner(path: pathlib.Path):
    """Исполнитель: проверенный валидатором SQL диалекта PostgreSQL → SQLite, файл витрины подключён как dm."""
    def run(sql: str, row_limit: int, timeout_s: float | None = None) -> Result:
        started = time.monotonic()
        conn = sqlite3.connect(":memory:")
        conn.execute("ATTACH DATABASE ? AS dm", (str(path),))
        try:
            cur = conn.execute(sqlglot.transpile(sql, read="postgres", write="sqlite")[0])
            rows = cur.fetchmany(row_limit + 1)
            columns = [d[0] for d in cur.description]
        finally:
            conn.close()
        return Result(columns=columns, rows=[tuple(r) for r in rows[:row_limit]],
                      elapsed_ms=int((time.monotonic() - started) * 1000), truncated=len(rows) > row_limit)
    return run


def patch_validator(test, catalog: Catalog) -> None:
    """Валидатор читает каталог из глобальных переменных модуля — подменяем их на время теста."""
    for name, value in (("CATALOG", catalog), ("DIALECT", "postgres"), ("ALLOWED_SCHEMA", "dm"),
                        ("SCOPED_TABLES", catalog.scoped_tables), ("SCOPE_COLUMN", catalog.scope_column),
                        ("PROJECTED_TABLES", catalog.projected_tables)):
        patcher = mock.patch.object(validator, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)


def month_days(day: date) -> int:
    return monthrange(day.year, day.month)[1]
