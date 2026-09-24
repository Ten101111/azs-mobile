"""ИИ-25 и ИИ-16: типы утверждений ответа и правила рекомендаций.

Тип пункта ставит код по происхождению чисел, а не модель: факт — ячейка
результата запроса, расчёт — разность, отношение или вывод Python с формулой,
гипотеза — «почему» и пункты без привязки к данным, рекомендация — по шаблону
из шести полей (Р-9) со стоп-листом и закрытыми темами.
"""
from __future__ import annotations

import unittest

from backend.ai import semantic
from backend.ai.agent import claims, grounding, llm, loop, recommend
from backend.ai.agent.state import RECOMMENDATION_NOTE, Analysis, Plan, ResultSet, Step, Workspace
from backend.tests.test_ai_agent import StandCase


def _workspace() -> Workspace:
    ws = Workspace()
    ws.add(ResultSet(id="r1", columns=["Месяц", "Чеки, шт", "Изменение, %"],
                     rows=[["2026-07", 1000.0, None], ["2026-08", 900.0, -10.0]],
                     source="sql", purpose="чеки по месяцам"))
    ws.add(ResultSet(id="r2", columns=["АЗС", "Конверсия НТУ, %"],
                     rows=[["АЗС № 01001", 31.5], ["АЗС № 01003", 18.2]],
                     source="sql", purpose="конверсия НТУ по АЗС"))
    ws.add(ResultSet(id="p1", columns=["Показатель", "Значение"], rows=[["медиана группы", 27.0]],
                     source="python", purpose="медиана группы"))
    ws.steps.append(Step(key="s3", kind="python", label="Посчитал", purpose="медиана группы",
                         result_id="p1", output="median 27.0", code="print(median(r2))"))
    return ws


def _rec(**kw) -> dict:
    base = {"action": "Можно рассмотреть разбор выкладки НТУ на АЗС № 01003.",
            "basis": "Конверсия НТУ на АЗС № 01003 — 18,2 % при медиане группы 27 %.",
            "effect": "Рост конверсии НТУ к медиане группы.",
            "limits": "Не подходит, если на объекте идёт ремонт магазина.",
            "source": "анализ данных", "confidence": "средняя"}
    base.update(kw)
    return base


class ClaimTypeTests(unittest.TestCase):
    def setUp(self):
        self.ws = _workspace()

    def _annotate(self, **kw) -> Analysis:
        analysis = Analysis(headline="Итог", **kw)
        self.summary = claims.annotate(analysis, self.ws)
        return analysis

    def test_cell_of_a_query_result_is_a_fact_with_source_and_column(self):
        a = self._annotate(happened=["В августе 2026 — 900 чеков."])
        claim = a.claims["happened"][0]
        self.assertEqual(claim["type"], claims.FACT)
        self.assertEqual(claim["sources"][0]["id"], "r1")
        self.assertEqual(claim["columns"], ["Чеки, шт"])

    def test_difference_and_rate_are_calculations_with_formula_and_inputs(self):
        a = self._annotate(happened=["Чеков стало меньше на 100.", "Снижение — 10 % к июлю."])
        diff, rate = a.claims["happened"]
        self.assertEqual(diff["type"], claims.CALC)
        self.assertIn("1 000", diff["formula"][0])
        self.assertIn("900", diff["formula"][0])
        self.assertEqual(rate["type"], claims.CALC)  # столбец «Изменение, %» считается в запросе

    def test_python_output_is_a_calculation(self):
        a = self._annotate(happened=["Медиана группы по конверсии НТУ — 27 %."])
        self.assertEqual(a.claims["happened"][0]["type"], claims.CALC)

    def test_why_items_are_hypotheses_with_what_to_check(self):
        a = self._annotate(why=["Возможно, снижение объясняется меньшим числом работающих АЗС.",
                                "Чеков стало меньше на 100."])
        a2 = Analysis(headline="x", why=["Возможно, дело в сезоне."], checks={"Возможно, дело в сезоне.": "те же месяцы прошлого года"})
        claims.annotate(a2, self.ws)
        first, second = a.claims["why"]
        self.assertEqual({first["type"], second["type"]}, {claims.HYPOTHESIS})
        self.assertEqual(first["check"], claims.CHECK_WHY)
        self.assertEqual(second["note"], claims.NOTE_CAUSE)
        self.assertEqual(a2.claims["why"][0]["check"], "те же месяцы прошлого года")

    def test_causal_wording_in_happened_is_a_hypothesis(self):
        a = self._annotate(happened=["Снижение на 100 чеков могло быть связано с ремонтом."])
        self.assertEqual(a.claims["happened"][0]["type"], claims.HYPOTHESIS)

    def test_fact_without_link_to_a_result_does_not_pass(self):
        a = self._annotate(happened=["Динамика неравномерная.", "Лучший результат — у АЗС № 01001."],
                           where=["Выручка 12 345 руб. у соседей."])
        free, linked = a.claims["happened"]
        self.assertEqual(free["type"], claims.HYPOTHESIS)
        self.assertEqual(free["check"], claims.CHECK_FREE)
        self.assertEqual(linked["type"], claims.FACT)
        self.assertEqual(linked["sources"][0]["id"], "r2")
        self.assertEqual(a.where, [])  # неподтверждённое число — пункт снят
        self.assertIn(grounding.UNVERIFIED_NOTE, a.limitations)

    def test_summary_counts_types_for_the_journal(self):
        self._annotate(happened=["В августе 2026 — 900 чеков.", "Чеков стало меньше на 100."],
                       why=["Возможно, сезон."], recs=[_rec()])
        self.assertEqual(self.summary["counts"], {"fact": 1, "calc": 1, "hypothesis": 1, "recommendation": 1})


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.ws = _workspace()

    def _review(self, *items) -> Analysis:
        analysis = Analysis(headline="Итог", recs=[recommend.normalize(i) for i in items])
        analysis.actions = [r["action"] for r in analysis.recs]
        self.summary = claims.annotate(analysis, self.ws)
        return analysis

    def test_complete_recommendation_gets_source_and_confidence_from_code(self):
        a = self._review(_rec(source="", confidence=""))
        rec = a.recommendations[0]
        self.assertTrue(rec["source"].startswith("Анализ витрины"))
        self.assertIn("r2", rec["source"])
        self.assertEqual(rec["confidence"], "средняя")
        self.assertEqual(a.as_dict()["recommendationNote"], RECOMMENDATION_NOTE)
        self.assertEqual(a.actions, [rec["action"]])

    def test_incomplete_recommendation_is_not_shown(self):
        a = self._review(_rec(limits=""), "Проверить выкладку.")
        self.assertEqual(a.recommendations, [])
        self.assertEqual(a.as_dict()["recommendationsWithheld"], 2)
        self.assertNotIn("recommendationNote", a.as_dict())

    def test_stop_list_softens_categorical_wording(self):
        a = self._review(_rec(action="Необходимо срочно разобрать выкладку НТУ на АЗС № 01003.",
                              effect="Обязательно даст рост конверсии."))
        rec = a.recommendations[0]
        self.assertEqual(rec["action"], "Стоит в ближайшее время разобрать выкладку НТУ на АЗС № 01003.")
        self.assertEqual(rec["effect"], "Даст рост конверсии.")
        self.assertTrue(rec["softened"])

    def test_staff_sanctions_and_promotions_are_withheld(self):
        a = self._review(_rec(action="Можно рассмотреть замену управляющего на АЗС № 01003."),
                         _rec(action="Можно рассмотреть лишение премии кассиров."),
                         _rec(action="Можно рассмотреть акцию на кофе на АЗС № 01003."),
                         _rec(effect="Скидка на хот-дог повысит конверсию."))
        self.assertEqual(a.recommendations, [])
        topics = [w.get("topic") for w in self.summary["withheld"]]
        self.assertEqual(topics, ["staff", "staff", "promo", "promo"])

    def test_basis_must_rest_on_answer_data_or_methodology(self):
        a = self._review(_rec(basis="Так обычно делают."),
                         _rec(basis="По методике конверсия ниже медианы группы — сигнал к разбору.", source="методика НТУ, п. 4"))
        self.assertEqual(len(a.recommendations), 1)
        self.assertEqual(a.recommendations[0]["source"], "Методика НТУ, п. 4")
        self.assertEqual(a.recommendations[0]["confidence"], "средняя")  # без данных — не выше средней

    def test_confidence_is_capped_by_hypothetical_basis(self):
        a = self._review(_rec(basis="Конверсия 18,2 % — возможно, из-за выкладки.", confidence="высокая"))
        self.assertEqual(a.recommendations[0]["confidence"], "низкая")

    def test_at_most_three_recommendations(self):
        a = self._review(*[_rec() for _ in range(5)])
        self.assertEqual(len(a.recommendations), 3)

    def test_unverified_number_in_effect_removes_recommendation(self):
        analysis = Analysis(headline="Итог", recs=[recommend.normalize(_rec(effect="Плюс 4 321 чек в месяц."))])
        analysis.actions = [analysis.recs[0]["action"]]
        cleaned, removed = grounding.strip_unverified(analysis, self.ws)
        self.assertEqual(cleaned.recs, [])
        self.assertEqual(cleaned.actions, [])
        self.assertEqual(len(removed), 1)


class UnsolicitedRecommendationTests(unittest.TestCase):
    """Решение владельца 23.09.2026: рекомендации — по просьбе или когда без них ответ неполон."""

    def test_request_detection(self):
        for q in ("Что сделать, чтобы закрыть план по НТУ?", "Дай рекомендации по 58-123", "Как поднять конверсию НТУ?",
                  "Какие меры предпринять?", "посоветуй, что делать с трафиком"):
            self.assertTrue(recommend.requested(q), q)
        for q in ("Сколько чеков было вчера?", "Почему снизился трафик?", "Покажи динамику выручки НТУ"):
            self.assertFalse(recommend.requested(q), q)

    def test_unsolicited_keeps_one_confident_recommendation(self):
        ws = _workspace()
        a = Analysis(headline="Итог", recs=[recommend.normalize(_rec(confidence="низкая")),
                                             recommend.normalize(_rec()), recommend.normalize(_rec())])
        summary = claims.annotate(a, ws, asked=False)
        self.assertEqual(len(a.recommendations), 1)
        self.assertEqual(a.recommendations[0]["confidence"], "средняя")
        self.assertFalse(summary["asked"])

    def test_insufficient_data_message_only_when_asked(self):
        ws = _workspace()
        silent = Analysis(headline="Итог", recs=[recommend.normalize(_rec(basis="Так обычно делают."))])
        claims.annotate(silent, ws, asked=False)
        self.assertNotIn("recommendationsWithheld", silent.as_dict())
        loud = Analysis(headline="Итог", recs=[recommend.normalize(_rec(basis="Так обычно делают."))])
        claims.annotate(loud, ws, asked=True)
        self.assertEqual(loud.as_dict()["recommendationsWithheld"], 1)

    def test_prompts_carry_the_rule(self):
        from backend.ai.agent import prompts
        self.assertIn(prompts.NOT_ASKED_NOTE, prompts.finalize_user("q", {}, "e", [], asked=False))
        self.assertIn(prompts.ASKED_NOTE, prompts.finalize_user("q", {}, "e", [], asked=True))


class ParsingTests(unittest.TestCase):
    def test_finish_payload_with_objects_and_strings(self):
        a = loop._analysis_from({
            "headline": "Итог", "happened": ["факт"],
            "why": [{"text": "Возможно, сезон.", "check": "прошлый год"}, "Просто строка."],
            "actions": [_rec(), "Строка без полей"],
        })
        self.assertEqual(a.why, ["Возможно, сезон.", "Просто строка."])
        self.assertEqual(a.checks, {"Возможно, сезон.": "прошлый год"})
        self.assertEqual(len(a.recs), 2)
        self.assertEqual(a.actions[1], "Строка без полей")
        view = a.model_view()
        self.assertEqual(view["why"][0], {"text": "Возможно, сезон.", "check": "прошлый год"})
        self.assertEqual(view["actions"][0]["basis"], _rec()["basis"])

    def test_old_answers_without_claims_keep_their_shape(self):
        a = Analysis(headline="x", happened=["y"], actions=["z"])
        self.assertEqual(set(a.as_dict()), {"headline", "happened", "why", "where", "actions", "limitations"})

    def test_prompt_mentions_template_and_forbidden_topics(self):
        from backend.ai.agent import prompts
        self.assertIn("Не предлагай акции и скидки", prompts.FINALIZE_SYSTEM)
        schema = prompts.FINISH_TOOL["function"]["parameters"]["properties"]
        # Источник и уверенность ставит код — модель пишет только четыре коротких поля.
        self.assertEqual(schema["actions"]["items"]["required"], ["action", "basis", "effect", "limits"])
        self.assertIn("check", schema["why"]["items"]["properties"])


class PublicGroundingTests(unittest.TestCase):
    def test_withheld_text_is_hidden_from_roles_without_sql(self):
        from backend.ai import api
        g = {"checked": 2, "claims": {"counts": {}, "withheld": [{"action": "Уволить кассира", "reason": "К13", "topic": "staff"}]}}
        self.assertEqual(api._public_grounding(g, False)["claims"]["withheld"], [{"reason": "К13"}])
        self.assertEqual(api._public_grounding(g, True), g)


class LoopClaimsTests(StandCase):
    def test_agent_run_returns_typed_claims_and_reviewed_recommendations(self):
        script = [
            {"tool": "run_sql", "arguments": {"sql": "SELECT period AS \"Месяц\", ROUND(SUM(checks)) AS \"Чеки, шт\" FROM station_kpi_daily GROUP BY period ORDER BY period",
                                              "purpose": "чеки по месяцам"}},
            {"tool": "finish", "arguments": {
                "headline": "Чеки снизились на 10 % — с 11 160 до 10 044.",
                "happened": ["В августе — 10 044 чеков.", "Снижение — 1 116 чеков."],
                "why": [{"text": "Возможно, сказался сезон.", "check": "те же месяцы прошлого года"}],
                "actions": [{"action": "Необходимо разобрать режим работы объектов со снижением.",
                             "basis": "Чеков в августе 10 044 против 11 160 в июле.",
                             "effect": "Станет видно, где снижение связано с работой объекта.",
                             "limits": "Не подходит объектам на ремонте.", "confidence": "высокая"},
                            {"action": "Можно рассмотреть акцию на кофе.", "basis": "Чеков в августе 10 044.",
                             "effect": "Рост чеков.", "limits": "Не подходит объектам без кафе.", "confidence": "низкая"}],
                "limitations": [], "main_result": "r1"}},
        ]
        outcome = loop.run("Как изменились чеки и что сделать?", self.scope,
                           plan=Plan(standalone_question="Как изменились чеки и что сделать", task_type="compare", depth="analyze"),
                           model=llm.ScriptedModel(script), on_stage=lambda e: None,
                           catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        a = outcome.analysis.as_dict()
        self.assertEqual([c["type"] for c in a["claims"]["happened"]], ["fact", "calc"])
        self.assertEqual(a["claims"]["why"][0]["check"], "те же месяцы прошлого года")
        self.assertEqual(len(a["recommendations"]), 1)
        self.assertTrue(a["recommendations"][0]["action"].startswith("Стоит разобрать"))
        self.assertEqual(a["actions"], [a["recommendations"][0]["action"]])
        self.assertEqual(outcome.grounding["claims"]["counts"]["recommendation"], 1)
        self.assertEqual(outcome.grounding["claims"]["withheld"][0]["topic"], "promo")


class FailedTurnTests(StandCase):
    def test_failed_model_turn_after_data_goes_to_final_step(self):
        """Ollama ответила 500 на ходе модели (обрезанный вызов инструмента): данные есть — итог по ним."""
        def broken_turn(messages):
            raise llm.ModelUnavailable("Модель ответила ошибкой 500")
        script = [
            {"tool": "run_sql", "arguments": {"sql": "SELECT period AS \"Месяц\", ROUND(SUM(checks)) AS \"Чеки, шт\" FROM station_kpi_daily GROUP BY period ORDER BY period",
                                              "purpose": "чеки по месяцам"}},
            broken_turn,
            {"headline": "Чеки снизились с 11 160 до 10 044.", "happened": ["В августе — 10 044 чеков."], "main_result": "r1"},
        ]
        outcome = loop.run("Как изменились чеки?", self.scope,
                           plan=Plan(standalone_question="Как изменились чеки", task_type="compare", depth="analyze"),
                           model=llm.ScriptedModel(script), on_stage=lambda e: None,
                           catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        self.assertTrue(outcome.ok)
        self.assertIsNone(outcome.rule)
        self.assertEqual(outcome.analysis.happened, ["В августе — 10 044 чеков."])
        self.assertTrue(any("сбой хода модели" in item for item in outcome.analysis.limitations))

    def test_failed_first_turn_without_data_is_still_an_error(self):
        def broken_turn(messages):
            raise llm.ModelUnavailable("Модель ответила ошибкой 500")
        outcome = loop.run("Как изменились чеки?", self.scope,
                           plan=Plan(standalone_question="Как изменились чеки", task_type="compare", depth="analyze"),
                           model=llm.ScriptedModel([broken_turn]), on_stage=lambda e: None,
                           catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        self.assertEqual(outcome.rule, "model_unavailable")


if __name__ == "__main__":
    unittest.main()
