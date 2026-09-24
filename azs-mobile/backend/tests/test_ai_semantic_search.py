"""Поиск показателей в семантическом слое витрины ОХД (semantic.find): точность и ложные совпадения.

Замер 24.09.2026 на этих наборах. Основной — вопросы эталонного набора и типовые
формулировки: до правки верный показатель первым в 32 из 48, среди трёх первых —
в 42, ложные показатели — в 9 вопросах, ложные измерения — в 3 из 6; после — 48, 48,
0 и 0. Отложенный (по нему ничего не настраивали): первым — 14 → 21 из 25, среди
трёх — 20 → 25, ложные — 4 → 0.

Причины ошибок были две: короткие синонимы совпадали внутри слов («ус» в «успеваю»,
«мп» в «компании», «ру» в «выручке», «аб» в «работе», «кл» в «клиентах»), а полное
совпадение нескольких слов в другом падеже («конверсию НТУ») весило 0,7 — меньше,
чем одно короткое «НТУ» (0,93).
"""
from __future__ import annotations

import unittest

from backend.ai import semantic

QUESTIONS = [
    # вопрос, ожидаемые показатели (любой из), показатели, которых быть не должно
    ("Какая выручка НТУ у моих объектов за сентябрь", {"revenue_ntu"}, set()),
    ("Успеваю ли я по графику плана НТУ", {"plan_completion_ntu", "plan_ntu_revenue"}, {"avg_rating"}),
    ("Сколько чеков на АЗС 5044 за вчера", {"traffic", "checks_per_station"}, set()),
    ("Сколько я продал топлива за месяц", {"fuel_volume", "fuel_weight"}, set()),
    ("Какая средняя заправка по моей группе", {"avg_fill"}, set()),
    ("Покажи конверсию по всем моим объектам таблицей", {"conversion_ntu"}, set()),
    ("Сколько оценок в приложении пришло за сентябрь", {"ratings"}, set()),
    ("Почему упала конверсия на моём худшем объекте", {"conversion_ntu"}, set()),
    ("Что сделать, чтобы закрыть план по НТУ", {"plan_completion_ntu", "plan_ntu_revenue"}, set()),
    ("У меня растёт негатив, что делать", {"negative_ratings"}, set()),
    ("Почему у меня средний чек ниже, чем в группе", {"avg_check_ntu"}, set()),
    ("Выполню ли я план по НТУ до конца месяца", {"plan_completion_ntu", "plan_ntu_revenue"}, set()),
    ("Какой ВД по моим АЗС за сентябрь", {"vd_ntu"}, set()),
    ("Покажи КС по моим объектам", {"service_quality"}, set()),
    ("Какой у меня УС", {"avg_rating"}, set()),
    ("Сколько жалоб ЕГЛ по зоне за месяц", {"complaints"}, set()),
    ("Доля чеков с КЛ по моим АЗС", {"loyalty_share"}, set()),
    ("Сравни конверсию НТУ по обществам", {"conversion_ntu"}, set()),
    ("Средний чек НТУ по ОНПО за сентябрь", {"avg_check_ntu"}, set()),
    ("Выручка НТУ по ОНПО за август", {"revenue_ntu"}, set()),
    ("Какой ТМ у меня лучший по конверсии", {"conversion_ntu"}, set()),
    ("Сколько топлива продали в тоннах за неделю", {"fuel_weight"}, set()),
    ("Реализация дизельного топлива за месяц", {"fuel_volume_dt"}, set()),
    ("Продажи бензина к прошлому году", {"fuel_volume_ab"}, set()),
    ("ВД на клиента по моим АЗС", {"vd_per_client"}, {"loyalty_checks"}),
    ("Сколько клиентов было на АЗС вчера", {"traffic"}, {"loyalty_checks"}),
    ("Маржа НТУ по регионам", {"margin_ntu"}, set()),
    ("Количество работающих АЗС в сети", {"active_stations"}, set()),
    ("ВД кафе за сентябрь", {"vd_cafe"}, set()),
    ("Сколько негатива по кассе на моих АЗС", {"neg_cashier"}, set()),
    ("Сколько негатива по чистоте на моих АЗС", {"neg_clean"}, set()),
    ("Средняя оценка в мобильном приложении", {"avg_rating"}, set()),
    ("Выполнение плана выручки НТУ по обществам в текущем месяце", {"plan_completion_ntu"}, set()),
    ("Как изменились чеки относительно прошлого месяца", {"traffic"}, set()),
    ("Какие АЗС работают хуже всех по выручке", {"revenue", "revenue_ntu"}, {"fuel_volume_ab"}),
    ("Сравни двух РУ по ВД НТУ", {"vd_ntu"}, set()),
    ("Сколько товаров НТУ продано в штуках", {"items_ntu"}, set()),
    ("Комплексность чека НТУ", {"complexity_ntu"}, set()),
    ("Покрытие расходов по АЗС", {"coverage"}, set()),
    ("Какой план топлива B2C на сентябрь", {"plan_fuel_b2c"}, set()),
    ("Выручка от топлива без НДС за неделю", {"revenue_fuel"}, set()),
    ("Продажи НТУ на 100 клиентов", {"items_per_100"}, {"loyalty_checks"}),
    ("Работа компании в сентябре: общая выручка", {"revenue"}, {"neg_app", "fuel_volume_ab"}),
    ("Максимальная выручка НТУ за день", {"revenue_ntu"}, {"service_quality"}),
    ("Успеваю ли я по плану выручки НТУ", {"plan_completion_ntu", "plan_ntu_revenue"}, {"avg_rating"}),
    ("Импортные товары: ВД непродовольственных товаров", {"vd_neprod"}, {"neg_app"}),
    ("Средняя цена товара НТУ", {"avg_price_ntu"}, set()),
    ("Сравни конверсию НТУ по территориальным менеджерам моего управления", {"conversion_ntu"}, set()),
]

DIMENSIONS = [
    ("Выручка НТУ по ОНПО за август", {"npo"}, {"regional_manager"}),
    ("Какие АЗС работают хуже всех по выручке", {"station"}, {"regional_manager"}),
    ("Сравни двух РУ по ВД НТУ", {"regional_manager"}, set()),
    ("Какой ТМ у меня лучший по конверсии", {"territory_manager"}, set()),
    ("Группа сопоставимых АЗС по конверсии", {"station"}, {"regional_manager"}),
    ("Выручка в деньгах по дням", {"day"}, set()),
]


HELD_OUT = [
    ("Какой трафик был на моих станциях в выходные", {"traffic"}, set()),
    ("Сколько литров бензина продали за неделю", {"fuel_volume_ab", "fuel_volume"}, set()),
    ("Динамика среднего чека по магазину", {"avg_check_ntu"}, set()),
    ("Какая маржинальность НТУ в сентябре", {"margin_ntu"}, set()),
    ("Сравни качество сервиса по обществам", {"service_quality"}, set()),
    ("Сколько жалоб поступило на мои АЗС", {"complaints"}, set()),
    ("Какая доля нетопливных чеков", {"conversion_ntu", "checks_ntu"}, set()),
    ("Покажи валовой доход магазина по регионам", {"vd_ntu"}, set()),
    ("Сколько баллов списали клиенты", {"points_out"}, {"loyalty_checks"}),
    ("Какие АЗС отстают от плана выручки НТУ", {"plan_completion_ntu", "plan_ntu_revenue"}, set()),
    ("Средняя оценка клиентов в приложении за неделю", {"avg_rating"}, set()),
    ("Количество плохих оценок по чистоте", {"neg_clean", "negative_ratings"}, set()),
    ("Продажи в кафе за месяц", {"vd_cafe", "items_cafe"}, set()),
    ("Сколько дизеля ушло юрлицам", {"fuel_volume_dt", "fuel_volume_b2b"}, set()),
    ("Где упала посещаемость", {"traffic"}, set()),
    ("Опекс по моим объектам", {"opex"}, set()),
    ("Сколько чеков в день в среднем на станции", {"checks_per_day", "checks_per_station", "traffic"}, set()),
    ("Доля клиентов с картой лояльности", {"loyalty_share"}, set()),
    ("Какая работа по конверсии в кафе у группы", {"conversion_ntu"}, {"fuel_volume_ab", "regional_manager"}),
    ("Выручка магазина по ТМ моего управления", {"revenue_ntu"}, set()),
    ("План по ВД НТУ на месяц", {"plan_ntu_vd"}, set()),
    ("Комплексные чеки с топливом и НТУ", {"complex_checks"}, set()),
    ("Уровень сервиса по моим АЗС", {"avg_rating"}, set()),
    ("Импорт данных: выручка без НДС по топливу", {"revenue_fuel"}, {"neg_app"}),
    ("Как дела с максимальной выручкой НТУ в выходные", {"revenue_ntu"}, {"service_quality"}),
]



class SemanticSearchTests(unittest.TestCase):
    def test_expected_metric_comes_first(self):
        for question, want, _never in QUESTIONS:
            with self.subTest(question=question):
                found = semantic.DWH.find(question)["metrics"]
                self.assertIn(found[0]["key"], want, [(m["key"], m["score"]) for m in found[:3]])

    def test_held_out_questions_find_the_metric_among_three(self):
        first = 0
        for question, want, _never in HELD_OUT:
            with self.subTest(question=question):
                keys = [m["key"] for m in semantic.DWH.find(question)["metrics"][:3]]
                self.assertTrue(set(keys) & want, keys)
                first += keys[0] in want
        self.assertGreaterEqual(first, 21)  # на 24.09.2026 — 21 из 25

    def test_no_false_metrics_or_dimensions(self):
        for question, _want, never in QUESTIONS + HELD_OUT:
            strong = {m["key"] for m in semantic.DWH.find(question)["metrics"][:3] if m["score"] >= 0.7}
            self.assertFalse(strong & never, question)
        for question, want, never in DIMENSIONS:
            dims = {d["key"] for d in semantic.DWH.find(question)["dimensions"] if d["score"] >= 0.7}
            self.assertTrue(dims & want, question)
            self.assertFalse(dims & never, question)

    def test_abbreviations_match_only_as_whole_words(self):
        def top(question):
            return semantic.DWH.find(question)["metrics"][0]["key"]
        self.assertEqual(top("Какой у меня УС"), "avg_rating")
        self.assertEqual(top("Покажи КС по моим объектам"), "service_quality")
        rated = {m["key"] for m in semantic.DWH.find("Успеваю ли я по плану")["metrics"]}
        self.assertNotIn("avg_rating", rated)  # «ус» внутри «успеваю» — не уровень сервиса
        dims = {d["key"] for d in semantic.DWH.find("Выручка по сети")["dimensions"] if d["score"] >= 0.5}
        self.assertNotIn("regional_manager", dims)  # «ру» внутри «выручки» — не руководитель управления

    def test_generic_words_do_not_pick_the_station_count(self):
        self.assertNotEqual(semantic.DWH.find("Сколько чеков на АЗС 5044 за вчера")["metrics"][0]["key"],
                            "active_stations")
        self.assertEqual(semantic.DWH.find("Сколько АЗС в сети")["metrics"][0]["key"], "active_stations")


if __name__ == "__main__":
    unittest.main()
