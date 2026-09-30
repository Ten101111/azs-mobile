"""ИИ-23: режим планирования — план до выполнения, правка человеком, выполнение по плану.

Разбор задачи даёт карточку из семи полей; выполнение — только после подтверждения;
правки проверяет код (пункт вне контура и зависимость «снизу» не принимаются);
шаги агента привязываются к пунктам, пропуски объясняются; всё — в журнале.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.ai import api as ai_api
from backend.ai import dialogs as store
from backend.ai import journal, pipeline, quotas
from backend.ai.agent import llm, planning
from backend.ai.agent.state import Plan
from backend.tests.test_ai_agent import StandCase

SQL = 'SELECT period AS "Месяц", ROUND(SUM(checks)) AS "Чеки" FROM station_kpi_daily GROUP BY period ORDER BY period'
TRIAGE = {
    "standalone_question": "Почему снизились чеки в августе 2026 к июлю", "task_type": "diagnose", "depth": "deep",
    "period": "июль и август 2026", "filters": ["вся сеть"], "steps": ["a", "b"],
    "goal": "Найти, из-за чего снизились чеки",
    "expected_result": "Цифры по месяцам, вклад драйверов и выводы",
    "subtasks": [{"title": "Чеки по месяцам из витрины", "kind": "sql"},
                 {"title": "Разложить изменение на драйверы", "kind": "python", "after": [1]},
                 {"title": "График вклада драйверов", "kind": "chart", "after": [2]},
                 {"title": "Сформулировать вывод", "kind": "text", "after": [2]}],
    "risks": ["Неполный последний месяц"], "success_criteria": ["Названы драйверы с цифрами"],
}


def plan_card():
    plan = Plan(standalone_question=TRIAGE["standalone_question"], task_type="diagnose", depth="deep",
                period=TRIAGE["period"], filters=["вся сеть"])
    return planning.draft(plan, {k: TRIAGE[k] for k in ("goal", "expected_result", "subtasks", "risks", "success_criteria")})


class PlanCardTests(unittest.TestCase):
    def test_draft_has_seven_fields_and_only_contour_actions(self):
        card = plan_card()
        self.assertEqual(card["status"], "draft")
        self.assertEqual(card["goal"], "Найти, из-за чего снизились чеки")
        self.assertTrue(card["expected"] and card["risks"] and card["success"])
        self.assertEqual([s["kind"] for s in card["subtasks"]], ["sql", "python", "chart", "text"])
        self.assertEqual(card["subtasks"][2]["after"], ["s2"])                   # зависимость — номер пункта
        plan = Plan(standalone_question="q", steps=["Посчитать долю НТУ", "Отправить итог письмом руководителю"])
        self.assertEqual([s["title"] for s in planning.draft(plan)["subtasks"]], ["Посчитать долю НТУ"])

    def test_user_edits_are_checked_and_recorded(self):
        card = plan_card()
        edited = {"goal": card["goal"], "expected": card["expected"], "subtasks": [
            {"id": "s1", "title": "Чеки по месяцам из витрины", "after": []},
            {"id": "s2", "title": "Разложить изменение на драйверы", "after": ["s1"]},
            {"id": "", "title": "Сравнить с тем же периодом прошлого года", "after": ["s1"]},
            {"id": "s4", "title": "Сформулировать вывод", "after": ["s2", "s3"]},        # s3 удалён — связь снимается
        ]}
        approved, changes = planning.validate(edited, card)
        self.assertEqual([s["id"] for s in approved["subtasks"]], ["s1", "s2", "u1", "s4"])
        self.assertEqual(approved["subtasks"][2]["origin"], "user")
        self.assertEqual(approved["subtasks"][2]["kind"], "python")
        self.assertEqual(approved["subtasks"][3]["after"], ["s2"])
        self.assertEqual({c["op"] for c in changes}, {"remove", "add"})
        self.assertEqual(approved["depth"], "deep")                                     # уровень правкой не меняется
        with self.assertRaisesRegex(planning.PlanInvalid, "вне возможностей"):
            planning.validate({"subtasks": [{"id": "s1", "title": "Удалить старые записи из витрины"}]}, card)
        with self.assertRaisesRegex(planning.PlanInvalid, "стоит ниже"):
            planning.validate({"subtasks": [{"id": "s2", "title": "Разложить изменение на драйверы", "after": ["s1"]},
                                            {"id": "s1", "title": "Чеки по месяцам из витрины"}]}, card)
        moved, changes = planning.validate({"subtasks": [{"id": "s1", "title": "Чеки по месяцам из витрины"},
                                                         {"id": "s4", "title": "Сформулировать вывод"},
                                                         {"id": "s2", "title": "Разложить изменение на драйверы"}]}, card)
        self.assertIn("move", {c["op"] for c in changes})

    def test_progress_explains_what_was_not_done(self):
        card, _ = planning.validate(plan_card(), plan_card())
        steps = [{"kind": "sql", "ok": True, "subtask": "s1", "label": "Прочитал витрину"},
                 {"kind": "python", "ok": False, "subtask": "s2", "label": "Расчёт"},
                 {"kind": "sql", "ok": True, "label": "Лишний запрос"}]
        done = planning.progress(card, steps, [{"subtask": "3", "reason": "нечего рисовать без расчёта"}],
                                 finished=True, stop_reason="finish")
        self.assertEqual([s["status"] for s in done["subtasks"]], ["done", "failed", "skipped", "done"])
        self.assertEqual(done["subtasks"][2]["note"], "нечего рисовать без расчёта")
        self.assertEqual(done["extra"], ["Лишний запрос"])
        self.assertEqual(done["done"], 2)


class PlanPipelineTests(StandCase):
    def _run(self, script, **kwargs):
        model = llm.ScriptedModel(script)
        pipeline.MODEL_FACTORY = lambda name: model
        try:
            return pipeline.ask("Почему упали чеки в августе?", "admin", None, "test", **kwargs), model
        finally:
            pipeline.MODEL_FACTORY = None

    def test_deep_question_stops_at_the_plan_and_runs_after_approval(self):
        draft, model = self._run([TRIAGE], depth="deep", plan_mode="on")
        self.assertTrue(draft.ok)
        self.assertEqual(draft.rule, "plan")
        self.assertEqual(draft.plan_card["status"], "draft")
        self.assertEqual(len(model.calls), 1)                       # только разбор: агент не запускался
        self.assertIn("goal", model.transcript[0][0]["content"])    # в разборе — поля карточки
        row = sqlite3.connect(journal.JOURNAL_DB).execute(
            "SELECT verdict, plan_json FROM ai_queries WHERE id = ?", (draft.journal_id,)).fetchone()
        self.assertEqual(row[0], "plan")
        self.assertEqual(json.loads(row[1])["proposed"]["goal"], draft.plan_card["goal"])

        original = draft.plan_card
        edited = {**original, "subtasks": [s for s in original["subtasks"] if s["kind"] != "chart"]}
        approved, _ = planning.validate(edited, original)
        script = [   # разбора нет: сразу шаги по пунктам
            {"tool": "run_sql", "arguments": {"sql": SQL, "purpose": "чеки по месяцам", "subtask": "1"}},
            {"tool": "finish", "arguments": {"headline": "Чеки: 11 160 в июле и 10 044 в августе.", "happened": [],
                                             "main_result": "r1",
                                             "deviations": [{"subtask": "2", "reason": "драйверов нет в витрине"}]}},
        ]
        answer, model = self._run(script, depth="deep", approved=approved, original=original)
        self.assertTrue(answer.ok, answer.error)
        self.assertEqual(answer.plan["source"], "approved")
        card = answer.plan_card
        self.assertEqual([s["status"] for s in card["subtasks"]], ["done", "skipped", "done"])
        self.assertEqual(card["subtasks"][1]["note"], "драйверов нет в витрине")
        self.assertEqual(answer.steps[0]["subtask"], "s1")
        prompt = model.transcript[0][1]["content"]
        self.assertIn("Утверждённый пользователем план", prompt)
        from backend.ai.agent import loop, prompts, tools as agent_tools

        specs = loop._plan_specs([spec.as_ollama() for spec in agent_tools.specs()] + [prompts.FINISH_TOOL])
        props = {t["function"]["name"]: t["function"]["parameters"]["properties"] for t in specs}
        self.assertIn("subtask", props["run_sql"])
        self.assertIn("deviations", props["finish"])
        self.assertNotIn("deviations", prompts.FINISH_TOOL["function"]["parameters"]["properties"])
        logged = json.loads(sqlite3.connect(journal.JOURNAL_DB).execute(
            "SELECT plan_json FROM ai_queries WHERE id = ?", (answer.journal_id,)).fetchone()[0])
        self.assertEqual(logged["changes"], [{"op": "remove", "title": "График вклада драйверов"}])
        self.assertEqual((logged["done"], logged["total"]), (2, 3))

    def test_plan_mode_leaves_simple_questions_alone(self):
        script = [
            {**TRIAGE, "depth": "analyze", "task_type": "compare"},
            {"tool": "run_sql", "arguments": {"sql": SQL, "purpose": "чеки"}},
            {"tool": "finish", "arguments": {"headline": "Чеки: 11 160 в июле и 10 044 в августе.", "happened": [], "main_result": "r1"}},
        ]
        answer, _ = self._run(script, depth="auto", plan_mode="on")
        self.assertIsNone(answer.plan_card)                          # «Авто» — без плана: он только у «Среднего» и «Высокого»
        self.assertTrue(answer.ok)
        draft, _ = self._run([{**TRIAGE, "depth": "analyze"}], depth="analyze", plan_mode="on")
        self.assertEqual((draft.rule, draft.plan_card["depth"]), ("plan", "analyze"))   # «Средний» по кнопке — с планом


class PlanApiTests(StandCase):
    def setUp(self):
        super().setUp()
        import os

        self._flag = os.environ.get("AI_DEMO_ENABLED")
        os.environ["AI_DEMO_ENABLED"] = "1"
        self._store_db = store.JOURNAL_DB
        store.JOURNAL_DB = journal.JOURNAL_DB
        quotas.RUNS.reset()
        quotas.set_overrides({})
        self.calls = []

        def fake_ask(question, role, binding, actor, model=None, on_stage=None, depth="auto", history=None,
                     control=None, files=None, memory=None, **kwargs):
            self.calls.append({"question": question, "depth": depth, "history": len(history or []), **kwargs})
            answer = pipeline.Answer(ok=True, question=question, scope_label="вся сеть", depth=depth, summary="Ответ.")
            if kwargs.get("approved"):
                answer.plan_card = planning.progress(kwargs["approved"], [], [], finished=True)
            elif kwargs.get("plan_mode") == "on" and depth == "deep":
                answer.rule = "plan"
                answer.plan_card = plan_card()
            return answer

        self._ask = ai_api.pipeline.ask
        ai_api.pipeline.ask = fake_ask

        class User:
            id, role, isAdmin, email, name = 7, "regional_manager", False, "u7@example.com", "Тест"
            roleTitle, roleBinding, scopeLabel, aiDialog = "РУ", "РУ Один", "РУ Один", True

        self.user = User()
        app = FastAPI()

        def require_admin():
            raise HTTPException(status_code=403, detail="нет")

        app.include_router(ai_api.build_router(require_admin, lambda: self.user))
        self.client = TestClient(app)

    def tearDown(self):
        import os

        ai_api.pipeline.ask = self._ask
        store.JOURNAL_DB = self._store_db
        if self._flag is None:
            os.environ.pop("AI_DEMO_ENABLED", None)
        else:
            os.environ["AI_DEMO_ENABLED"] = self._flag
        super().tearDown()

    def test_plan_is_stored_edited_run_once_and_can_be_cancelled(self):
        first = self.client.post("/api/ai/ask", json={"question": "Почему упали чеки?", "depth": "deep", "planMode": "on"}).json()
        self.assertEqual(first["planCard"]["status"], "draft")
        dialog, message = first["dialogId"], first["messageId"]
        card = first["planCard"]
        bad = {**card, "subtasks": card["subtasks"] + [{"title": "Отправить итог письмом"}]}
        refused = self.client.post("/api/ai/ask", json={"question": "x", "dialogId": dialog, "planFor": message, "plan": bad})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("вне возможностей", refused.json()["detail"])
        edited = {**card, "subtasks": card["subtasks"][:2]}
        done = self.client.post("/api/ai/ask", json={"question": "x", "dialogId": dialog, "planFor": message, "plan": edited})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["messageId"], message)                      # ответ — на месте карточки
        self.assertEqual(self.calls[-1]["question"], "Почему упали чеки?")      # вопрос — из карточки
        self.assertEqual(self.calls[-1]["depth"], "deep")
        self.assertEqual(len(self.calls[-1]["approved"]["subtasks"]), 2)
        messages = self.client.get(f"/api/ai/dialogs/{dialog}/messages").json()["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["answer"]["planCard"]["status"], "done")
        again = self.client.post("/api/ai/ask", json={"question": "x", "dialogId": dialog, "planFor": message})
        self.assertEqual(again.status_code, 409)

        second = self.client.post("/api/ai/ask", json={"question": "А по РУ?", "dialogId": dialog, "depth": "deep",
                                                       "planMode": "on"}).json()
        self.assertEqual(self.calls[-1]["history"], 1)
        # Невыполненный план в память диалога не идёт.
        self.client.post("/api/ai/ask", json={"question": "Ещё?", "dialogId": dialog})
        self.assertEqual(self.calls[-1]["history"], 1)
        cancelled = self.client.post(f"/api/ai/messages/{second['messageId']}/plan/cancel")
        self.assertEqual(cancelled.json()["planCard"]["status"], "cancelled")
        self.assertEqual(self.client.post(f"/api/ai/messages/{second['messageId']}/plan/cancel").status_code, 409)
        self.user.id = 8
        self.assertEqual(self.client.post("/api/ai/ask", json={"question": "x", "planFor": message}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
