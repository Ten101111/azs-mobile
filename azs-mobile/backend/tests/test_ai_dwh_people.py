"""РУ, ТМ, статус и тип АЗС из справочников bds витрины ОХД.

Проверяется то, от чего зависит правильность ответа «сколько у Ивановой»:
каталог описывает ровно четыре поля и ключи, валидатор не выпускает из
справочников других столбцов, подставляет схему и область данных по своему
ключу каждой таблицы, правила и примеры учат соединению по периодам.
Соединения с ОХД в тестах нет: SQL проверяется по тексту. Выполнение на
PostgreSQL проверено отдельно на синтетической схеме dm/bds.
Фамилии в тестах вымышленные.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from backend.ai import build_dwh_catalog as builder
from backend.ai import catalog as catalog_module
from backend.ai import contract, semantic, validator
from backend.ai.agent import tools
from backend.ai.agent.state import Budget
from backend.ai.executor import Result
from backend.ai.validator import Rejected, Scope, validate

ALLOWED = {
    "l_azs_rm_dt_vers": ["ksss_code", "dt_vers_start", "dt_vers_end", "rm_fio"],
    "l_azs_tm_dt_vers": ["ksss_code", "dt_vers_start", "dt_vers_end", "tm_fio"],
    "s_azs_ksss": ["ksss_azs_code", "status_azs_name", "type_azs_name"],
}

PERSON_SQL = """
SELECT p.fio AS "ФИО", p.role AS "Роль", ROUND(SUM(f.vd_ntu)) AS "ВД НТУ, руб",
       COUNT(DISTINCT f.ksss_azs_code) AS "АЗС"
FROM dm.data_for_ai_analytic_part_1 AS f
JOIN (SELECT ksss_code, dt_vers_start, dt_vers_end, rm_fio AS fio, 'РУ' AS role FROM bds.l_azs_rm_dt_vers
      UNION ALL
      SELECT ksss_code, dt_vers_start, dt_vers_end, tm_fio, 'ТМ' FROM l_azs_tm_dt_vers) AS p
  ON p.ksss_code = f.ksss_azs_code AND f.account_date >= p.dt_vers_start AND f.account_date <= p.dt_vers_end
WHERE f.account_date >= DATE '2026-09-01' AND f.account_date < DATE '2026-10-01'
  AND (p.fio ILIKE '%Сидоров%' OR p.fio ILIKE '%Кузнецов%')
GROUP BY p.fio, p.role
"""


def _build(folder: pathlib.Path, values: dict | None = None) -> catalog_module.Catalog:
    """Каталог, собранный настоящим сборщиком из минимальных описаний витрин."""
    descriptions = folder / "DWH_outputs"
    descriptions.mkdir()
    header = '"column_order","column_name","data_type","is_nullable","description"\n'
    (descriptions / "Table_1_description.csv").write_text(
        header + "1,ksss_azs_code,bigint,YES,КССС АЗС\n2,npo,text,YES,НПО\n3,account_date,date,YES,День\n"
                 "4,vd_ntu,numeric,YES,ВД НТУ\n5,sum_receipt_netto_ntu,numeric,YES,Выручка НТУ\n"
                 "6,rating,numeric,YES,Оценка\n7,num_azs,integer,YES,Номер АЗС\n", encoding="utf-8")
    (descriptions / "Table_2_description.csv").write_text(
        header + "1,ksss_azs_code,bigint,YES,КССС АЗС\n2,account_date,date,YES,День\n"
                 "3,plan_ntu_revenue,numeric,YES,План\n", encoding="utf-8")
    values_file = descriptions / "bds_values.json"
    if values is not None:
        values_file.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    out = folder / "catalog.json"
    with mock.patch.object(builder, "DESCRIPTIONS", descriptions), \
            mock.patch.object(builder, "VALUES_FILE", values_file), \
            mock.patch.object(sys, "argv", ["build", "--out", str(out)]), \
            mock.patch("builtins.print"):
        builder.main()
    return catalog_module._from_file(out)


class DwhCase(unittest.TestCase):
    """Валидатор и контракт переключаются на каталог ОХД на время теста."""

    values: dict | None = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.catalog = _build(pathlib.Path(self._tmp.name), self.values)
        patches = [
            mock.patch.object(validator, "CATALOG", self.catalog),
            mock.patch.object(validator, "SCOPED_TABLES", self.catalog.scoped_tables),
            mock.patch.object(validator, "PROJECTED_TABLES", self.catalog.projected_tables),
            mock.patch.object(validator, "SCOPE_COLUMN", self.catalog.scope_column),
            mock.patch.object(validator, "DIALECT", "postgres"),
            mock.patch.object(validator, "ALLOWED_SCHEMA", "dm"),
            mock.patch.object(contract, "CATALOG", self.catalog),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self._tmp.cleanup()


class CatalogTests(DwhCase):
    def test_reference_tables_carry_only_the_four_fields_and_keys(self):
        for table, columns in ALLOWED.items():
            self.assertEqual(self.catalog.columns_of(table), columns)
            self.assertEqual(self.catalog.schema_of(table), "bds")
            self.assertIn(table, self.catalog.projected_tables)
            self.assertIn(table, self.catalog.scoped_tables)
        self.assertEqual(self.catalog.scope_column_of("l_azs_rm_dt_vers"), "ksss_code")
        self.assertEqual(self.catalog.scope_column_of("s_azs_ksss"), "ksss_azs_code")
        self.assertEqual(self.catalog.schema_of("data_for_ai_analytic_part_1"), "dm")
        self.assertNotIn("rating", self.catalog.tables["data_for_ai_analytic_part_1"])

    def test_description_teaches_dated_join_and_surname_search(self):
        text = self.catalog.description
        self.assertIn("bds.l_azs_rm_dt_vers — руководитель управления", text)
        self.assertIn("f.account_date >= rm.dt_vers_start AND f.account_date <= rm.dt_vers_end", text)
        self.assertIn("ILIKE '%Иванов%'", text)
        self.assertIn("type_azs_name — признак наличия кафе и магазина", text)
        self.assertNotIn("COALESCE(rm.dt_vers_end", text)
        questions = [q for q, _ in self.catalog.examples]
        self.assertTrue(any("у Сидоровой и у Кузнецова" in q for q in questions))
        person = next(sql for q, sql in self.catalog.examples if "Сидоровой" in q)
        self.assertIn("UNION ALL", person)
        # Примеры со справочниками проходят валидатор (в тестовом каталоге урезанные витрины,
        # поэтому прежние примеры с их столбцами здесь не проверяются).
        reference_examples = [sql for _, sql in self.catalog.examples if "bds." in sql]
        # Четыре: у Сидоровой и Кузнецова, по ТМ общества, по типу АЗС и «кто РУ и ТМ у АЗС»
        # (решение владельца 23.09.2026 — ИИ отвечает на это по ОХД, не по карточке).
        self.assertEqual(len(reference_examples), 4)
        self.assertTrue(any(q.startswith("Кто РУ и ТМ у АЗС") for q in questions))
        for sql in reference_examples:
            validate(sql, Scope.all_network())

    def test_fast_path_rules_follow_the_catalog_dialect(self):
        rules = contract.rules()
        self.assertIn("PostgreSQL", rules)
        self.assertIn("справочникам bds", rules)
        self.assertNotIn("station_kpi_daily", rules)
        self.assertIn(rules, contract.system_prompt())


class CheckedValuesTests(DwhCase):
    values = {
        "checked_at": "2026-09-23T10:00:00", "open_end": True, "warnings": [],
        "values": {"status_azs_name": [{"value": "Действующая", "objects": 10}],
                   "type_azs_name": [{"value": "АЗС с кафе", "objects": 4}, {"value": "АЗС с магазином", "objects": 6}]},
    }

    def test_checked_values_and_open_end_reach_the_prompt(self):
        text = self.catalog.description
        self.assertIn("значения: 'Действующая'", text)
        self.assertIn("'АЗС с кафе', 'АЗС с магазином'", text)
        self.assertIn("COALESCE(rm.dt_vers_end, DATE '9999-12-31')", text)
        person = next(sql for q, sql in self.catalog.examples if "Сидоровой" in q)
        self.assertIn("COALESCE(p.dt_vers_end, DATE '9999-12-31')", person)


class ValidatorTests(DwhCase):
    def test_references_are_projected_to_allowed_columns_for_every_role(self):
        checked = validate(PERSON_SQL, Scope.all_network())
        self.assertIn("SELECT\n      ksss_code,\n      dt_vers_start,\n      dt_vers_end,\n      rm_fio\n    FROM bds.l_azs_rm_dt_vers", checked.sql)
        self.assertIn("FROM bds.l_azs_tm_dt_vers", checked.sql)          # схема дописана системой
        self.assertNotIn("ksss_code IN", checked.sql)                     # вся сеть — без фильтра
        star = validate("SELECT * FROM bds.s_azs_ksss", Scope.all_network()).sql
        self.assertIn("ksss_azs_code,\n    status_azs_name,\n    type_azs_name\n  FROM bds.s_azs_ksss", star)

    def test_scope_uses_each_tables_own_key(self):
        checked = validate(PERSON_SQL, Scope.for_stations(["101", "103"], "ТМ")).sql
        self.assertIn("ksss_azs_code IN (101, 103)", checked)
        self.assertEqual(checked.count("ksss_code IN (101, 103)"), 2)
        ref = validate("SELECT type_azs_name, COUNT(*) FROM bds.s_azs_ksss GROUP BY 1",
                       Scope.for_stations(["101"], "ТМ")).sql
        self.assertIn("ksss_azs_code IN (101)", ref)

    def test_nothing_beyond_the_four_fields_leaves_a_reference(self):
        with self.assertRaises(Rejected) as ctx:
            validate("SELECT rm_phone FROM bds.l_azs_rm_dt_vers", Scope.all_network())
        self.assertEqual(ctx.exception.rule, "unknown_column")
        # Через CTE проверка столбцов не работает, но проекция уже отрезала всё лишнее:
        # такого столбца в подзапросе нет, и витрина вернёт ошибку, а не данные.
        cte = validate("WITH x AS (SELECT * FROM bds.l_azs_rm_dt_vers) SELECT rm_phone FROM x",
                       Scope.all_network()).sql
        self.assertIn("rm_fio\n    FROM bds.l_azs_rm_dt_vers", cte)
        for sql, rule in (("SELECT * FROM bds.l_azs_other", "unknown_table"),
                          ("SELECT * FROM public.l_azs_rm_dt_vers", "qualified_table"),
                          ("SELECT * FROM dm.l_azs_rm_dt_vers", "qualified_table"),
                          ("SELECT * FROM bds.data_for_ai_analytic_part_1", "qualified_table")):
            with self.assertRaises(Rejected) as ctx:
                validate(sql, Scope.all_network())
            self.assertEqual(ctx.exception.rule, rule, sql)

    def test_facts_tables_expose_only_catalog_columns_the_query_needs(self):
        one = validate("SELECT SUM(vd_ntu) FROM dm.data_for_ai_analytic_part_1", Scope.all_network()).sql
        self.assertIn("SELECT\n    vd_ntu\n  FROM dm.data_for_ai_analytic_part_1", one)
        star = validate("SELECT * FROM dm.data_for_ai_analytic_part_1", Scope.all_network()).sql
        self.assertIn("sum_receipt_netto_ntu", star)
        self.assertNotIn("rating", star)                                 # закрыто решением владельца
        cte = validate("WITH x AS (SELECT * FROM dm.data_for_ai_analytic_part_1) SELECT rating FROM x",
                       Scope.all_network()).sql
        self.assertNotIn("rating\n  FROM dm.", cte)                     # в подзапросе rating нет — витрина откажет
        count = validate("SELECT COUNT(*) FROM dm.data_for_ai_analytic_part_1", Scope.all_network()).sql
        self.assertIn("SELECT\n    ksss_azs_code\n  FROM dm.data_for_ai_analytic_part_1", count)
        using = validate("SELECT SUM(f.vd_ntu) FROM dm.data_for_ai_analytic_part_1 f "
                         "JOIN dm.data_for_ai_analytic_part_2 p USING (ksss_azs_code, account_date)",
                         Scope.all_network()).sql
        self.assertIn("ksss_azs_code,\n    account_date\n  FROM dm.data_for_ai_analytic_part_2", using)


class AgentToolTests(DwhCase):
    def test_dimension_values_use_reference_schema_and_key(self):
        seen = []

        def run_query(sql, limit):
            seen.append(sql)
            return Result(columns=["value", "objects"], rows=[("Сидорова Анна", 12)], elapsed_ms=1, truncated=False)

        dwh = semantic.load(self.catalog)
        ctx = tools.ToolContext(question="т", scope=Scope.for_stations(["101"], "ТМ"), budget=Budget.for_depth("analyze"),
                                catalog=self.catalog, semantic=dwh, run_query=run_query)
        self.assertEqual(ctx.qualified("l_azs_tm_dt_vers"), "bds.l_azs_tm_dt_vers")
        out = tools.call(ctx, "get_dimension_values", {"dimension": "тм", "search": "сидоров"})
        self.assertEqual(out["values"], [{"value": "Сидорова Анна", "objects": 12}])
        self.assertIn("COUNT(DISTINCT ksss_code)", seen[0])
        self.assertIn("FROM bds.l_azs_tm_dt_vers", seen[0])
        self.assertIn("ksss_code IN (101)", seen[0])


class SemanticTests(unittest.TestCase):
    def test_dwh_profile_knows_people_status_and_type(self):
        dwh = semantic.DWH
        self.assertEqual(dwh.dimension("ру").column, "rm_fio")
        self.assertEqual(dwh.dimension("тм").table, "l_azs_tm_dt_vers")
        self.assertEqual(dwh.dimension("по формату").column, "type_azs_name")
        self.assertEqual(dwh.dimension("статус").column, "status_azs_name")
        self.assertFalse(dwh.find("выручка по ТМ и РУ")["absent"])
        self.assertTrue(dwh.find("выручка у управляющего АЗС")["absent"])
        self.assertTrue(any("dt_vers_start" in rule for rule in dwh.rules))
        self.assertIn("rm_join", dwh.entity)


if __name__ == "__main__":
    unittest.main()
