"""Места из вопроса (28.09.2026): регион, общество и город — точные значения справочника.

Модель тратила 3–8 ходов на поиск написания места; теперь их находит код до обращения к
модели, по справочнику в области данных пользователя. Справочник подменён: проверяются
сопоставление по основе слова, город → регион там, где городов в витрине нет, аббревиатуры,
область данных в запросе справочника, кэш и тишина при ошибке.
"""
from __future__ import annotations

import unittest

from backend.ai import executor, places, semantic
from backend.ai.agent import prompts
from backend.ai.validator import Scope
from backend.tests.test_ai_agent import StandCase

STAND_VALUES = {
    "region": [("г. Москва", 213), ("Московская обл.", 200), ("Краснодарский край", 164), ("Пермский край", 129),
               ("Вологодская обл.", 130), ("г. Санкт-Петербург", 119), ("Республика Башкортостан", 67)],
    "npo": [("ЦНП", 924), ("ЮНП", 733), ("УНП", 709), ("СЗНП", 404)],
    "city": [("г. Москва", 211), ("г. Пермь", 44), ("г. Нижний Новгород", 44), ("г. Великий Новгород", 9),
             ("прочее", 118)],
}
DWH_VALUES = {"region": [("Пермский край", 57), ("Краснодарский край", 140), ("Новосибирская область", 30)],
              "npo": [("ООО «ЛУКОЙЛ-Пермнефтепродукт»", 57)]}


def found(question, values=STAND_VALUES, sem=semantic.STAND):
    return [(p.word, p.dimension, p.value, p.via_city)
            for p in places.find(question, values, sem, StandCase._stand_catalog())]


class MatchTests(unittest.TestCase):
    def test_case_forms_of_regions_and_cities(self):
        self.assertEqual(found("Выручка НТУ в Пермском крае за август"), [("Пермском", "region", "Пермский край", False)])
        self.assertEqual(found("Сравни АЗС в Перми и в Нижнем Новгороде"),
                         [("Перми", "city", "г. Пермь", False), ("Нижнем Новгороде", "city", "г. Нижний Новгород", False)])
        self.assertIn(("Вологодской", "region", "Вологодская обл.", False), found("Конверсия в Вологодской области"))

    def test_all_significant_words_must_match(self):
        # «Новгороде» без «Нижнем» — ни Нижний, ни Великий: не угадываем.
        self.assertEqual([f for f in found("АЗС в Новгороде") if f[1] == "city"], [])

    def test_abbreviations_match_exactly(self):
        self.assertEqual(found("Выручка по ЦНП и ЮНП"), [("ЦНП", "npo", "ЦНП", False), ("ЮНП", "npo", "ЮНП", False)])
        self.assertEqual(found("Выручка по цнп"), [])

    def test_ordinary_words_are_not_places(self):
        self.assertEqual(found("Сравни выручку НТУ за сентябрь с прошлым годом, где просадка"), [])
        self.assertEqual(places.candidates("Сравни выручку НТУ за сентябрь с прошлым годом"), [])
        self.assertEqual(places.candidates("Как дела в Перми и по ЦНП"), ["Перми", "ЦНП"])

    def test_city_points_to_its_region_where_there_are_no_cities(self):
        result = found("Как менялась выручка в Перми", DWH_VALUES, semantic.DWH)
        self.assertEqual(result, [("Перми", "region", "Пермский край", True)])
        block = places.Resolution(places.find("в Перми", DWH_VALUES, semantic.DWH, StandCase._stand_catalog())).prompt_block()
        self.assertIn("все АЗС региона", block)
        self.assertIn("region_name = 'Пермский край'", block)
        # Где города есть, город не превращается в регион.
        self.assertNotIn(("Перми", "region", "Пермский край", True), found("выручка в Перми"))

    def test_prompt_carries_the_block(self):
        text = prompts.agent_user("Выручка в Перми", {"taskType": "lookup", "depth": "deep"}, "вся сеть", "2026-09-28",
                                  None, {"metrics": [], "dimensions": [], "absent": [], "places": "Места из вопроса — тест"},
                                  {"sql": 5, "python": 2, "charts": 2})
        self.assertIn("Места из вопроса — тест", text)


class DirectoryTests(StandCase):
    def setUp(self):
        super().setUp()
        places.clear_cache()
        self.queries = []

    def tearDown(self):
        places.clear_cache()
        super().tearDown()

    def _run_query(self, sql, limit):
        self.queries.append(sql)
        return executor.run(sql, limit)

    def test_directory_goes_through_the_scope(self):
        codes = ["10005", "10006"]
        scope = Scope.for_stations(codes, "две АЗС")
        places.resolve("Выручка в Перми", scope, catalog=self._stand_catalog(), semantic=semantic.STAND,
                       run_query=self._run_query)
        self.assertTrue(self.queries)
        self.assertTrue(all("10005" in sql for sql in self.queries))       # фильтр области данных

    def test_no_place_words_no_query_and_cache_is_reused(self):
        catalog = self._stand_catalog()
        self.assertIsNone(places.resolve("Сравни выручку НТУ с прошлым годом", self.scope, catalog=catalog,
                                         semantic=semantic.STAND, run_query=self._run_query))
        self.assertEqual(self.queries, [])
        places.resolve("Выручка в Перми", self.scope, catalog=catalog, semantic=semantic.STAND, run_query=self._run_query)
        first = len(self.queries)
        places.resolve("Выручка в Москве", self.scope, catalog=catalog, semantic=semantic.STAND, run_query=self._run_query)
        self.assertEqual(len(self.queries), first)                          # из кэша

    def test_failure_is_silent(self):
        def broken(sql, limit):
            raise RuntimeError("витрина недоступна")
        self.assertIsNone(places.resolve("Выручка в Перми", self.scope, catalog=self._stand_catalog(),
                                         semantic=semantic.STAND, run_query=broken))


if __name__ == "__main__":
    unittest.main()
