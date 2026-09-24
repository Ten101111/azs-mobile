"""ИИ-17 (часть): на одной оси графика — одна единица измерения.

Литры и тонны, рубли и проценты на общей оси дают ряд, лежащий на нуле.
Код оставляет ряды в единице главного ряда, остальные снимает с пометкой,
а подпись оси берёт из названия колонки, а не из подписи модели.
"""
from __future__ import annotations

import unittest

from backend.ai.agent import charts
from backend.ai.agent.state import ResultSet


def _rs(columns, rows):
    return ResultSet(id="r1", columns=columns, rows=rows, source="sql", purpose="объём по АЗС")


class ChartUnitTests(unittest.TestCase):
    def test_unit_is_read_from_column_name(self):
        self.assertEqual(charts.column_unit("Объём, т"), "т")
        self.assertEqual(charts.column_unit("Выручка НТУ, руб"), "₽")
        self.assertEqual(charts.column_unit("Конверсия НТУ, %"), "%")
        self.assertEqual(charts.column_unit("Топливо (литры)"), "л")
        self.assertIsNone(charts.column_unit("Чеки"))

    def test_liters_and_tonnes_are_not_mixed_on_one_axis(self):
        rs = _rs(["АЗС", "Объём, л", "Объём, т"], [["А", 1_105_000, 850], ["Б", 990_000, 760]])
        chart = charts.build(rs, {"type": "bar", "x": "АЗС", "series": ["Объём, л", "Объём, т"], "unit": "т"}, "c1")
        self.assertEqual([s["name"] for s in chart["series"]], ["Объём, л"])
        self.assertEqual(chart["dropped"], ["Объём, т"])
        self.assertEqual(chart["unit"], "л")  # единица оси — из данных, а не из подписи модели
        self.assertIn("другая единица", chart["note"])

    def test_same_unit_series_stay_together(self):
        rs = _rs(["Месяц", "Выручка 2026, ₽", "Выручка 2025, ₽"], [["07", 12_000_000, 11_000], ["08", 12_400_000, 11_500]])
        chart = charts.build(rs, {"type": "line", "x": "Месяц", "series": ["Выручка 2026, ₽", "Выручка 2025, ₽"]}, "c1")
        self.assertEqual(len(chart["series"]), 2)
        self.assertNotIn("dropped", chart)

    def test_unlabelled_series_of_very_different_scale_is_dropped(self):
        rs = _rs(["Месяц", "Чеки", "Средний чек"], [["07", 41_000, 410], ["08", 42_000, 415]])
        chart = charts.build(rs, {"type": "line", "x": "Месяц", "series": ["Чеки", "Средний чек"]}, "c1")
        self.assertEqual([s["name"] for s in chart["series"]], ["Чеки"])

    def test_group_split_and_scatter_are_not_touched(self):
        rs = _rs(["Месяц", "Объём, т", "Регион"], [["07", 10, "A"], ["07", 5000, "B"]])
        grouped = charts.build(rs, {"type": "bar", "x": "Месяц", "series": ["Объём, т"], "group": "Регион"}, "c1")
        self.assertEqual(len(grouped["series"]), 2)
        scatter = charts.build(_rs(["Трафик, шт", "Конверсия, %"], [[100, 30], [200, 25]]),
                               {"type": "scatter", "x": "Трафик, шт", "series": ["Конверсия, %"]}, "c2")
        self.assertEqual(len(scatter["points"]), 2)


if __name__ == "__main__":
    unittest.main()
