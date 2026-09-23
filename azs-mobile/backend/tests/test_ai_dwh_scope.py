"""Область данных ИИ на витрине ОХД — по справочникам самой витрины.

Решение владельца 23.09.2026: ИИ живёт в ДВХ. Проверяется, что при
AI_DB_BACKEND=postgres список объектов РУ, ТМ и ОНПО берётся запросом к ОХД,
а не из локального справочника; что значение привязки экранируется; что
отказ витрины даёт пустую область, а не доступ ко всей сети; что на стенде
SQLite всё осталось как было. Соединения с ОХД нет: исполнитель подменён.
Фамилии вымышленные.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from backend import roles
from backend.ai import dwh_scope, executor, scope
from backend.ai.catalog import Catalog
from backend.ai.executor import ExecutionError, Result

DWH = Catalog(
    tables={"data_for_ai_analytic_part_1": {"ksss_azs_code", "npo", "account_date"},
            "l_azs_rm_dt_vers": {"ksss_code", "rm_fio"},
            "l_azs_tm_dt_vers": {"ksss_code", "tm_fio"}},
    scoped_tables=set(), dialect="postgres", schema="dm",
    scope_column="ksss_azs_code", facts_table="data_for_ai_analytic_part_1",
    date_column="account_date",
    table_schemas={"l_azs_rm_dt_vers": "bds", "l_azs_tm_dt_vers": "bds"},
)


def _result(rows):
    return Result(columns=["k"], rows=[tuple(r) for r in rows], elapsed_ms=1, truncated=False)


class DwhScopeTest(unittest.TestCase):
    def setUp(self):
        dwh_scope.clear_cache()
        self.sql: list[str] = []
        patches = [
            mock.patch.object(executor, "BACKEND", "postgres"),
            mock.patch.object(dwh_scope, "CATALOG", DWH),
            mock.patch.dict(os.environ, {"AI_SCOPE_SOURCE": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, rows):
        def fake(sql, row_limit, timeout_s=None):
            self.sql.append(sql)
            return _result(rows)
        return mock.patch.object(executor, "run", side_effect=fake)

    def test_territory_manager_from_bds_on_today(self):
        with self._run([[5044], [9753]]):
            got = scope.build("territory_manager", "Петрова Анна Ивановна")
        self.assertFalse(got.unrestricted)
        self.assertEqual(got.ksss, ("5044", "9753"))
        self.assertIn("по ОХД", got.label)
        self.assertEqual(len(self.sql), 1)
        self.assertIn("FROM bds.l_azs_tm_dt_vers", self.sql[0])
        self.assertIn("tm_fio = 'Петрова Анна Ивановна'", self.sql[0])
        self.assertIn("CURRENT_DATE >= dt_vers_start", self.sql[0])

    def test_regional_manager_from_bds(self):
        with self._run([[1]]):
            scope.build("regional_manager", "Сидоров Олег")
        self.assertIn("FROM bds.l_azs_rm_dt_vers", self.sql[0])
        self.assertIn("rm_fio = 'Сидоров Олег'", self.sql[0])

    def test_npo_from_facts(self):
        with self._run([[7], [8]]):
            got = scope.build("aup_npo", "УНП")
        self.assertEqual(got.ksss, ("7", "8"))
        self.assertIn("FROM dm.data_for_ai_analytic_part_1", self.sql[0])
        self.assertIn("npo = 'УНП'", self.sql[0])

    def test_binding_is_escaped(self):
        with self._run([]):
            scope.build("territory_manager", "О'Нил'; DROP TABLE x; --")
        self.assertIn("'О''Нил''; DROP TABLE x; --'", self.sql[0])

    def test_local_reference_not_used(self):
        with self._run([[1]]), mock.patch.object(roles, "resolve",
                                                  side_effect=AssertionError("справочник приложения")):
            scope.build("territory_manager", "Петрова Анна Ивановна")

    def test_dwh_failure_gives_empty_scope_not_network(self):
        with mock.patch.object(executor, "run", side_effect=ExecutionError("нет соединения")):
            got = scope.build("territory_manager", "Петрова Анна Ивановна")
        self.assertFalse(got.unrestricted)
        self.assertEqual(got.ksss, ())
        self.assertIn("не ответила", got.label)
        # Ошибку не кэшируем: следующая попытка снова идёт в витрину.
        with self._run([[1]]):
            again = scope.build("territory_manager", "Петрова Анна Ивановна")
        self.assertEqual(again.ksss, ("1",))

    def test_unknown_person_is_empty(self):
        with self._run([]):
            got = scope.build("territory_manager", "Нет Такого")
        self.assertEqual(got.ksss, ())
        self.assertIn("нет объектов", got.label)

    def test_cache(self):
        with self._run([[1]]):
            scope.build("territory_manager", "Петрова Анна Ивановна")
            scope.build("territory_manager", "Петрова Анна Ивановна")
        self.assertEqual(len(self.sql), 1)

    def test_unrestricted_and_codes_without_queries(self):
        with self._run([[1]]):
            self.assertTrue(scope.build("admin").unrestricted)
            self.assertEqual(scope.build("station", "5044").ksss, ("5044",))
            self.assertEqual(scope.build("agent", "5044, 9753;9755").ksss, ("5044", "9753", "9755"))
            self.assertEqual(scope.build("territory_manager", "").ksss, ())
        self.assertEqual(self.sql, [])

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            scope.build("nobody", "x")

    def test_identities_from_dwh(self):
        with self._run([["Петрова Анна Ивановна", 45]]):
            items = dwh_scope.identities(5)
        self.assertEqual(len(self.sql), 3)
        self.assertTrue(any("bds.l_azs_tm_dt_vers" in s for s in self.sql))
        self.assertIn(("territory_manager", "Петрова Анна Ивановна", 45), items)


class StandUnchangedTest(unittest.TestCase):
    def test_sqlite_uses_app_reference(self):
        dwh_scope.clear_cache()
        with mock.patch.object(executor, "BACKEND", "sqlite"), \
             mock.patch.object(roles, "resolve",
                               return_value=roles.DataScope(ksss=("1", "2"), label="ТМ")) as resolve, \
             mock.patch.object(executor, "run", side_effect=AssertionError("на стенде ОХД не нужна")):
            got = scope.build("territory_manager", "Петрова")
        resolve.assert_called_once()
        self.assertEqual(got.ksss, ("1", "2"))

    def test_emergency_switch(self):
        with mock.patch.object(executor, "BACKEND", "postgres"), \
             mock.patch.dict(os.environ, {"AI_SCOPE_SOURCE": "reference"}):
            self.assertFalse(dwh_scope.enabled())


if __name__ == "__main__":
    unittest.main()
