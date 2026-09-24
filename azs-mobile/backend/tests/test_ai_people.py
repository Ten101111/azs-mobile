"""Кто назван в вопросе — РУ или ТМ: правила владельца от 23.09.2026.

1. РУ и ТМ между собой не сравнивают — все названные считаются на одном уровне.
2. Фамилия есть и среди РУ, и среди ТМ — речь о РУ.
3. Один назван только как РУ, другой только как ТМ — считаются отдельно, без сравнения.

Фамилии вымышленные. Справочник людей в тестах подменяется, кроме последнего
класса, где резолвер идёт через валидатор и исполнитель по временному стенду.
"""
from __future__ import annotations

import unittest
from unittest import mock

from backend.ai import generator, people, pipeline
from backend.ai.catalog import Catalog
from backend.ai.executor import Result
from backend.ai.people import Person
from backend.ai.validator import Scope
from backend.tests.test_ai_agent import StandCase
from backend.tests.test_ai_dwh_people import DwhCase

RM, TM = people.ROLE_RM, people.ROLE_TM
DWH = Catalog(tables={"l_azs_rm_dt_vers": {"ksss_code", "rm_fio"}, "l_azs_tm_dt_vers": {"ksss_code", "tm_fio"}},
              table_schemas={"l_azs_rm_dt_vers": "bds", "l_azs_tm_dt_vers": "bds"})

DIRECTORY = [
    Person("Сидорова Анна Петровна", RM, 110),
    Person("Кузнецов Павел Ильич", RM, 48),
    Person("Кузнецова Ольга Игоревна", TM, 12),      # однофамилица РУ среди ТМ
    Person("Сидоров Илья Петрович", TM, 9),          # однофамилец РУ среди ТМ
    Person("Белов Олег Юрьевич", TM, 15),
    Person("Мирская Вера Андреевна", TM, 7),
    Person("Орлов Иван Сергеевич", RM, 60),
    Person("Орлов Пётр Андреевич", RM, 30),          # два РУ с одной фамилией
]


def resolve(question: str):
    return people.resolve_mentions(people.find_mentions(question, DIRECTORY), DWH)


class SurnameTests(unittest.TestCase):
    def test_surname_and_base(self):
        self.assertEqual(people.surname("Сидорова Анна Петровна"), "Сидорова")
        self.assertEqual(people.surname("И.П. Белов Олег Юрьевич"), "Белов")
        self.assertEqual(people.surname("ИП Белов О. Ю. (ООО «Ромашка»)"), "Белов")
        self.assertEqual(people.surname_base("Сидорова"), "сидоров")
        self.assertEqual(people.surname_base("Мирская"), "мирск")
        self.assertEqual(people.surname_base("Шульц"), "шульц")

    def test_case_forms_are_found_but_ordinary_words_are_not(self):
        words = {m.word for m in people.find_mentions(
            "Какой ВД НТУ у Сидоровой, у Кузнецова и по Мирской; сколько чеков у беловых тоже", DIRECTORY)}
        self.assertEqual(words, {"Сидоровой", "Кузнецова", "Мирской"})        # «беловых»: строчное, основа короче 6
        self.assertFalse(people.find_mentions("трафик городских и трассовых АЗС за сентябрь", DIRECTORY))


class RuleTests(unittest.TestCase):
    def test_two_regional_managers_compare_on_rm_level(self):
        res = resolve("Сравни ВД НТУ у Сидоровой и у Кузнецова за сентябрь")
        self.assertEqual(res.level, RM)
        self.assertEqual({p.fio for p in res.people()}, {"Сидорова Анна Петровна", "Кузнецов Павел Ильич"})
        # Однофамильцы-ТМ отброшены по правилу владельца.
        self.assertEqual(set(res.namesakes_dropped), {"Сидоровой", "Кузнецова"})
        block = res.prompt_block()
        self.assertIn("rm_fio IN ('Сидорова Анна Петровна', 'Кузнецов Павел Ильич')", block)
        self.assertIn("bds.l_azs_rm_dt_vers", block)
        self.assertIn("ТМ не подмешивай", block)
        self.assertIn("Сравнение на уровне РУ", res.note())

    def test_single_namesake_means_rm(self):
        res = resolve("Какая выручка НТУ у Сидоровой?")
        self.assertEqual(res.level, RM)
        self.assertEqual([p.fio for p in res.people()], ["Сидорова Анна Петровна"])

    def test_two_territory_managers_compare_on_tm_level(self):
        res = resolve("Сравни конверсию у Белова и Мирской")
        self.assertEqual(res.level, TM)
        self.assertIn("tm_fio IN (", res.prompt_block())
        self.assertIn("bds.l_azs_tm_dt_vers", res.prompt_block())

    def test_rm_and_tm_are_never_compared(self):
        res = resolve("Сравни ВД у Орлова и Белова")
        self.assertEqual(res.level, "mixed")
        self.assertEqual({p.role for p in res.people()}, {RM, TM})
        self.assertIn("не сопоставляй и не ранжируй", res.prompt_block())
        self.assertIn("без сравнения", res.note())

    def test_namesakes_within_one_role_are_shown_separately(self):
        res = resolve("Сколько чеков у Орлова в августе?")
        self.assertEqual(res.level, RM)
        self.assertEqual(len(res.people()), 2)
        self.assertIn("несколько человек с такой фамилией", res.prompt_block())

    def test_nobody_named(self):
        self.assertIsNone(resolve("Сколько чеков было вчера?"))


class DirectoryTests(DwhCase):
    def test_directory_goes_through_validator_with_scope_and_is_cached(self):
        people.clear_cache()
        seen = []

        def run_query(sql, limit):
            seen.append(sql)
            if "rm_fio" in sql:
                return Result(columns=["fio", "objects"], rows=[("Сидорова Анна Петровна", 3)], elapsed_ms=1, truncated=False)
            return Result(columns=["fio", "objects"], rows=[("Сидоров Илья Петрович", 1), ("отсутствует", 2)],
                          elapsed_ms=1, truncated=False)

        scope = Scope.for_stations(["101", "103"], "ТМ")
        found = people.directory(scope, self.catalog, run_query)
        self.assertEqual({(p.fio, p.role) for p in found}, {("Сидорова Анна Петровна", RM), ("Сидоров Илья Петрович", TM)})
        self.assertEqual(len(seen), 2)
        self.assertIn("FROM bds.l_azs_rm_dt_vers", seen[0])
        self.assertIn("ksss_code IN (101, 103)", seen[0])
        people.directory(scope, self.catalog, run_query)
        self.assertEqual(len(seen), 2)                                   # второй раз — из кэша
        res = people.resolve("Выручка у Сидоровой", scope, self.catalog, run_query)
        self.assertEqual(res.level, RM)
        people.clear_cache()

    def test_unreachable_directory_means_no_hint(self):
        people.clear_cache()

        def broken(sql, limit):
            raise RuntimeError("нет соединения")

        self.assertIsNone(people.resolve("Выручка у Сидоровой", Scope.all_network(), self.catalog, broken))


class PipelineTests(StandCase):
    """Стенд: РУ и ТМ из stations. У «РУ Один» и «ТМ Один» одна фамилия — значит, речь о РУ."""

    def test_fast_path_gets_people_context_and_note(self):
        people.clear_cache()
        seen = {}

        def fake_generate(question, feedback=None, model=None, context="", **_):
            seen["context"] = context
            sql = ("SELECT s.regional_manager AS \"РУ\", SUM(d.checks) AS \"Чеки\" FROM station_kpi_daily d "
                   "JOIN stations s ON s.ksss = d.ksss WHERE s.regional_manager IN ('РУ Один') GROUP BY 1")
            return generator.Generated(sql=sql, raw=sql, model="test", elapsed_ms=1)

        with mock.patch.object(pipeline.generator, "generate", fake_generate), \
                mock.patch.object(pipeline.generator, "narrate", lambda *a, **k: ("готово", 1)):
            answer = pipeline.ask("Сколько чеков у Одина?", "admin", None, "test", depth="fast")
        self.assertTrue(answer.ok, answer.error)
        self.assertIn("ЛЮДИ В ВОПРОСЕ", seen["context"])
        self.assertIn("regional_manager IN ('РУ Один')", seen["context"])
        self.assertTrue(answer.notes[0].startswith("Руководители определены по справочникам"))
        people.clear_cache()


if __name__ == "__main__":
    unittest.main()
