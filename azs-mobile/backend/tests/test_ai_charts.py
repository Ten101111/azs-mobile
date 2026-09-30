"""ИИ-17: графики только на числах результата, сравнение периодов, KPI с отклонением.

Модель называет результат и колонки — числа подставляет код. Сравнение периодов
выравнивает дни: неполный текущий месяц сравнивается с теми же днями прошлого.
«Лёгкий» получает простой график без участия модели.
"""
from __future__ import annotations

import unittest
from unittest import mock

from backend.ai import export, generator, pipeline
from backend.ai.agent import charts, tools
from backend.ai.agent.state import Budget, ResultSet
from backend.tests.test_ai_agent import StandCase

REVENUE = "Выручка НТУ, ₽"


def daily(rows):
    return ResultSet(id="r1", columns=["Дата", REVENUE], rows=rows, source="sql", purpose="выручка НТУ по дням")


def september():
    """Сентябрь 2025 целиком и 1–25 сентября 2026 (месяц ещё идёт)."""
    return daily([[f"2025-09-{d:02d}", 100.0 + d] for d in range(1, 31)]
                 + [[f"2026-09-{d:02d}", 110.0 + d] for d in range(1, 26)])


class ChartSpecTests(unittest.TestCase):
    def test_numbers_come_only_from_the_result(self):
        rs = september()
        for bad in ({"type": "bar", "x": "Дата", "values": [1, 2, 3]},
                    {"type": "line", "x": "Дата", "data": {"a": 1}},
                    {"type": "kpi", "columns": ["Нет такой"]},
                    {"type": "kpi", "columns": [REVENUE], "base": {REVENUE: "План"}},
                    {"type": "waterfall", "x": "Дата", "series": [REVENUE], "start": 12345}):
            with self.assertRaises(tools.ToolError, msg=str(bad)):
                charts.build(rs, bad, "c1")
        chart = charts.build(rs, {"type": "line", "x": "Дата", "series": [REVENUE]}, "c1")
        self.assertEqual((chart["source"], chart["sourceKind"], chart["sourceTitle"]), ("r1", "sql", "выручка НТУ по дням"))
        self.assertEqual(chart["period"], "01.09.2025 – 25.09.2026")
        # Начало водопада — число, которое в результате есть, принимается.
        wf = charts.build(rs, {"type": "waterfall", "x": "Дата", "series": [REVENUE], "start": 101}, "c2")
        self.assertEqual(wf["start"], 101)

    def test_create_chart_needs_an_existing_result(self):
        ctx = tools.ToolContext(question="тест", scope=None, budget=Budget.for_depth("deep"))
        with self.assertRaises(tools.ToolError):
            charts.create_chart(ctx, source="r9", type="line")
        ctx.workspace.add(september())
        charts.create_chart(ctx, source="r1", type="line", x="Дата", series=[REVENUE])
        self.assertEqual(ctx.workspace.charts[0]["source"], "r1")

    def test_compare_uses_the_same_days_and_names_both_periods(self):
        chart = charts.build(september(), {"type": "compare", "x": "Дата", "series": [REVENUE], "total": "sum"}, "c1")
        self.assertEqual(chart["type"], "compare")
        self.assertEqual([s["name"] for s in chart["series"]], ["1–25 сен 2026", "1–25 сен 2025"])
        self.assertEqual(len(chart["x"]), 25)
        self.assertEqual(chart["x"][0], "01.09")
        totals = [p["total"] for p in chart["periods"]]
        self.assertEqual(totals, [sum(110.0 + d for d in range(1, 26)), sum(100.0 + d for d in range(1, 26))])
        self.assertEqual(chart["changePct"], round((totals[0] / totals[1] - 1) * 100, 1))
        self.assertIn("по 25 дн.", chart["note"])
        self.assertEqual(chart["unit"], "₽")
        self.assertEqual(chart["period"], "1–25 сен 2026 и 1–25 сен 2025")

    def test_compare_month_to_month_drops_days_without_pair(self):
        rs = daily([[f"2026-08-{d:02d}", 10.0] for d in range(1, 32)] + [[f"2026-09-{d:02d}", 12.0] for d in range(1, 31)])
        chart = charts.build(rs, {"type": "compare", "x": "Дата", "series": [REVENUE], "shift": "month", "total": "avg"}, "c1")
        self.assertEqual([s["name"] for s in chart["series"]], ["1–30 сен 2026", "1–30 авг 2026"])
        self.assertEqual(chart["xTitle"], "День месяца")
        self.assertEqual(chart["changePct"], 20.0)
        monthly = ResultSet(id="r2", columns=["Месяц", "Литры, л"],
                            rows=[[f"2025-{m:02d}", 1000 + m] for m in range(1, 13)] + [[f"2026-{m:02d}", 1100 + m] for m in range(1, 9)],
                            source="sql")
        chart = charts.build(monthly, {"type": "compare", "x": "Месяц", "series": ["Литры, л"]}, "c2")
        self.assertEqual(chart["x"], ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг"])
        self.assertIn("по 8 мес.", chart["note"])

    def test_compare_refuses_what_it_cannot_align(self):
        only_now = daily([[f"2026-09-{d:02d}", 1.0] for d in range(1, 10)])
        with self.assertRaisesRegex(tools.ToolError, "прошлого года"):
            charts.build(only_now, {"type": "compare", "x": "Дата", "series": [REVENUE]}, "c1")
        doubled = daily([["2025-09-01", 1.0], ["2025-09-01", 2.0], ["2026-09-01", 3.0], ["2026-09-02", 4.0]])
        with self.assertRaisesRegex(tools.ToolError, "сгруппируй"):
            charts.build(doubled, {"type": "compare", "x": "Дата", "series": [REVENUE]}, "c1")
        text_x = ResultSet(id="r3", columns=["ОНПО", REVENUE], rows=[["А", 1], ["Б", 2]], source="sql")
        with self.assertRaisesRegex(tools.ToolError, "не даты"):
            charts.build(text_x, {"type": "compare", "x": "ОНПО", "series": [REVENUE]}, "c1")

    def test_kpi_deviation_from_base_is_counted_by_code(self):
        rs = ResultSet(id="r1", columns=["Факт, ₽", "План, ₽"], rows=[[950, 1000]], source="sql")
        card = charts.build(rs, {"type": "kpi", "columns": ["Факт, ₽"], "base": {"Факт, ₽": "План, ₽"}}, "c1")["cards"][0]
        self.assertEqual((card["base"], card["baseLabel"], card["delta"], card["deltaPct"]), (1000, "План, ₽", -50.0, -5.0))
        columns, rows = export._chart_table({"type": "kpi", "cards": [card]})
        self.assertEqual(columns, ["Показатель", "Значение", "База", "С чем сравнение", "Изменение", "Изменение, %"])
        self.assertEqual(rows[0][-1], -5.0)

    def test_no_more_than_six_series(self):
        names = "АБВГДЕЖЗ"
        long = ResultSet(id="r1", columns=["Месяц", "ОНПО", REVENUE],
                         rows=[[m, o, i * 10] for m in ("2026-07", "2026-08") for i, o in enumerate(names, 1)], source="sql")
        chart = charts.build(long, {"type": "line", "x": "Месяц", "series": [REVENUE], "group": "ОНПО"}, "c1")
        self.assertEqual([s["name"] for s in chart["series"]], list("ВГДЕЖЗ"))   # крупнейшие, в порядке данных
        self.assertIn("6 рядов из 8", chart["note"])
        wide = ResultSet(id="r2", columns=["Месяц"] + [f"Ряд {i}, ₽" for i in range(8)],
                         rows=[["2026-07"] + list(range(8)), ["2026-08"] + list(range(8))], source="sql")
        chart = charts.build(wide, {"type": "line", "x": "Месяц"}, "c2")
        self.assertLessEqual(len(chart["series"]), 6)


class AutoChartTests(unittest.TestCase):
    def test_simple_charts_only_when_obvious(self):
        line = charts.auto_chart(["Месяц", REVENUE], [["2026-06", 1], ["2026-07", 2], ["2026-08", 3]])
        self.assertEqual((line["type"], line["source"], line["auto"], line["period"]), ("line", "r1", True, "июн 2026 – авг 2026"))
        bar = charts.auto_chart(["ОНПО", REVENUE, "Чеки, шт"], [["А", 1, 5], ["Б", 2, 6], ["В", 3, 7]])
        self.assertEqual(bar["type"], "bar")
        self.assertEqual([s["name"] for s in bar["series"]], [REVENUE])        # другая единица — не на ту же ось
        self.assertNotIn("note", bar)
        self.assertEqual(charts.auto_chart(["Номер АЗС", REVENUE], [[101, 1], [102, 2], [103, 3]])["type"], "bar")
        self.assertIsNone(charts.auto_chart(["РУ", "АЗС", REVENUE], [["a", "x", 1]] * 4))     # два текстовых столбца
        self.assertIsNone(charts.auto_chart(["ОНПО", REVENUE], [["А", 1], ["Б", 2]]))           # мало строк
        self.assertIsNone(charts.auto_chart(["АЗС", REVENUE], [[f"a{i}", i] for i in range(40)]))  # длинный список


class FastPathChartTests(StandCase):
    def test_light_level_gets_a_chart_built_by_code(self):
        def fake_generate(question, feedback=None, model=None, context="", **_):
            sql = 'SELECT period AS "Месяц", SUM(checks) AS "Чеки, шт" FROM station_kpi_daily GROUP BY period ORDER BY period'
            return generator.Generated(sql=sql, raw=sql, model="test", elapsed_ms=1)

        with mock.patch.object(pipeline.generator, "generate", fake_generate), \
                mock.patch.object(pipeline.generator, "narrate", lambda *a, **k: ("готово", 1)):
            few = pipeline.ask("Чеки по месяцам", "admin", None, "test", depth="fast")
        self.assertTrue(few.ok, few.error)
        # Два месяца на стенде — меньше трёх точек: графика нет, остаётся таблица.
        self.assertEqual(few.charts, [])

        def daily_generate(question, feedback=None, model=None, context="", **_):
            sql = ('SELECT metric_date AS "Дата", SUM(checks) AS "Чеки, шт" FROM station_kpi_daily '
                   'GROUP BY metric_date ORDER BY metric_date')
            return generator.Generated(sql=sql, raw=sql, model="test", elapsed_ms=1)

        with mock.patch.object(pipeline.generator, "generate", daily_generate), \
                mock.patch.object(pipeline.generator, "narrate", lambda *a, **k: ("готово", 1)):
            answer = pipeline.ask("Чеки по дням", "admin", None, "test", depth="fast")
        self.assertTrue(answer.ok, answer.error)
        self.assertEqual(len(answer.charts), 1)
        chart = answer.charts[0]
        self.assertEqual((chart["type"], chart["source"]), ("line", "r1"))
        self.assertEqual(chart["series"][0]["values"], [float(row[1]) for row in answer.rows])
        part = export.part_of({"charts": answer.charts, "sql": answer.sql, "depth": "fast"}, "chart-c1")
        self.assertEqual(part["sql"], answer.sql)


if __name__ == "__main__":
    unittest.main()
