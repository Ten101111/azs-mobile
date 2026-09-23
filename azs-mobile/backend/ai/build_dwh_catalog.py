"""Сборка каталога ИИ-контура из описаний витрин ОХД.

Читает выгруженные описания столбцов и складывает data/ai_catalog.json —
файл, по которому работают валидатор и контракт модели. Имена столбцов
витрин берутся из описания дословно, вручную ничего не переписывается.

К двум витринам dm добавляются три справочника схемы bds — кто руководил
объектом на дату (РУ и ТМ) и текущие статус и тип АЗС. Из них модель видит
только четыре поля (rm_fio, tm_fio, status_azs_name, type_azs_name) и ключи
для соединения; остальные столбцы справочников отсекает валидатор проекцией.
Если рядом лежит DWH_outputs/bds_values.json (его пишет
`backend.ai.check_dwh_people`), в описание попадают точные значения статуса и
типа АЗС и выбирается форма соединения по периодам.

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

# Справочники схемы bds. Описания написаны вручную и намеренно короткие:
# модель видит ровно эти столбцы, валидатор не выпускает из таблиц другие.
REF_SCHEMA = "bds"
RM = "l_azs_rm_dt_vers"
TM = "l_azs_tm_dt_vers"
KSSS_REF = "s_azs_ksss"
REFERENCES = {
    RM: {
        "note": "руководитель управления (РУ), за которым объект закреплён в каждый период",
        "scope_column": "ksss_code",
        "columns": [
            ("ksss_code", "bigint", "КССС АЗС, связь с ksss_azs_code витрин"),
            ("dt_vers_start", "date", "первый день закрепления"),
            ("dt_vers_end", "date", "последний день закрепления, включительно"),
            ("rm_fio", "text", "ФИО руководителя управления (РУ)"),
        ],
    },
    TM: {
        "note": "территориальный менеджер (ТМ), за которым объект закреплён в каждый период",
        "scope_column": "ksss_code",
        "columns": [
            ("ksss_code", "bigint", "КССС АЗС, связь с ksss_azs_code витрин"),
            ("dt_vers_start", "date", "первый день закрепления"),
            ("dt_vers_end", "date", "последний день закрепления, включительно"),
            ("tm_fio", "text", "ФИО территориального менеджера (ТМ)"),
        ],
    },
    KSSS_REF: {
        "note": "текущие статус и тип АЗС, одна строка на объект",
        "scope_column": "ksss_azs_code",
        "columns": [
            ("ksss_azs_code", "bigint", "КССС АЗС, связь с ksss_azs_code витрин"),
            ("status_azs_name", "text", "статус работы АЗС (текущий, не на дату)"),
            ("type_azs_name", "text", "тип АЗС: признак наличия кафе и магазина"),
        ],
    },
}
VALUES_FILE = DESCRIPTIONS / "bds_values.json"

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


def people_note(open_end: bool) -> str:
    """Правила для справочников РУ/ТМ и статуса/типа АЗС.

    open_end — у действующих периодов dt_vers_end пустой (так покажет проверка
    check_dwh_people); тогда конец периода берётся через COALESCE.
    """
    end = "COALESCE(rm.dt_vers_end, DATE '9999-12-31')" if open_end else "rm.dt_vers_end"
    return f"""\
РУ И ТМ — НА ДАТУ. Кто руководил объектом, хранится по периодам: РУ — в bds.{RM},
ТМ — в bds.{TM}. Соединяй по объекту и по попаданию дня в период, тогда факты
каждого дня относятся к тому, кто руководил объектом в этот день:

    JOIN bds.{RM} rm ON rm.ksss_code = f.ksss_azs_code
     AND f.account_date >= rm.dt_vers_start AND f.account_date <= {end}

Для ТМ то же самое с bds.{TM} и tm_fio. Объект, сменивший ТМ в середине
месяца, делится между двумя ТМ по дням — это правильно, так и должно быть.

ФАМИЛИЯ В ВОПРОСЕ. «У Ивановой», «по Петрову», «Сидорову» — это РУ или ТМ. Если перед
вопросом есть блок «ЛЮДИ В ВОПРОСЕ» — роль и точные ФИО уже определены по справочникам:
соединяй только указанный справочник и фильтруй точным ФИО из блока. Если блока нет —
ищи в обоих справочниках сразу (пример ниже, через UNION ALL) по основе фамилии без
падежного окончания: ILIKE '%Иванов%'. Выводи полное ФИО, роль (РУ или ТМ) и число АЗС.
Не выдумывай ФИО — бери из таблицы. Если строк нет — такого человека нет, так и скажи.

РУ И ТМ НЕ СРАВНИВАЮТ. Это разные уровни: объекты ТМ входят в управление РУ. РУ сравнивают
только с РУ, ТМ — только с ТМ. Если фамилия нашлась и среди РУ, и среди ТМ (однофамильцы),
речь идёт о РУ — строки ТМ отбрось. Если названы РУ и ТМ вместе — посчитай каждого
отдельно и не сопоставляй их.

СТАТУС И ТИП АЗС. bds.{KSSS_REF} — одна строка на объект с текущими статусом и типом
(не на дату). Соединение: JOIN bds.{KSSS_REF} k ON k.ksss_azs_code = f.ksss_azs_code.
type_azs_name — признак наличия кафе и магазина: по нему считают и фильтруют АЗС
«по формату». Для подсчёта объектов — COUNT(DISTINCT f.ksss_azs_code).
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
        # Фамилии в примерах вымышленные.
        "Какой суммарный ВД НТУ у Сидоровой и у Кузнецова в августе 2026",
        'SELECT p.fio AS "ФИО", p.role AS "Роль",\n'
        '       ROUND(SUM(f.vd_ntu)) AS "ВД НТУ, руб", COUNT(DISTINCT f.ksss_azs_code) AS "АЗС"\n'
        f"FROM {SCHEMA}.{FACTS} AS f\n"
        "JOIN (SELECT ksss_code, dt_vers_start, dt_vers_end, rm_fio AS fio, 'РУ' AS role\n"
        f"      FROM {REF_SCHEMA}.{RM}\n"
        "      UNION ALL\n"
        "      SELECT ksss_code, dt_vers_start, dt_vers_end, tm_fio, 'ТМ'\n"
        f"      FROM {REF_SCHEMA}.{TM}) AS p\n"
        "  ON p.ksss_code = f.ksss_azs_code\n"
        " AND f.account_date >= p.dt_vers_start AND f.account_date <= p.dt_vers_end\n"
        "WHERE f.account_date >= DATE '2026-08-01' AND f.account_date < DATE '2026-09-01'\n"
        "  AND (p.fio ILIKE '%Сидоров%' OR p.fio ILIKE '%Кузнецов%')\n"
        "GROUP BY p.fio, p.role\n"
        "ORDER BY 3 DESC",
    ),
    (
        # Кто сейчас руководит объектом — решение владельца 23.09.2026: ИИ отвечает
        # по справочникам ОХД, а не по карточке приложения.
        "Кто РУ и ТМ у АЗС 5044",
        'SELECT DISTINCT f.ksss_azs_code AS "КССС", f.num_azs AS "Номер АЗС",\n'
        '       rm.rm_fio AS "РУ", tm.tm_fio AS "ТМ"\n'
        f"FROM {SCHEMA}.{FACTS} AS f\n"
        f"LEFT JOIN {REF_SCHEMA}.{RM} AS rm\n"
        "  ON rm.ksss_code = f.ksss_azs_code\n"
        " AND CURRENT_DATE >= rm.dt_vers_start AND CURRENT_DATE <= COALESCE(rm.dt_vers_end, DATE '9999-12-31')\n"
        f"LEFT JOIN {REF_SCHEMA}.{TM} AS tm\n"
        "  ON tm.ksss_code = f.ksss_azs_code\n"
        " AND CURRENT_DATE >= tm.dt_vers_start AND CURRENT_DATE <= COALESCE(tm.dt_vers_end, DATE '9999-12-31')\n"
        "WHERE (f.ksss_azs_code = 5044 OR f.num_azs = 5044)\n"
        "  AND f.account_date > CURRENT_DATE - 60",
    ),
    (
        "Выручка НТУ по ТМ общества ЦНП за август 2026",
        'SELECT tm.tm_fio AS "ТМ", ROUND(SUM(f.sum_receipt_netto_ntu)) AS "Выручка НТУ, руб",\n'
        '       COUNT(DISTINCT f.ksss_azs_code) AS "АЗС"\n'
        f"FROM {SCHEMA}.{FACTS} AS f\n"
        f"JOIN {REF_SCHEMA}.{TM} AS tm\n"
        "  ON tm.ksss_code = f.ksss_azs_code\n"
        " AND f.account_date >= tm.dt_vers_start AND f.account_date <= tm.dt_vers_end\n"
        "WHERE f.npo = 'ЦНП'\n"
        "  AND f.account_date >= DATE '2026-08-01' AND f.account_date < DATE '2026-09-01'\n"
        "GROUP BY tm.tm_fio\n"
        "ORDER BY 2 DESC",
    ),
    (
        "Сколько АЗС каждого типа и какая у них выручка НТУ за август 2026",
        'SELECT k.type_azs_name AS "Тип АЗС", COUNT(DISTINCT f.ksss_azs_code) AS "АЗС",\n'
        '       ROUND(SUM(f.sum_receipt_netto_ntu)) AS "Выручка НТУ, руб"\n'
        f"FROM {SCHEMA}.{FACTS} AS f\n"
        f"JOIN {REF_SCHEMA}.{KSSS_REF} AS k ON k.ksss_azs_code = f.ksss_azs_code\n"
        "WHERE f.account_date >= DATE '2026-08-01' AND f.account_date < DATE '2026-09-01'\n"
        "GROUP BY k.type_azs_name\n"
        "ORDER BY 3 DESC",
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


def read_values() -> dict:
    """Результат проверки справочников (check_dwh_people), если он есть."""
    if not VALUES_FILE.exists():
        return {}
    try:
        return json.loads(VALUES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def reference_columns(values: dict) -> dict[str, list[dict]]:
    """Столбцы справочников bds для описания; точные значения — если проверены."""
    out = {}
    for table, spec in REFERENCES.items():
        columns = []
        for name, type_, description in spec["columns"]:
            known = values.get("values", {}).get(name) or []
            if known:
                listed = ", ".join(f"'{item['value']}'" for item in known[:15])
                description = f"{description}; значения: {listed}"
            columns.append({"name": name, "type": type_, "description": description})
        out[table] = columns
    return out


def describe(tables: dict[str, list[dict]], references: dict[str, list[dict]] | None = None,
             open_end: bool = False) -> str:
    references = references or {}
    head = "Диалект: PostgreSQL. Доступны две витрины схемы dm"
    head += " и три справочника схемы bds." if references else "."
    blocks = [head, ""]
    for table, columns in tables.items():
        blocks.append(f"{SCHEMA}.{table} — {TABLE_NOTES[table]}")
        width = max(len(c["name"]) for c in columns)
        for column in columns:
            note = f"  {column['description']}" if column["description"] else ""
            blocks.append(f"  {column['name']:<{width}}  {column['type']}{note}")
        blocks.append("")
    for table, columns in references.items():
        blocks.append(f"{REF_SCHEMA}.{table} — {REFERENCES[table]['note']}")
        width = max(len(c["name"]) for c in columns)
        for column in columns:
            blocks.append(f"  {column['name']:<{width}}  {column['type']}  {column['description']}")
        blocks.append("")
    blocks.append(RULES_NOTE)
    if references:
        blocks.append(people_note(open_end))
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

    values = read_values()
    open_end = bool(values.get("open_end"))
    references = reference_columns(values)
    examples = [
        (question, sql.replace("f.account_date <= p.dt_vers_end",
                               "f.account_date <= COALESCE(p.dt_vers_end, DATE '9999-12-31')")
                      .replace("f.account_date <= tm.dt_vers_end",
                               "f.account_date <= COALESCE(tm.dt_vers_end, DATE '9999-12-31')")
         if open_end else sql)
        for question, sql in EXAMPLES
    ]

    payload = {
        "dialect": "postgres",
        "schema": SCHEMA,
        "scope_column": SCOPE_COLUMN,
        "scope_value_type": "number",
        "facts_table": FACTS,
        "plans_table": PLANS,
        "date_column": "account_date",
        "tables": {
            **{
                # project: наружу выходят только столбцы каталога — dt_ins и rating
                # недоступны даже через SELECT * или CTE.
                table: {
                    "columns": [column["name"] for column in columns],
                    "scoped": True,
                    "project": True,
                }
                for table, columns in tables.items()
            },
            # Справочники bds: своя схема, свой ключ объекта, наружу — только эти столбцы.
            **{
                table: {
                    "schema": REF_SCHEMA,
                    "columns": [column["name"] for column in columns],
                    "scoped": True,
                    "scope_column": REFERENCES[table]["scope_column"],
                    "project": True,
                }
                for table, columns in references.items()
            },
        },
        "description": describe(tables, references, open_end),
        "examples": [{"question": question, "sql": sql} for question, sql in examples],
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(columns) for columns in tables.values())
    print(f"{out.name}: {len(tables)} витрины, {total} столбцов, {len(examples)} примеров")
    for table, columns in tables.items():
        print(f"  {SCHEMA}.{table}: {len(columns)} столбцов")
    for table, columns in references.items():
        print(f"  {REF_SCHEMA}.{table}: {', '.join(c['name'] for c in columns)}")
    if values:
        print(f"  значения статуса и типа — из проверки {values.get('checked_at', '')}"
              + ("; конец действующих периодов пустой — соединение через COALESCE" if open_end else ""))
        for warning in values.get("warnings", []):
            print(f"  ВНИМАНИЕ: {warning}")
    else:
        print("  значения статуса и типа АЗС не проверены: запустите python -m backend.ai.check_dwh_people")


if __name__ == "__main__":
    main()
