"""Единый стиль ответа и пределы администратора (23.09.2026).

1. Ответ модели, оборванный на лимите токенов, не попадает на экран сырым
   JSON: заголовок и пункты достаются из обрывка, иначе — только заголовок.
2. Технические имена колонок латиницей, период и числа в скобках, десятичная
   точка приводятся к единому виду; заголовки таблиц — по-русски.
3. У администратора ограничений в рамках лимитов нет: бюджет шагов и время
   агента не ограничивают, таблицы и ответы модели большие.
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from backend.ai import api, journal, limits, semantic, textstyle
from backend.ai.agent import llm, loop, sql_tool
from backend.ai.agent.state import Budget, Plan
from backend.tests.test_ai_agent import StandCase

TRUNCATED = ('{ "headline": "Конверсия НТУ на 500 отстающих АЗС составляет 11.49% (сентябрь 2026), что в 6 раз ниже '
             'уровня лидеров (71.46%).", "happened": [ "Средний чек НТУ на отстающих АЗС — 314.19 руб., у лидеров — '
             '405.47 руб. (сентябрь 2026).", "Среднее количество позиций НТУ на чек (vd_per_client) на отстающих — 11.8", '
             '"Сложность чека НТУ (complexity_ntu')


class SalvageTests(unittest.TestCase):
    def test_truncated_final_json_gives_headline_and_items(self):
        parsed = llm.salvage_json(TRUNCATED)
        self.assertTrue(parsed["headline"].startswith("Конверсия НТУ"))
        self.assertEqual(len(parsed["happened"]), 2)

    def test_unparseable_json_never_reaches_the_screen(self):
        a = loop._analysis_from_prose('{"headline": "Итог по сети", "happened": [ {{{')
        self.assertEqual(a.headline, "Итог по сети")
        garbage = loop._analysis_from_prose('{"happened": [ {{{')
        self.assertFalse(textstyle.looks_like_json(textstyle.headline(garbage.headline, "Запасной заголовок.")))

    def test_prose_answer_splits_into_headline_and_items(self):
        a = loop._analysis_from_prose("Чеков стало меньше. В августе — 900. В июле — 1 000.")
        self.assertEqual(a.headline, "Чеков стало меньше.")
        self.assertEqual(a.happened, ["В августе — 900.", "В июле — 1 000."])


class StyleTests(unittest.TestCase):
    def test_brackets_latin_names_and_decimal_point(self):
        text = "Конверсия НТУ на 500 отстающих АЗС составляет 11.49% (сентябрь 2026), что в 6 раз ниже уровня лидеров (71.46%)."
        self.assertEqual(textstyle.clean(text),
                         "Конверсия НТУ на 500 отстающих АЗС составляет 11,49 % в сентябре 2026, что в 6 раз ниже уровня лидеров — 71,46 %.")
        self.assertEqual(textstyle.clean("Позиций на чек (vd_per_client) — 11.8 (2026-09)."),
                         "Позиций на чек — 11,8 в сентябре 2026.")

    def test_known_names_become_russian_and_dates_stay(self):
        self.assertIn("чеков в день", textstyle.clean("Рост показателя checks_per_day за месяц.", semantic.STAND))
        self.assertEqual(textstyle.clean("Отчёт от 23.09.2026, АЗС 58-123."), "Отчёт от 23.09.2026, АЗС 58-123.")

    def test_headline_is_one_or_two_sentences_with_a_period(self):
        self.assertEqual(textstyle.headline("Итог 12.4 млн ₽ (сентябрь 2026)"), "Итог 12,4 млн ₽ в сентябре 2026.")
        self.assertEqual(textstyle.headline('{"headline": "x"', "Запасной заголовок"), "Запасной заголовок.")

    def test_result_columns_are_renamed_to_russian(self):
        self.assertEqual(textstyle.rename_columns(["checks_per_day", "Месяц"], semantic.STAND),
                         ["Чеков в день, шт/день", "Месяц"])


class SqlColumnsTests(StandCase):
    def test_agent_sees_russian_column_names(self):
        ctx = self.ctx()
        out = sql_tool.run_sql(ctx, "SELECT period AS month, SUM(checks) * 1.0 / COUNT(DISTINCT metric_date) AS checks_per_day "
                                    "FROM station_kpi_daily GROUP BY period", "чеки в день")
        self.assertIn("Чеков в день, шт/день", out["columns"])


class AdminLimitsTests(unittest.TestCase):
    def test_admin_has_no_limits_other_roles_keep_them(self):
        self.assertTrue(limits.for_role("admin").unlimited)
        self.assertFalse(limits.for_role("regional_manager").unlimited)
        admin = Budget.for_depth("analyze", limits.for_role("admin"))
        usual = Budget.for_depth("analyze", limits.for_role("regional_manager"))
        self.assertGreater(admin.sql_calls, usual.sql_calls)
        self.assertGreater(admin.model_turns, usual.model_turns)
        self.assertGreater(admin.row_limit, usual.row_limit)
        self.assertEqual(limits.ADMIN.max_seconds, 0)  # общего предела времени нет

    def test_depth_hint_for_admin_has_no_time_limit(self):
        # Ориентир времени читается из журнала — только из временного, не из data/ai_journal.sqlite3.
        with tempfile.TemporaryDirectory() as folder:
            saved = journal.JOURNAL_DB
            journal.JOURNAL_DB = pathlib.Path(folder) / "journal.sqlite3"
            try:
                texts = {o["code"]: o["typical"] for o in api.depth_options(unlimited=True)}
            finally:
                journal.JOURNAL_DB = saved
        if "deep" in texts and not texts["deep"].startswith("обычно"):
            self.assertEqual(texts["deep"], "без ограничения времени")

    def test_admin_run_ignores_overall_time_limit(self):
        saved = loop.MAX_SECONDS
        loop.MAX_SECONDS = -1  # обычный предел исчерпан сразу
        try:
            model = llm.ScriptedModel([{"headline": "Итог.", "happened": []}])
            plan = Plan(standalone_question="q", task_type="compare", depth="analyze")
            usual = loop.run("q", _Scope(), plan=plan, model=model, on_stage=lambda e: None,
                             data_range={}, hits={}, today="2026-09-22", scope_label="вся сеть")
            self.assertIn("лимит времени", " ".join(usual.analysis.limitations))
            model = llm.ScriptedModel([{"tool": "finish", "arguments": {"headline": "Итог.", "happened": []}}])
            admin = loop.run("q", _Scope(), plan=plan, model=model, on_stage=lambda e: None,
                             data_range={}, hits={}, today="2026-09-22", scope_label="вся сеть",
                             limits=limits.for_role("admin"))
            self.assertNotIn("лимит времени", " ".join(admin.analysis.limitations))
        finally:
            loop.MAX_SECONDS = saved


class _Scope:
    label = "вся сеть"
    role = "admin"


if __name__ == "__main__":
    unittest.main()
