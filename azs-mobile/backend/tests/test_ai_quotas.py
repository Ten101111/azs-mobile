"""Ролевые лимиты сложности ИИ (ИИ-03).

Проверяется: таблица Р-2 по ролям, два одновременных вопроса, квота «Высокого»
на день с возвратом при сбое, общая очередь тяжёлых задач с позицией и
отменой, длина вопроса, алерт решения №12, понижение «Высокого» до «Среднего»
в конвейере, остановка агента человеком, итоги ответа в журнале и отказы API.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ai import api as ai_api
from backend.ai import dialogs as store
from backend.ai import journal, limits, pipeline, quotas
from backend.ai.agent import llm, loop
from backend.ai.agent.state import Budget, Plan
from backend.tests.test_ai_agent import StandCase


class _User:
    def __init__(self, uid=7, role="regional_manager", is_admin=False):
        self.id = uid
        self.role = role
        self.isAdmin = is_admin
        self.email = f"user{uid}@example.com"
        self.name = "Тест"
        self.roleTitle = "Руководитель управления"
        self.roleBinding = "РУ Один"
        self.scopeLabel = "РУ Один"
        self.aiDialog = True


class QuotaCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (journal.JOURNAL_DB, store.JOURNAL_DB, quotas.clock)
        path = pathlib.Path(self._tmp.name) / "journal.db"
        journal.JOURNAL_DB = path
        store.JOURNAL_DB = path
        self.now = time.mktime((2026, 9, 24, 12, 0, 0, 0, 0, -1))
        quotas.clock = lambda: self.now
        quotas.RUNS.reset()
        quotas.set_overrides({})
        quotas.refresh()

    def tearDown(self):
        journal.JOURNAL_DB, store.JOURNAL_DB, quotas.clock = self._saved
        quotas.RUNS.reset()
        quotas.set_overrides({})
        quotas.refresh()
        self._tmp.cleanup()

    def use_deep(self, user, n):
        """n засчитанных «Высоких» сегодня."""
        for _ in range(n):
            run = quotas.RUNS.start(user, user.role, "deep")
            self.assertEqual(quotas.RUNS.enter_heavy(run), "granted")
            quotas.RUNS.finish(run, "ok", depth="deep")


class TableTests(QuotaCase):
    def test_values_follow_owner_table_r2(self):
        self.assertEqual(quotas.value("regional_manager", "deep_per_day"), 10)
        self.assertEqual(quotas.value("aup_npo", "deep_per_day"), 20)
        self.assertEqual(quotas.value("aup_network", "deep_per_day"), 30)
        self.assertIsNone(quotas.value("admin", "deep_per_day"))
        self.assertIsNone(quotas.value("subadmin", "deep_per_day"))
        self.assertEqual(quotas.value("admin", "question_chars"), 4000)
        self.assertEqual(quotas.value("aup_npo", "question_chars"), 2000)
        self.assertEqual(quotas.value("regional_manager", "rows_deep"), 1000)
        self.assertEqual(quotas.value("aup_npo", "rows_deep"), 2000)
        # Незнакомая роль — самые строгие значения.
        self.assertEqual(quotas.value("territory_manager", "deep_per_day"), 10)

    def test_run_limits_by_level_and_admin_without_limits(self):
        deep = limits.for_run("regional_manager", "deep")
        self.assertEqual((deep.max_seconds, deep.agent_rows), (240.0, 1000))
        analyze = limits.for_run("aup_npo", "analyze")
        self.assertEqual((analyze.max_seconds, analyze.agent_rows), (120.0, 1000))
        fast = limits.for_run("aup_npo", "fast")
        self.assertEqual((fast.max_seconds, fast.fast_rows), (60.0, 200))
        self.assertEqual(Budget.for_depth("deep", deep).row_limit, 1000)
        self.assertEqual(Budget.for_depth("deep", limits.for_run("aup_npo", "deep")).row_limit, 2000)
        admin = limits.for_run("admin", "analyze")
        self.assertTrue(admin.unlimited)
        self.assertEqual(admin.max_seconds, 0)

    def test_overrides_replace_defaults(self):
        quotas.set_overrides({("ru", "deep_per_day"): 15})
        self.assertEqual(quotas.value("regional_manager", "deep_per_day"), 15)
        self.assertEqual(quotas.value("aup_npo", "deep_per_day"), 20)


class AdmissionTests(QuotaCase):
    def test_two_concurrent_questions_third_is_refused(self):
        user = _User()
        first = quotas.RUNS.start(user, user.role, "auto")
        quotas.RUNS.start(user, user.role, "auto")
        with self.assertRaises(quotas.Refused) as caught:
            quotas.RUNS.start(user, user.role, "auto")
        self.assertEqual(caught.exception.reason, "concurrent")
        self.assertIn("два ваших вопроса", caught.exception.message)
        # Другой человек не задет.
        quotas.RUNS.start(_User(uid=8), "regional_manager", "auto")
        quotas.RUNS.finish(first, "ok")
        quotas.RUNS.start(user, user.role, "auto")

    def test_deep_quota_refuses_forced_deep_and_suggests_medium(self):
        user = _User()
        self.use_deep(user, 10)
        with self.assertRaises(quotas.Refused) as caught:
            quotas.RUNS.start(user, user.role, "deep")
        self.assertEqual((caught.exception.reason, caught.exception.suggest), ("deep_quota", "analyze"))
        self.assertIn("10 из 10", caught.exception.message)
        # «Авто» принимается, но «Высокий» внутри него заменяется «Средним».
        run = quotas.RUNS.start(user, user.role, "auto")
        depth, note = quotas.Control(run).enter_deep()
        self.assertEqual(depth, "analyze")
        self.assertIn("израсходован", note)

    def test_quota_resets_next_day_and_admin_has_none(self):
        user = _User()
        self.use_deep(user, 10)
        self.now += 86400
        quotas.RUNS.start(user, user.role, "deep")
        admin = _User(uid=1, role="admin", is_admin=True)
        self.use_deep(admin, 12)
        self.assertIsNone(quotas.usage(admin, "admin")["deep"])

    def test_failure_of_model_returns_the_quota(self):
        user = _User()
        run = quotas.RUNS.start(user, user.role, "deep")
        self.assertEqual(quotas.RUNS.enter_heavy(run), "granted")
        quotas.RUNS.finish(run, "failed", depth="deep", refund=True)
        self.assertEqual(quotas.usage(user, user.role)["deep"], {"limit": 10, "used": 0, "left": 10})

    def test_question_length_by_role(self):
        user = _User()
        with self.assertRaises(quotas.Refused) as caught:
            quotas.RUNS.check_question(user, user.role, "в" * 2001, "auto")
        self.assertEqual(caught.exception.reason, "question_chars")
        quotas.RUNS.check_question(user, "admin", "в" * 4000, "auto")

    def test_daily_alert_and_load_report(self):
        quotas.set_overrides({("general", "daily_alert"): 3})
        user = _User()
        for _ in range(5):
            quotas.RUNS.finish(quotas.RUNS.start(user, user.role, "auto", actor=user.email), "ok")
        report = quotas.load_report(7)
        self.assertEqual(len(report["alerts"]), 1)
        self.assertEqual((report["alerts"][0]["questions"], report["alerts"][0]["threshold"]), (5, 3))
        self.use_deep(user, 10)
        with self.assertRaises(quotas.Refused):
            quotas.RUNS.start(user, user.role, "deep")
        hits = {item["reason"]: item["count"] for item in quotas.load_report(7)["hits"]}
        self.assertEqual(hits["deep_quota"], 1)
        self.assertEqual(quotas.load_report(7)["heavyRuns"], 10)


class QueueTests(QuotaCase):
    def test_third_heavy_task_waits_with_position_and_admin_goes_first(self):
        users = [_User(uid=i) for i in (11, 12, 13)]
        runs = [quotas.RUNS.start(u, u.role, "deep") for u in users]
        self.assertEqual(quotas.RUNS.enter_heavy(runs[0]), "granted")
        self.assertEqual(quotas.RUNS.enter_heavy(runs[1]), "granted")
        positions: list[int] = []
        result: dict = {}
        waiter = threading.Thread(target=lambda: result.setdefault(
            "third", quotas.RUNS.enter_heavy(runs[2], positions.append)))
        waiter.start()
        time.sleep(0.3)
        self.assertEqual(positions, [1])
        # Администратор встаёт в начало очереди.
        admin = _User(uid=1, role="admin", is_admin=True)
        admin_run = quotas.RUNS.start(admin, "admin", "deep")
        admin_positions: list[int] = []
        admin_waiter = threading.Thread(target=lambda: result.setdefault(
            "admin", quotas.RUNS.enter_heavy(admin_run, admin_positions.append)))
        admin_waiter.start()
        time.sleep(0.3)
        self.assertEqual(admin_positions, [1])
        quotas.RUNS.finish(runs[0], "ok", depth="deep")
        admin_waiter.join(3)
        self.assertEqual(result.get("admin"), "granted")
        self.assertNotIn("third", result)
        quotas.RUNS.finish(runs[1], "ok", depth="deep")
        waiter.join(3)
        self.assertEqual(result.get("third"), "granted")

    def test_cancel_while_waiting_and_foreign_cancel_is_ignored(self):
        users = [_User(uid=i) for i in (21, 22, 23)]
        runs = [quotas.RUNS.start(u, u.role, "deep") for u in users]
        quotas.RUNS.enter_heavy(runs[0])
        quotas.RUNS.enter_heavy(runs[1])
        result: dict = {}
        waiter = threading.Thread(target=lambda: result.setdefault("r", quotas.RUNS.enter_heavy(runs[2])))
        waiter.start()
        time.sleep(0.2)
        self.assertFalse(quotas.RUNS.cancel(runs[2].id, users[0]))
        self.assertTrue(quotas.RUNS.cancel(runs[2].id, users[2]))
        waiter.join(3)
        self.assertEqual(result.get("r"), "cancelled")
        quotas.RUNS.finish(runs[2], "cancelled")
        self.assertEqual(quotas.usage(users[2], users[2].role)["deep"]["used"], 0)

    def test_queue_timeout_downgrades(self):
        quotas.set_overrides({("general", "heavy_slots"): 0, ("general", "queue_wait_s"): 0})
        user = _User()
        run = quotas.RUNS.start(user, user.role, "auto")
        depth, note = quotas.Control(run).enter_deep()
        self.assertEqual(depth, "analyze")
        self.assertIn("не засчитан", note)


class AgentStopTests(unittest.TestCase):
    def test_agent_stops_before_next_turn_when_cancelled(self):
        class _Scope:
            label = "вся сеть"
            role = "admin"

        model = llm.ScriptedModel([{"tool": "finish", "arguments": {"headline": "Итог.", "happened": []}}])
        plan = Plan(standalone_question="q", task_type="compare", depth="analyze")
        outcome = loop.run("q", _Scope(), plan=plan, model=model, on_stage=lambda e: None, data_range={},
                           hits={}, today="2026-09-24", scope_label="вся сеть", should_stop=lambda: True)
        self.assertEqual((outcome.rule, outcome.stop_reason), ("cancelled", "cancelled"))
        self.assertFalse(outcome.ok)


class _FakeControl:
    def __init__(self, depth="analyze", note="«Высокий» на сегодня израсходован (10 из 10)", stop=False):
        self.result = (depth, note)
        self.stop = stop
        self.left = False

    def cancelled(self):
        return self.stop

    def enter_deep(self, on_position=None):
        return self.result

    def leave_deep(self):
        self.left = True


class PipelineQuotaTests(StandCase):
    SCRIPT = [
        {"standalone_question": "Почему упали чеки в августе", "task_type": "diagnose", "depth": "deep", "steps": ["a"]},
        {"tool": "run_sql", "arguments": {"sql": "SELECT period AS \"Месяц\", ROUND(SUM(checks)) AS \"Чеки\" "
                                                 "FROM station_kpi_daily GROUP BY period ORDER BY period",
                                          "purpose": "чеки"}},
        {"tool": "finish", "arguments": {"headline": "Чеки: 11 160 в июле и 10 044 в августе.", "happened": [],
                                         "main_result": "r1"}},
    ]

    def _ask(self, control, role="regional_manager"):
        model = llm.ScriptedModel(list(self.SCRIPT))
        pipeline.MODEL_FACTORY = lambda name: model
        try:
            return pipeline.ask("Почему упали чеки?", "admin", None, "test", depth="deep", control=control)
        finally:
            pipeline.MODEL_FACTORY = None

    def test_deep_is_downgraded_with_a_note_and_stats_reach_the_journal(self):
        answer = self._ask(_FakeControl())
        self.assertTrue(answer.ok)
        self.assertEqual((answer.depth, answer.depth_requested), ("analyze", "deep"))
        self.assertIn("израсходован", answer.analysis["limitations"][0])
        conn = sqlite3.connect(journal.JOURNAL_DB)
        row = conn.execute("SELECT depth, depth_requested, tool_calls, stop_reason, total_ms, model_calls "
                           "FROM ai_queries ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(row[:4], ("analyze", "deep", 1, "finish"))
        self.assertIsNotNone(row[4])

    def test_granted_deep_releases_the_slot(self):
        control = _FakeControl(depth="deep", note="")
        answer = self._ask(control)
        self.assertEqual(answer.depth, "deep")
        self.assertTrue(control.left)

    def test_cancelled_in_queue_gives_no_answer(self):
        answer = self._ask(_FakeControl(depth="cancelled", note=""))
        self.assertEqual(answer.rule, "cancelled")
        self.assertFalse(answer.ok)


class _Answer:
    ok = True
    question = "q"
    scope_label = "РУ Один"
    summary = "Итог"
    sql = "SELECT 1"
    sql_raw = "SELECT 1"
    columns = ["n"]
    rows = [[1]]
    notes = []
    truncated = False
    model = "m"
    model_ms = 1
    narrate_ms = 0
    sql_ms = 1
    attempts = 1
    error = None
    rule = None
    journal_id = None
    depth = "deep"


class ApiQuotaTests(QuotaCase):
    def setUp(self):
        super().setUp()
        self._saved_flag = os.environ.get("AI_DEMO_ENABLED")
        self._saved_ask = ai_api.pipeline.ask
        os.environ["AI_DEMO_ENABLED"] = "1"
        self.seen = []

        def fake_ask(question, role, binding, actor, model=None, on_stage=None, depth="auto", history=None,
                     control=None, files=None, memory=None):
            self.seen.append(depth)
            if control is not None and depth == "deep":
                control.enter_deep()
                control.leave_deep()
            return _Answer()

        ai_api.pipeline.ask = fake_ask
        self.current = _User()
        app = FastAPI()
        app.include_router(ai_api.build_router(lambda: self.current, lambda: self.current))
        self.client = TestClient(app)

    def tearDown(self):
        ai_api.pipeline.ask = self._saved_ask
        if self._saved_flag is None:
            os.environ.pop("AI_DEMO_ENABLED", None)
        else:
            os.environ["AI_DEMO_ENABLED"] = self._saved_flag
        super().tearDown()

    def test_limits_counter_and_quota_refusal(self):
        body = self.client.get("/api/ai/limits").json()
        self.assertEqual(body["limits"]["deep"], {"limit": 10, "used": 0, "left": 10})
        self.assertEqual(body["limits"]["questionChars"], 2000)
        answer = self.client.post("/api/ai/ask", json={"question": "Почему?", "depth": "deep"}).json()
        self.assertEqual(answer["quota"]["deep"]["used"], 1)
        self.use_deep(self.current, 9)
        response = self.client.post("/api/ai/ask/stream", json={"question": "Почему?", "depth": "deep"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["X-AI-Suggest-Depth"], "analyze")
        self.assertIn("10 из 10", response.json()["detail"])
        deep = {d["code"]: d for d in self.client.get("/api/ai/limits").json()["depths"]}["deep"]
        self.assertTrue(deep["disabled"])
        self.assertIn("10 из 10", deep["reason"])

    def test_long_question_and_foreign_cancel(self):
        response = self.client.post("/api/ai/ask", json={"question": "в" * 2001})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["X-AI-Limit"], "question_chars")
        self.assertEqual(self.client.post("/api/ai/runs/999/cancel").status_code, 404)

    def test_stream_starts_with_run_id(self):
        response = self.client.post("/api/ai/ask/stream", json={"question": "Выручка"})
        first = response.text.split("\n\n")[0]
        self.assertIn("event: run", first)
        self.assertIn("runId", json.loads(first.split("data:")[1]))


if __name__ == "__main__":
    unittest.main()
