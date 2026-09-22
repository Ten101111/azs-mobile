"""Сборка каталога ИИ-контура из описаний витрин ОХД.

Читает выгруженные описания столбцов и складывает data/ai_catalog.json —
файл, по которому работают валидатор и контракт модели. Имена столбцов
берутся из описания дословно, вручную ничего не переписывается.

    python3 backend/ai/build_dwh_catalog.py
    python3 backend/ai/build_dwh_catalog.py --out data/ai_catalog.json
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
APP = Path(__file__).resolve().parents[2]
DESCRIPTIONS = ROOT / "DWH_outputs"

SCHEMA = "dm"
FACTS = "data_for_ai_analytic_part_1"
PLANS = "data_for_ai_analytic_part_2"
SCOPE_COLUMN = "ksss_azs_code"

SOURCES = {FACTS: "Table_1_description.csv", PLANS: "Table_2_description.csv"}

# Технические и закрытые решением владельца столбцы в каталог не попадают:
# dt_ins — служебная отметка загрузки, rating — оценка «Яндекс», которую
# договорились не выводить ни в аналитику, ни в справки.
EXCLUDED = {"dt_ins", "rating"}

TABLE_NOTES = {
    FACTS: "факты по АЗС за день: топливо, чеки, НТУ, валовой доход",
    PLANS: "планы и сервис по АЗС за день: план топлива и НТУ, оценки в мобильном приложении, жалобы, покрытие, OPEX",
}

RULES_NOTE = """\
Гранулярность обеих витрин — одна строка на АЗС за день, ключ ksss_azs_code + account_date.
Соединять витрины следует по обоим полям ключа сразу.
Измерения (НПО, регион, номер АЗС) есть только в part_1.

ДВА ИДЕНТИФИКАТОРА ОБЪЕКТА. ksss_azs_code — внутренний код АЗС, num_azs — номер
на вывеске. Это разные числа у одной и той же станции. Когда в вопросе объект
назван просто числом («АЗС 5044», «по 77551»), неизвестно, какое из полей имеется
в виду, поэтому фильтруйте по обоим сразу и выводите оба в результат:

    WHERE (ksss_azs_code = 5044 OR num_azs = 5044)
    ... SELECT ksss_azs_code AS "КССС", num_azs AS "Номер АЗС", ...

ПЕРИОДЫ. Столбца с месяцем нет, период задаётся диапазоном по account_date.
Конкретный месяц:
    account_date >= DATE '2026-08-01' AND account_date < DATE '2026-09-01'
Текущий месяц с начала и по сегодня («на сегодняшний день», «в этом месяце»,
«с начала месяца»):
    account_date >= DATE_TRUNC('month', CURRENT_DATE) AND account_date <= CURRENT_DATE
Текущий год:
    account_date >= DATE_TRUNC('year', CURRENT_DATE) AND account_date <= CURRENT_DATE
Последние N дней:
    account_date > CURRENT_DATE - INTERVAL '30 days'
Не подставляйте сегодняшнюю дату числом — пользуйтесь CURRENT_DATE.
"""

EXAMPLES = [
    (
        "Выручка НТУ по сети за август 2026",
        'SELECT ROUND(SUM(sum_receipt_netto_ntu)) AS "Выручка НТУ, руб"\n'
        f"FROM {SCHEMA}.{FACTS}\n"
        "WHERE account_date >= DATE '2026-08-01' AND account_date < DATE '2026-09-01'",
    ),
    (
        "Топ-5 АЗС по конверсии НТУ за сентябрь 2026",
        'SELECT num_azs AS "Номер АЗС",\n'
        '       ROUND(100.0 * SUM(cnt_cheq_ntu) / NULLIF(SUM(cnt_cheq_tu), 0), 1) AS "Конверсия НТУ, %"\n'
        f"FROM {SCHEMA}.{FACTS}\n"
        "WHERE account_date >= DATE '2026-09-01' AND account_date < DATE '2026-10-01'\n"
        "GROUP BY num_azs\n"
        "ORDER BY 2 DESC\n"
        "LIMIT 5",
    ),
    (
        "Сравни реализацию топлива за август 2026 с августом 2025",
        'SELECT ROUND(SUM(sum_weight) FILTER (WHERE account_date >= DATE \'2026-08-01\''
        " AND account_date < DATE '2026-09-01'), 1) AS \"Август 2026, т\",\n"
        '       ROUND(SUM(sum_weight) FILTER (WHERE account_date >= DATE \'2025-08-01\''
        " AND account_date < DATE '2025-09-01'), 1) AS \"Август 2025, т\"\n"
        f"FROM {SCHEMA}.{FACTS}\n"
        "WHERE (account_date >= DATE '2025-08-01' AND account_date < DATE '2025-09-01')\n"
        "   OR (account_date >= DATE '2026-08-01' AND account_date < DATE '2026-09-01')",
    ),
    (
        "Выполнение плана НТУ за август 2026",
        'SELECT ROUND(SUM(f.sum_receipt_netto_ntu)) AS "Факт НТУ, руб",\n'
        '       ROUND(SUM(p.plan_ntu_revenue)) AS "План НТУ, руб",\n'
        "       ROUND(100.0 * SUM(f.sum_receipt_netto_ntu)"
        ' / NULLIF(SUM(p.plan_ntu_revenue), 0), 1) AS "Выполнение плана, %"\n'
        f"FROM {SCHEMA}.{FACTS} AS f\n"
        f"JOIN {SCHEMA}.{PLANS} AS p\n"
        "  ON p.ksss_azs_code = f.ksss_azs_code AND p.account_date = f.account_date\n"
        "WHERE f.account_date >= DATE '2026-08-01' AND f.account_date < DATE '2026-09-01'",
    ),
    (
        "Средняя оценка в мобильном приложении за август 2026",
        "SELECT ROUND((SUM(rate_cnt_5) * 5 + SUM(rate_cnt_4) * 4 + SUM(rate_cnt_3) * 3\n"
        "              + SUM(rate_cnt_2) * 2 + SUM(rate_cnt_1))::numeric\n"
        '             / NULLIF(SUM(all_rate), 0), 3) AS "Средняя оценка в МП"\n'
        f"FROM {SCHEMA}.{PLANS}\n"
        "WHERE account_date >= DATE '2026-08-01' AND account_date < DATE '2026-09-01'",
    ),
    (
        "Какой план НТУ у АЗС 5044 и как он выполнен с начала месяца",
        'SELECT f.ksss_azs_code AS "КССС", f.num_azs AS "Номер АЗС",\n'
        '       ROUND(SUM(f.sum_receipt_netto_ntu)) AS "Факт НТУ, руб",\n'
        '       ROUND(SUM(p.plan_ntu_revenue)) AS "План НТУ, руб",\n'
        "       ROUND(100.0 * SUM(f.sum_receipt_netto_ntu)"
        ' / NULLIF(SUM(p.plan_ntu_revenue), 0), 1) AS "Выполнение плана, %"\n'
        f"FROM {SCHEMA}.{FACTS} AS f\n"
        f"JOIN {SCHEMA}.{PLANS} AS p\n"
        "  ON p.ksss_azs_code = f.ksss_azs_code AND p.account_date = f.account_date\n"
        "WHERE (f.ksss_azs_code = 5044 OR f.num_azs = 5044)\n"
        "  AND f.account_date >= DATE_TRUNC('month', CURRENT_DATE)\n"
        "  AND f.account_date <= CURRENT_DATE\n"
        "GROUP BY f.ksss_azs_code, f.num_azs",
    ),
    (
        "Выручка НТУ по обществам за июль 2026",
        'SELECT npo AS "ОНПО", ROUND(SUM(sum_receipt_netto_ntu)) AS "Выручка НТУ, руб"\n'
        f"FROM {SCHEMA}.{FACTS}\n"
        "WHERE account_date >= DATE '2026-07-01' AND account_date < DATE '2026-08-01'\n"
        "GROUP BY npo\n"
        "ORDER BY 2 DESC",
    ),
]


def read_columns(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        {
            "name": row["column_name"].strip(),
            "type": row["data_type"].strip(),
            "description": (row.get("description") or "").strip(),
        }
        for row in rows
        if row.get("column_name") and row["column_name"].strip() not in EXCLUDED
    ]


def describe(tables: dict[str, list[dict]]) -> str:
    blocks = ["Диалект: PostgreSQL. Доступны ровно две витрины схемы dm.", ""]
    for table, columns in tables.items():
        blocks.append(f"{SCHEMA}.{table} — {TABLE_NOTES[table]}")
        width = max(len(c["name"]) for c in columns)
        for column in columns:
            note = f"  {column['description']}" if column["description"] else ""
            blocks.append(f"  {column['name']:<{width}}  {column['type']}{note}")
        blocks.append("")
    blocks.append(RULES_NOTE)
    blocks.append(
        "Производные показатели считай формулами:\n"
        "  конверсия НТУ, %       = 100.0 * SUM(cnt_cheq_ntu) / NULLIF(SUM(cnt_cheq_tu), 0)\n"
        "  средний чек НТУ, руб   = SUM(sum_receipt_netto_ntu) / NULLIF(SUM(cnt_cheq_ntu), 0)\n"
        "  средняя заправка, л    = SUM(sum_volume) / NULLIF(SUM(cnt_cheq_tu), 0)\n"
        "  маржа НТУ, %           = 100.0 * SUM(vd_ntu) / NULLIF(SUM(sum_receipt_netto_ntu), 0)\n"
        "  комплексность НТУ, шт  = SUM(sum_quant_ntu_retail) * 1.0 / NULLIF(SUM(cnt_cheq_ntu), 0)\n"
        "  средняя оценка, балл   = (SUM(rate_cnt_5) * 5.0 + SUM(rate_cnt_4) * 4 + SUM(rate_cnt_3) * 3\n"
        "                            + SUM(rate_cnt_2) * 2 + SUM(rate_cnt_1)) / NULLIF(SUM(all_rate), 0)\n"
        "  негативные оценки, шт  = SUM(rate_cnt_1) + SUM(rate_cnt_2)\n"
        "\n"
        "Количество товаров НТУ считай по ретейлу — sum_quant_ntu_retail. Столбец\n"
        "sum_quant_ntu_balance в расчётах не используй: соотношение двух учётов\n"
        "не подтверждено владельцем данных.\n"
        "\n"
        "Целые столбцы делятся нацело. Там, где нужен дробный результат, умножай\n"
        "числитель на 1.0 — иначе средняя оценка и комплексность выйдут ровным числом."
    )
    return "\n".join(blocks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Каталог ИИ-контура из описаний витрин ОХД")
    parser.add_argument("--out", default=str(APP / "data" / "ai_catalog.dwh.json"))
    args = parser.parse_args()

    tables = {}
    for table, filename in SOURCES.items():
        path = DESCRIPTIONS / filename
        if not path.exists():
            raise SystemExit(f"Нет описания витрины: {path}")
        tables[table] = read_columns(path)

    payload = {
        "dialect": "postgres",
        "schema": SCHEMA,
        "scope_column": SCOPE_COLUMN,
        "scope_value_type": "number",
        "facts_table": FACTS,
        "plans_table": PLANS,
        "date_column": "account_date",
        "tables": {
            table: {
                "columns": [column["name"] for column in columns],
                "scoped": True,
            }
            for table, columns in tables.items()
        },
        "description": describe(tables),
        "examples": [{"question": question, "sql": sql} for question, sql in EXAMPLES],
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(columns) for columns in tables.values())
    print(f"{out.name}: {len(tables)} витрины, {total} столбцов, {len(EXAMPLES)} примеров")
    for table, columns in tables.items():
        print(f"  {SCHEMA}.{table}: {len(columns)} столбцов")


if __name__ == "__main__":
    main()
