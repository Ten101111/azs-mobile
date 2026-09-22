"""Аналитический агент: семантика, инструменты, песочница, графики, цикл, память.

Модель здесь подменена сценарием: проверяется контур (валидатор, исполнитель,
проверки качества, песочница, привязка графиков к данным, сверка чисел,
память диалога), а не качество рассуждений. Данные — маленький стенд во
временных файлах, чтобы тесты не зависели от 500-мегабайтной базы.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import tempfile
import unittest

from backend import roles
from backend.ai import executor, journal, pipeline, semantic
from backend.ai import scope as scope_builder
from backend.ai.agent import checks, charts, grounding, llm, loop, memory, sandbox, tools
from backend.ai.agent.state import Analysis, Budget, Plan, ResultSet, Workspace


def _build_stand(folder: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    kpi = folder / "kpi.sqlite3"
    ref = folder / "ref.sqlite3"
    conn = sqlite3.connect(kpi)
    conn.execute("""CREATE TABLE station_kpi_daily (
        metric_date TEXT NOT NULL, period TEXT NOT NULL, ksss TEXT NOT NULL,
        revenue REAL NOT NULL DEFAULT 0, revenue_ntu REAL, fuel_volume REAL NOT NULL DEFAULT 0,
        checks REAL NOT NULL DEFAULT 0, checks_ntu REAL, avg_check REAL,
        updated_at TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (metric_date, ksss))""")
    rows = []
    for ksss, base in (("1001", 300), ("1002", 200), ("1003", 120)):
        for month, factor in (("2026-07", 1.0), ("2026-08", 0.9)):
            for day in range(1, 19):
                date = f"{month}-{day:02d}"
                value = base * factor
                rows.append((date, month, ksss, value * 900, value * 250, value * 30,
                             value, value * 0.4, 625.0, "t", "test"))
    conn.executemany("INSERT INTO station_kpi_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    conn = sqlite3.connect(ref)
    conn.execute("""CREATE TABLE stations (
        ksss TEXT PRIMARY KEY, station_number TEXT, name TEXT, station_type TEXT, status TEXT,
        npo TEXT, region TEXT, city TEXT, address TEXT, format TEXT, location TEXT,
        service_cluster TEXT, shop TEXT, shop_area REAL, trk_count REAL, posts_count REAL,
        has_cafe INTEGER, has_shop INTEGER, is_active INTEGER, is_agency INTEGER,
        regional_manager TEXT, territory_manager TEXT, manager TEXT)""")
    conn.executemany(
        "INSERT INTO stations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("1001", "01001", "АЗС № 01001", "АЗС", "Действующая", "УНП", "г. Москва", "Москва", "", "А", "Город",
             "Магазин – кафе", "да", 120, 6, 8, 1, 1, 1, 0, "РУ Один", "ТМ Один", "Упр Один"),
            ("1002", "01002", "АЗС № 01002", "АЗС", "Действующая", "УНП", "г. Москва", "Москва", "", "А", "Трасса",
             "Магазин", "да", 80, 4, 6, 0, 1, 1, 0, "РУ Один", "ТМ Один", "Упр Два"),
            ("1003", "01003", "АЗС № 01003", "АЗС", "Действующая", "УНП", "Тверская обл.", "Тверь", "", "Б", "Трасса",
             "Магазин", "да", 60, 4, 4, 0, 1, 1, 0, "РУ Два", "ТМ Два", "Упр Три"),
        ],
    )
    conn.commit()
    conn.close()
    return kpi, ref


class StandCase(unittest.TestCase):
    """Общая подготовка: временный стенд подменяет базы исполнителя и журнала."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        folder = pathlib.Path(self._tmp.name)
        kpi, ref = _build_stand(folder)
        self._saved = (executor.KPI_DB, executor.REFERENCE_DB, executor.BACKEND,
                       journal.JOURNAL_DB, scope_builder.REFERENCE_DB, roles.REFERENCE_DB)
        executor.KPI_DB, executor.REFERENCE_DB, executor.BACKEND = kpi, ref, "sqlite"
        journal.JOURNAL_DB = folder / "journal.sqlite3"
        scope_builder.REFERENCE_DB = ref
        roles.REFERENCE_DB = ref
        self.scope = scope_builder.build("admin")

    def tearDown(self):
        (executor.KPI_DB, executor.REFERENCE_DB, executor.BACKEND,
         journal.JOURNAL_DB, scope_builder.REFERENCE_DB, roles.REFERENCE_DB) = self._saved
        self._tmp.cleanup()

    def ctx(self, depth: str = "deep") -> tools.ToolContext:
        return tools.ToolContext(question="тест", scope=self.scope, budget=Budget.for_depth(depth),
                                 catalog=self._stand_catalog(), semantic=semantic.STAND)

    @staticmethod
    def _stand_catalog():
        from backend.ai.catalog import BUILTIN_SCOPED, BUILTIN_TABLES, Catalog
        return Catalog(tables={k: set(v) for k, v in BUILTIN_TABLES.items()}, scoped_tables=set(BUILTIN_SCOPED),
                       facts_table="station_kpi_daily", date_column="metric_date")


class SemanticTests(unittest.TestCase):
    def test_metric_is_found_by_synonym_and_form(self):
        self.assertEqual(semantic.STAND.metric("трафик").key, "traffic")
        self.assertEqual(semantic.STAND.metric("чеков").key, "traffic")
        self.assertEqual(semantic.STAND.metric("конверсии").key, "conversion_ntu")
        self.assertEqual(semantic.DWH.metric("ВД на клиента").key, "vd_per_client")
        self.assertIsNone(semantic.STAND.metric("погода"))

    def test_absent_things_are_named_not_invented(self):
        found = semantic.STAND.find("Как изменились продажи кофе в августе относительно июля")
        self.assertTrue(any("кофе" in item for item in found["absent"]))
        found = semantic.DWH.find("средняя цена товара и наличие на полке")
        self.assertTrue(any("цена" in item for item in found["absent"]))
        self.assertTrue(any("наличие" in item for item in found["absent"]))

    def test_short_abbreviation_matches_only_whole_word(self):
        self.assertFalse(semantic.STAND.find("Сколько чеков было вчера")["absent"])
        self.assertTrue(semantic.STAND.find("какой ВД по сети")["absent"])

    def test_catalog_section_overrides_builtin_formula(self):
        from backend.ai.catalog import Catalog
        catalog = Catalog(tables={"data_for_ai_analytic_part_1": {"cnt_cheq"}}, dialect="postgres",
                          facts_table="data_for_ai_analytic_part_1",
                          semantic={"metrics": [{"key": "traffic", "title": "Трафик", "expr": "SUM(cnt_cheq_b2c)", "unit": "шт"}],
                                    "absent": ["тестовое отсутствие"]})
        merged = semantic.load(catalog)
        self.assertEqual(merged.metrics["traffic"].expr, "SUM(cnt_cheq_b2c)")
        self.assertEqual(merged.absent, ["тестовое отсутствие"])
        self.assertIn("vd_ntu", merged.metrics)


class ChecksTests(unittest.TestCase):
    def test_empty_and_all_null_are_reported_with_data_range(self):
        empty = checks.inspect(["x"], [], False, 100, ("2025-01-01", "2026-09-18"))
        self.assertIn("Результат пуст", empty[0])
        self.assertIn("2026-09-18", empty[0])
        nulls = checks.inspect(["x"], [[None]], False, 100, ("2025-01-01", "2026-09-18"))
        self.assertIn("Все значения пустые", nulls[0])

    def test_duplicates_truncation_and_partial_month(self):
        rows = [["2026-08", 1.0], ["2026-08", 1.0], ["2026-09", None]]
        notes = checks.inspect(["Месяц", "v"], rows, True, 3, ("2025-01-01", "2026-09-18"))
        text = " ".join(notes)
        self.assertIn("Повторяющихся строк: 1", text)
        self.assertIn("Обрезано по лимиту 3", text)
        self.assertIn("пустые (NULL)", text)
        self.assertIn("неполный", text)


class ToolTests(StandCase):
    def test_schema_tools_use_catalog_not_information_schema(self):
        ctx = self.ctx()
        schema = tools.call(ctx, "get_schema", {})
        self.assertEqual({t["table"] for t in schema["tables"]}, {"station_kpi_daily", "stations"})
        self.assertEqual(schema["data_range"]["min_date"], "2026-07-01")
        definition = tools.call(ctx, "get_metric_definition", {"metric": "трафик"})
        self.assertEqual(definition["expr"], "SUM(checks)")
        self.assertIn("active_stations", definition["drivers"])
        missing = tools.call(ctx, "get_metric_definition", {"metric": "цена кофе"})
        self.assertFalse(missing["found"])
        self.assertTrue(missing["absent"])

    def test_dimension_values_are_case_insensitive_for_cyrillic(self):
        values = tools.call(self.ctx(), "get_dimension_values", {"dimension": "регион", "search": "моск"})
        self.assertEqual([v["value"] for v in values["values"]], ["г. Москва"])
        time_dim = tools.call(self.ctx(), "get_dimension_values", {"dimension": "месяц"})
        self.assertEqual((time_dim["first"], time_dim["last"]), ("2026-07", "2026-08"))

    def test_run_sql_goes_through_validator_and_checks(self):
        ctx = self.ctx()
        ok = tools.call(ctx, "run_sql", {"sql": "SELECT period AS \"Месяц\", SUM(checks) AS \"Чеки\" FROM station_kpi_daily GROUP BY period ORDER BY period",
                                          "purpose": "чеки по месяцам"})
        self.assertEqual(ok["id"], "r1")
        self.assertEqual(ok["rows"][0][0], "2026-07")
        self.assertTrue(any("Период в результате" in w for w in ok["warnings"]))
        bad = tools.call(ctx, "run_sql", {"sql": "DELETE FROM stations", "purpose": "нельзя"})
        self.assertEqual(bad["rule"], "not_select")
        foreign = tools.call(ctx, "run_sql", {"sql": "SELECT * FROM auth_users", "purpose": "нельзя"})
        self.assertEqual(foreign["rule"], "unknown_table")
        self.assertEqual([s.ok for s in ctx.workspace.steps], [True, False, False])

    def test_scope_is_applied_to_agent_queries(self):
        narrow = scope_builder.build("territory_manager", "ТМ Два")
        ctx = tools.ToolContext(question="тест", scope=narrow, budget=Budget.for_depth("analyze"),
                                catalog=self._stand_catalog(), semantic=semantic.STAND)
        result = tools.call(ctx, "run_sql", {"sql": "SELECT COUNT(DISTINCT ksss) AS n FROM station_kpi_daily", "purpose": "объекты"})
        self.assertEqual(result["rows"][0][0], 1)

    def test_budget_limits_sql_calls(self):
        ctx = self.ctx("analyze")
        ctx.budget.sql_calls = 1
        tools.call(ctx, "run_sql", {"sql": "SELECT 1 AS a FROM stations", "purpose": "первый"})
        second = tools.call(ctx, "run_sql", {"sql": "SELECT 1 AS a FROM stations", "purpose": "второй"})
        self.assertIn("лимит запросов", second["error"])

    def test_argument_validation_reports_missing_and_enum(self):
        ctx = self.ctx()
        self.assertIn("purpose", tools.call(ctx, "run_sql", {"sql": "SELECT 1 FROM stations"})["error"])
        self.assertIn("инструмента", tools.call(ctx, "no_such_tool", {})["error"])


class SandboxTests(StandCase):
    def _r1(self):
        return ResultSet(id="r1", columns=["Месяц", "Чеки"], rows=[["2026-07", 100.0], ["2026-08", 90.0], ["2026-09", 95.0]], source="sql")

    def test_python_returns_result_and_helpers_work(self):
        ctx = self.ctx()
        ctx.workspace.add(self._r1())
        out = tools.call(ctx, "run_python", {"code": "t = trend(r1, 'Месяц', 'Чеки')\nprint(t['first']['y'], t['last']['y'])\nresult = pct_change(95, 100)",
                                             "inputs": ["r1"], "purpose": "тренд"})
        self.assertIsNone(out.get("error"), out)
        self.assertIn("100.0 95.0", out["stdout"])
        self.assertEqual(out["result"]["id"], "p1")
        self.assertEqual(out["result"]["object"]["pct"], -5.0)

    def test_python_cannot_import_os_or_open_files(self):
        ctx = self.ctx()
        ctx.workspace.add(self._r1())
        blocked = tools.call(ctx, "run_python", {"code": "import os\nprint(os.listdir('/'))", "purpose": "импорт"})
        self.assertIn("запрещён", blocked["error"])
        opened = tools.call(ctx, "run_python", {"code": "open('/etc/passwd').read()", "purpose": "файл"})
        self.assertIn("NameError", opened["error"])
        self.assertFalse(ctx.workspace.steps[-1].ok)

    def test_python_timeout_is_enforced(self):
        outcome = sandbox.execute("while True:\n    pass", {}, timeout_s=2)
        self.assertIn("по времени", outcome["error"])

    def test_environment_secrets_do_not_reach_the_sandbox(self):
        os.environ["DWH_DB_PASSWORD_TEST_MARKER"] = "secret"
        try:
            ctx = self.ctx()
            out = tools.call(ctx, "run_python", {"code": "import os", "purpose": "окружение"})
            self.assertIn("запрещён", out["error"])
        finally:
            os.environ.pop("DWH_DB_PASSWORD_TEST_MARKER", None)


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.rs = ResultSet(id="r1", columns=["Месяц", "Чеки", "Регион"],
                            rows=[["2026-07", 100, "A"], ["2026-08", 90, "A"], ["2026-07", 50, "B"], ["2026-08", 60, "B"]],
                            source="sql", purpose="чеки")

    def test_values_come_from_the_result_not_from_the_model(self):
        chart = charts.build(self.rs, {"type": "line", "x": "Месяц", "series": ["Чеки"], "group": "Регион"}, "c1")
        self.assertEqual(chart["x"], ["2026-07", "2026-08"])
        self.assertEqual([s["name"] for s in chart["series"]], ["A", "B"])
        self.assertEqual(chart["series"][1]["values"], [50.0, 60.0])

    def test_kpi_waterfall_scatter_and_errors(self):
        kpi = charts.build(ResultSet(id="r2", columns=["Выручка", "Чеки"], rows=[[1000, 20]], source="sql"), {"type": "kpi"}, "c2")
        self.assertEqual([c["label"] for c in kpi["cards"]], ["Выручка", "Чеки"])
        wf = charts.build(ResultSet(id="r3", columns=["Драйвер", "Вклад"], rows=[["АЗС", -10], ["на АЗС", -30]], source="python"),
                          {"type": "waterfall", "x": "Драйвер", "series": ["Вклад"], "start": 100}, "c3")
        self.assertEqual(wf["total"], 60)
        sc = charts.build(ResultSet(id="r4", columns=["Трафик", "Конверсия", "АЗС"], rows=[[100, 30, "a"], [200, 25, "b"]], source="sql"),
                          {"type": "scatter", "x": "Трафик", "series": ["Конверсия"], "label": "АЗС"}, "c4")
        self.assertEqual(sc["points"][1], {"x": 200.0, "y": 25.0, "label": "b"})
        with self.assertRaises(tools.ToolError):
            charts.build(self.rs, {"type": "pie"}, "c5")
        with self.assertRaises(tools.ToolError):
            charts.build(self.rs, {"type": "bar", "x": "Месяц", "series": ["Нет такой"]}, "c6")


class GroundingTests(unittest.TestCase):
    def test_numbers_are_matched_including_derived_and_sign(self):
        ws = Workspace()
        ws.add(ResultSet(id="r1", columns=["Месяц", "Чеки"], rows=[["2026-07", 1000.0], ["2026-08", 900.0]], source="sql"))
        analysis = Analysis(headline="Чеки снизились на 10 % — с 1 000 до 900 в августе 2026.",
                            happened=["Разница составила 100 чеков.", "Выручка выросла до 12 345 руб."])
        report = grounding.check(analysis, ws)
        self.assertEqual(report["unverified"], ["12 345"])
        cleaned, removed = grounding.strip_unverified(analysis, ws)
        self.assertEqual(cleaned.happened, ["Разница составила 100 чеков."])
        self.assertEqual(len(removed), 1)
        self.assertTrue(any("не подтвердились" in item for item in cleaned.limitations))

    def test_json_extraction_tolerates_wrappers(self):
        self.assertEqual(llm.extract_json("Вот ответ:\n```json\n{\"a\": 1,}\n```"), {"a": 1})
        calls = llm.calls_from_text('{"tool": "run_sql", "arguments": {"sql": "SELECT 1", "purpose": "x"}}')
        self.assertEqual(calls[0].name, "run_sql")


class LoopTests(StandCase):
    def _plan(self, **kw) -> Plan:
        base = dict(standalone_question="Как изменились чеки в августе 2026 относительно июля 2026", task_type="compare",
                    depth="analyze", steps=["чеки по месяцам", "изменение"])
        base.update(kw)
        return Plan(**base)

    def test_triage_heuristic_sends_plain_lookup_to_fast_without_model(self):
        model = llm.ScriptedModel([])
        plan = loop.triage("Сколько чеков было вчера?", history=None, semantic=semantic.STAND, model=model,
                           today="2026-09-22", data_range=None, hits=None)
        self.assertEqual((plan.depth, plan.source), ("fast", "heuristic"))
        self.assertEqual(model.calls, [])

    def test_triage_uses_model_for_followups_and_respects_forced_depth(self):
        history = [{"question": "Покажи продажи за год", "answer": {"sql": "SELECT 1"}, "frame": {"metrics": ["revenue"], "period": "год"}}]
        model = llm.ScriptedModel([{"standalone_question": "Продажи за год только по Москве", "task_type": "compare", "depth": "analyze",
                                    "metrics": ["revenue"], "filters": ["Москва"], "steps": ["a"]}])
        plan = loop.triage("А только Москва?", history=history, semantic=semantic.STAND, model=model,
                           today="2026-09-22", data_range=None, hits=None, depth="deep")
        self.assertEqual(plan.standalone_question, "Продажи за год только по Москве")
        self.assertEqual(plan.depth, "deep")
        self.assertIn("Покажи продажи за год", model.transcript[0][1]["content"])

    def test_full_run_with_tools_finish_and_grounding(self):
        script = [
            {"tool": "get_metric_definition", "arguments": {"metric": "трафик"}},
            {"tool": "run_sql", "arguments": {"sql": "SELECT period AS \"Месяц\", ROUND(SUM(checks)) AS \"Чеки, шт\" FROM station_kpi_daily GROUP BY period ORDER BY period",
                                              "purpose": "чеки по месяцам"}},
            {"tool": "run_python", "arguments": {"code": "c = pct_change(r1.iloc[1]['Чеки, шт'], r1.iloc[0]['Чеки, шт'])\nprint(c['pct'])\nresult = r1",
                                                 "inputs": ["r1"], "purpose": "изменение"}},
            {"tool": "create_chart", "arguments": {"source": "r1", "type": "bar", "title": "Чеки", "x": "Месяц", "series": ["Чеки, шт"]}},
            {"tool": "finish", "arguments": {"headline": "Чеки снизились на 10 % — с 11 160 до 10 044.",
                                             "happened": ["Всего 999 999 чеков."], "why": [], "where": [], "actions": [],
                                             "limitations": ["в стенде нет цен"], "main_result": "r1"}},
            # исправление после сверки чисел
            {"headline": "Чеки снизились на 10 % — с 11 160 до 10 044.", "happened": ["Снижение — 1 116 чеков."],
             "limitations": ["в стенде нет цен"], "main_result": "r1"},
        ]
        model = llm.ScriptedModel(script)
        events = []
        outcome = loop.run("Как изменились чеки?", self.scope, plan=self._plan(), model=model,
                           on_stage=events.append, catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.analysis.headline, "Чеки снизились на 10 % — с 11 160 до 10 044.")
        self.assertEqual(outcome.analysis.happened, ["Снижение — 1 116 чеков."])
        self.assertEqual(outcome.grounding["unverified"], [])
        self.assertEqual([s.kind for s in outcome.workspace.steps], ["schema", "sql", "python", "chart", "write"])
        self.assertEqual(outcome.workspace.charts[0]["series"][0]["values"], [11160.0, 10044.0])
        self.assertEqual(outcome.frame["mainResult"], "r1")
        self.assertIn(("write", "done"), [(e["key"], e["state"]) for e in events])
        self.assertTrue(all(m["role"] != "tool" or "rows" in m["content"] or "error" in m["content"] or "found" in m["content"]
                            for m in model.transcript[-1]))

    def test_prose_answer_is_finalized_from_evidence_without_penalty(self):
        script = [
            {"tool": "run_sql", "arguments": {"sql": "SELECT ROUND(SUM(checks)) AS \"Чеки, шт\" FROM station_kpi_daily WHERE period = '2026-08'", "purpose": "чеки за август"}},
            "Чеков в августе 10 044.",
            "Итог выше.",
            {"headline": "В августе 2026 — 10 044 чека.", "happened": [], "main_result": "r1"},
        ]
        outcome = loop.run("Чеки за август", self.scope, plan=self._plan(depth="analyze"), model=llm.ScriptedModel(script),
                           catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.analysis.headline, "В августе 2026 — 10 044 чека.")
        self.assertFalse(any("остановлен" in item for item in outcome.analysis.limitations))

    def test_budget_exhaustion_is_reported_honestly(self):
        script = [{"tool": "get_data_range", "arguments": {}}] * 30 + [{"headline": "Данных не собрано.", "happened": []}]
        os.environ["AI_AGENT_TURNS"] = "3"
        try:
            outcome = loop.run("Что-то", self.scope, plan=self._plan(), model=llm.ScriptedModel(script),
                               catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        finally:
            os.environ.pop("AI_AGENT_TURNS", None)
        self.assertTrue(any("лимит ходов" in item for item in outcome.analysis.limitations))

    def test_missing_metrics_from_plan_end_up_in_limitations(self):
        script = [
            {"tool": "run_sql", "arguments": {"sql": "SELECT ROUND(SUM(revenue_ntu)) AS \"Выручка НТУ\" FROM station_kpi_daily WHERE period = '2026-08'", "purpose": "нту"}},
            {"tool": "finish", "arguments": {"headline": "Выручка НТУ за август — 2 511 000 руб.", "happened": []}},
        ]
        plan = self._plan(task_type="compare", missing=["кофе / SKU: разбивки по товарам нет"])
        outcome = loop.run("Продажи кофе", self.scope, plan=plan, model=llm.ScriptedModel(script),
                           catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        self.assertTrue(any("кофе" in item for item in outcome.analysis.limitations))

    def test_model_unavailable_becomes_a_rule(self):
        class Down:
            model = "down"

            def chat(self, *a, **kw):
                raise llm.ModelUnavailable("Ollama не отвечает")

        outcome = loop.run("Вопрос", self.scope, plan=self._plan(), model=Down(),
                           catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.rule, "model_unavailable")


class MemoryTests(unittest.TestCase):
    def test_followup_and_lookup_heuristics(self):
        self.assertTrue(memory.is_followup("А только Москва?"))
        self.assertTrue(memory.is_followup("почему?"))
        self.assertFalse(memory.is_followup("Сколько чеков было на АЗС 1059 вчера?"))
        self.assertTrue(memory.looks_like_lookup("Сколько чеков было вчера?"))
        self.assertFalse(memory.looks_like_lookup("Почему снизился трафик?"))
        self.assertFalse(memory.looks_like_lookup("Какие 10 АЗС сильнее всего снизили трафик?"))

    def test_history_block_uses_frames(self):
        history = [{"question": "Продажи кофе за год", "answer": {"summary": "Итог", "sql": "SELECT 1"},
                    "frame": {"standalone": "Продажи кофе за последние 12 месяцев", "metrics": ["revenue_ntu"], "period": "12 месяцев", "headline": "Итог"}}]
        block = memory.history_block(history)
        self.assertIn("Продажи кофе за последние 12 месяцев", block)
        self.assertIn("revenue_ntu", block)
        frame = memory.frame_from_answer("Вопрос", {"sql": "SELECT 1", "summary": "Ответ", "columns": ["a"]})
        self.assertEqual(frame["headline"], "Ответ")


class PipelineFastPathTests(StandCase):
    def test_fast_path_reports_no_data_instead_of_a_number(self):
        from backend.ai import generator

        saved = (generator.generate, generator.narrate)
        generator.generate = lambda q, f=None, m=None: generator.Generated(
            sql="SELECT ROUND(SUM(checks)) AS \"Чеки за вчера\" FROM station_kpi_daily WHERE metric_date = '2031-01-01'",
            raw="", model="fake", elapsed_ms=1)
        generator.narrate = lambda *a, **k: ("не должно вызываться", 1)
        try:
            answer = pipeline.ask("Сколько чеков было вчера?", "admin", None, "test", depth="auto")
        finally:
            generator.generate, generator.narrate = saved
        self.assertTrue(answer.ok)
        self.assertEqual(answer.depth, "fast")
        self.assertIn("данных в витрине нет", answer.summary)
        self.assertIn("2026-08-18", answer.summary)
        self.assertEqual(answer.frame["standalone"], "Сколько чеков было вчера?")

    def test_agent_path_is_used_for_analysis_and_logged(self):
        script = [
            {"standalone_question": "Как изменились чеки в августе 2026 относительно июля", "task_type": "compare", "depth": "analyze", "steps": ["a"]},
            {"tool": "run_sql", "arguments": {"sql": "SELECT period AS \"Месяц\", ROUND(SUM(checks)) AS \"Чеки\" FROM station_kpi_daily GROUP BY period ORDER BY period", "purpose": "чеки"}},
            {"tool": "finish", "arguments": {"headline": "Чеки: 11 160 в июле и 10 044 в августе.", "happened": [], "main_result": "r1"}},
        ]
        model = llm.ScriptedModel(script)
        pipeline.MODEL_FACTORY = lambda name: model
        try:
            answer = pipeline.ask("Как изменились чеки относительно прошлого месяца?", "admin", None, "test", depth="auto")
        finally:
            pipeline.MODEL_FACTORY = None
        self.assertTrue(answer.ok)
        self.assertEqual((answer.depth, answer.task_type), ("analyze", "compare"))
        self.assertEqual(answer.analysis["headline"], "Чеки: 11 160 в июле и 10 044 в августе.")
        self.assertEqual(answer.columns, ["Месяц", "Чеки"])
        self.assertTrue(answer.steps and answer.steps[0]["sql"])
        conn = sqlite3.connect(journal.JOURNAL_DB)
        row = conn.execute("SELECT depth, task_type, trace_json FROM ai_queries ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(row[0], "analyze")
        self.assertIn("steps", json.loads(row[2]))


if __name__ == "__main__":
    unittest.main()


class ApiAgentTests(unittest.TestCase):
    """Маршруты: глубина, история диалога автора, скрытие кода для ролей без права на SQL."""

    class _Answer:
        ok = True
        question = "Почему снизился трафик?"
        scope_label = "вся сеть"
        summary = "Итог"
        sql = "SELECT 1"
        sql_raw = "SELECT 1"
        columns = ["n"]
        rows = [[1]]
        notes = []
        truncated = False
        model = "scripted"
        model_ms = 10
        narrate_ms = 0
        sql_ms = 5
        attempts = 3
        error = None
        rule = None
        journal_id = None
        depth = "deep"
        task_type = "diagnose"
        analysis = {"headline": "Итог", "happened": [], "why": [], "where": [], "actions": [], "limitations": []}
        charts = [{"id": "c1", "type": "bar", "title": "t", "x": ["a"], "series": [{"name": "s", "values": [1]}]}]
        tables = []
        steps = [{"key": "s1", "kind": "sql", "label": "Прочитал витрину", "sql": "SELECT 1", "code": None, "output": None, "ok": True}]
        frame = {"standalone": "Почему снизился трафик в сентябре"}
        plan = {"taskType": "diagnose"}
        grounding = {"checked": 1, "unverified": []}
        plan_ms = 3

    class _User:
        def __init__(self, uid, role, is_admin=False):
            self.id = uid
            self.role = role
            self.isAdmin = is_admin
            self.email = f"u{uid}@example.com"
            self.name = "Тест"
            self.roleTitle = "Роль"
            self.roleBinding = ""
            self.scopeLabel = "вся сеть"
            self.aiDialog = True

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.ai import api as ai_api
        from backend.ai import dialogs as store

        self._tmp = tempfile.TemporaryDirectory()
        self._saved_db = (journal.JOURNAL_DB, store.JOURNAL_DB)
        self._saved_flag = os.environ.get("AI_DEMO_ENABLED")
        self._saved_ask = ai_api.pipeline.ask
        os.environ["AI_DEMO_ENABLED"] = "1"
        path = pathlib.Path(self._tmp.name) / "journal.db"
        journal.JOURNAL_DB = path
        store.JOURNAL_DB = path
        self.seen: list[dict] = []

        def fake_ask(question, role, binding, actor, model=None, on_stage=None, depth="auto", history=None):
            self.seen.append({"question": question, "depth": depth, "history": history or []})
            return ApiAgentTests._Answer()

        ai_api.pipeline.ask = fake_ask
        self.store = store
        self.current = self._User(7, "aup_network")
        app = FastAPI()
        app.include_router(ai_api.build_router(lambda: self.current, lambda: self.current))
        self.client = TestClient(app)

    def tearDown(self):
        from backend.ai import api as ai_api
        journal.JOURNAL_DB, self.store.JOURNAL_DB = self._saved_db
        ai_api.pipeline.ask = self._saved_ask
        if self._saved_flag is None:
            os.environ.pop("AI_DEMO_ENABLED", None)
        else:
            os.environ["AI_DEMO_ENABLED"] = self._saved_flag
        self._tmp.cleanup()

    def test_depth_is_validated_and_passed_through(self):
        bad = self.client.post("/api/ai/ask", json={"question": "Почему?", "depth": "turbo"})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/api/ai/ask", json={"question": "Почему?", "depth": "deep"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.seen[-1]["depth"], "deep")
        body = ok.json()
        self.assertEqual((body["depth"], body["taskType"]), ("deep", "diagnose"))
        self.assertEqual(body["analysis"]["headline"], "Итог")
        self.assertEqual(body["charts"][0]["type"], "bar")

    def test_code_is_stripped_for_roles_without_sql_right(self):
        body = self.client.post("/api/ai/ask", json={"question": "Почему?"}).json()
        self.assertIsNone(body["sql"])
        self.assertNotIn("sql", body["steps"][0])
        self.assertEqual(body["steps"][0]["label"], "Прочитал витрину")
        self.current = self._User(8, "admin", is_admin=True)
        body = self.client.post("/api/ai/ask", json={"question": "Почему?"}).json()
        self.assertEqual(body["steps"][0]["sql"], "SELECT 1")

    def test_history_comes_only_from_the_authors_own_dialog(self):
        first = self.client.post("/api/ai/ask", json={"question": "Покажи трафик за год"}).json()
        dialog_id = first["dialogId"]
        self.client.post("/api/ai/ask", json={"question": "А только Москва?", "dialogId": dialog_id})
        history = self.seen[-1]["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["frame"]["standalone"], "Почему снизился трафик в сентябре")
        stranger = self._User(9, "aup_network")
        self.current = stranger
        denied = self.client.post("/api/ai/ask", json={"question": "А только Москва?", "dialogId": dialog_id})
        self.assertEqual(denied.status_code, 404)

    def test_status_lists_depth_options(self):
        body = self.client.get("/api/ai/status").json()
        self.assertTrue(body["agentEnabled"])
        self.assertEqual([d["code"] for d in body["depths"]], ["auto", "fast", "analyze", "deep"])
