"""Вкладка «Лимиты ИИ» (ИИ-26): правка лимитов администратором без разработчика.

Проверяется: доступ только администратору, диапазоны и пределы железа, действие
новой квоты на следующий вопрос без перезапуска (кэш 60 с), журнал «было → стало»,
откат к значениям по умолчанию, счётчики «упирались за 7 дней» и письмо-алерт
решения №12 выбранному получателю.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.ai import api as ai_api
from backend.ai import dialogs as store
from backend.ai import executor, journal, limit_settings, quotas


class _User:
    def __init__(self, uid=7, role="regional_manager", is_admin=False, email=None):
        self.id = uid
        self.role = role
        self.isAdmin = is_admin
        self.email = email or f"user{uid}@example.com"
        self.name = "Тест"
        self.roleTitle = "Руководитель управления"
        self.roleBinding = "РУ Один"
        self.scopeLabel = "РУ Один"
        self.aiDialog = True


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


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (journal.JOURNAL_DB, store.JOURNAL_DB, ai_api.pipeline.ask, quotas.alert_sink)
        self._flag = os.environ.get("AI_DEMO_ENABLED")
        os.environ["AI_DEMO_ENABLED"] = "1"
        path = pathlib.Path(self._tmp.name) / "journal.db"
        journal.JOURNAL_DB = path
        store.JOURNAL_DB = path
        quotas.RUNS.reset()
        quotas.set_overrides({})
        quotas.refresh()

        def fake_ask(question, role, binding, actor, model=None, on_stage=None, depth="auto", history=None,
                     control=None):
            if control is not None and depth == "deep":
                control.enter_deep()
                control.leave_deep()
            return _Answer()

        ai_api.pipeline.ask = fake_ask
        self.mail: list[tuple] = []
        self.admin = _User(uid=1, role="admin", is_admin=True, email="admin@example.com")
        self.current = _User()

        def require_admin():
            if not getattr(self.current, "isAdmin", False):
                raise HTTPException(status_code=403, detail="Только администратор")
            return self.current

        app = FastAPI()
        app.include_router(ai_api.build_router(require_admin, lambda: self.current,
                                               lambda subject, text, to="": self.mail.append((subject, text, to))))
        self.client = TestClient(app)

    def tearDown(self):
        journal.JOURNAL_DB, store.JOURNAL_DB, ai_api.pipeline.ask, quotas.alert_sink = self._saved
        if self._flag is None:
            os.environ.pop("AI_DEMO_ENABLED", None)
        else:
            os.environ["AI_DEMO_ENABLED"] = self._flag
        quotas.RUNS.reset()
        quotas.refresh()
        self._tmp.cleanup()

    def put(self, *changes):
        return self.client.put("/api/ai/admin/limits", json={"changes": [
            {"group": g, "param": p, "value": v} for g, p, v in changes]})


class AccessTests(SettingsCase):
    def test_only_admin_sees_and_changes_limits(self):
        self.assertEqual(self.client.get("/api/ai/admin/limits").status_code, 403)
        self.assertEqual(self.put(("ru", "deep_per_day", 15)).status_code, 403)
        self.assertEqual(self.client.post("/api/ai/admin/limits/reset").status_code, 403)
        self.current = self.admin
        body = self.client.get("/api/ai/admin/limits").json()
        self.assertEqual([g["code"] for g in body["groups"]], ["admin", "network", "npo", "ru"])
        deep = next(p for p in body["params"] if p["key"] == "deep_per_day")
        self.assertEqual(deep["values"], {"admin": None, "network": 30, "npo": 20, "ru": 10})
        self.assertTrue(deep["nullable"])
        self.assertEqual({g["key"] for g in body["general"]},
                         {"heavy_slots", "queue_wait_s", "daily_alert", "alert_recipient", "audit_days"})
        self.assertIn("history_days", {p["key"] for p in body["params"]})         # ИИ-02
        self.assertIn("storage", body)


class RangeTests(SettingsCase):
    def setUp(self):
        super().setUp()
        self.current = self.admin

    def test_out_of_range_is_not_saved_at_all(self):
        response = self.put(("ru", "deep_per_day", 15), ("ru", "concurrent", 9))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Одновременных вопросов", response.json()["detail"])
        self.assertEqual(limit_settings.load(), {})     # ни одна правка из пакета не сохранена

    def test_hardware_ceilings_cannot_be_exceeded(self):
        for change in (("npo", "seconds_deep", limit_settings.TIME_CEILING + 1),
                       ("npo", "rows_deep", executor.MAX_ROWS + 1),
                       ("general", "heavy_slots", limit_settings.HEAVY_CEILING + 1),
                       ("ru", "question_chars", quotas.MAX_QUESTION_CHARS + 1),
                       ("ru", "concurrent", True),
                       ("ru", "rows_fast", "12.5"),
                       ("general", "alert_recipient", "не почта"),
                       ("ru", "priority", "срочно"),
                       ("ru", "unknown", 1)):
            self.assertEqual(self.put(change).status_code, 400, change)
        self.assertEqual(limit_settings.load(), {})

    def test_empty_means_unlimited_only_where_allowed(self):
        self.assertEqual(self.put(("ru", "deep_per_day", None)).status_code, 200)
        self.assertIsNone(quotas.value("regional_manager", "deep_per_day"))
        self.assertEqual(self.put(("ru", "concurrent", "")).status_code, 400)


class ApplyTests(SettingsCase):
    def test_new_quota_changes_the_next_question_without_restart(self):
        self.current = self.admin
        self.assertEqual(self.put(("ru", "deep_per_day", 1)).status_code, 200)
        self.current = _User()
        first = self.client.post("/api/ai/ask", json={"question": "Почему?", "depth": "deep"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["quota"]["deep"], {"limit": 1, "used": 1, "left": 0})
        second = self.client.post("/api/ai/ask", json={"question": "Почему?", "depth": "deep"})
        self.assertEqual(second.status_code, 429)
        self.assertIn("1 из 1", second.json()["detail"])

    def test_other_process_change_is_read_after_cache_expires(self):
        self.assertEqual(quotas.value("aup_npo", "deep_per_day"), 20)
        conn = sqlite3.connect(journal.JOURNAL_DB)
        conn.executescript(limit_settings.DDL)
        conn.execute("INSERT INTO ai_limits (grp, param, value, changed_by, changed_at) VALUES ('npo', 'deep_per_day', '25', 'x', 0)")
        conn.commit()
        conn.close()
        self.assertEqual(quotas.value("aup_npo", "deep_per_day"), 20)      # кэш ещё свежий
        quotas._cache["at"] = time.monotonic() - quotas.CACHE_SECONDS - 1
        self.assertEqual(quotas.value("aup_npo", "deep_per_day"), 25)      # не позже чем через 60 с

    def test_time_label_follows_the_new_limit(self):
        self.current = self.admin
        self.put(("ru", "seconds_analyze", 180))
        options = {o["code"]: o for o in ai_api.depth_options(role="regional_manager")}
        self.assertEqual(options["analyze"]["typical"], "до 3 мин")


class HistoryTests(SettingsCase):
    def setUp(self):
        super().setUp()
        self.current = self.admin

    def test_history_and_reset_to_defaults(self):
        self.put(("ru", "deep_per_day", 15), ("general", "daily_alert", 80))
        self.put(("ru", "deep_per_day", 12))
        history = self.client.get("/api/ai/admin/limits").json()["history"]
        self.assertEqual((history[0]["old"], history[0]["new"], history[0]["group"]), ("15", "12", "РУ"))
        self.assertEqual(history[0]["by"], "admin@example.com")
        self.assertEqual(len(history), 3)
        body = self.client.post("/api/ai/admin/limits/reset").json()
        self.assertEqual(body["reset"], 2)
        self.assertEqual(limit_settings.load(), {})
        self.assertEqual(quotas.value("regional_manager", "deep_per_day"), 10)
        self.assertEqual(sum(1 for h in body["history"] if h["action"] == "reset"), 2)

    def test_default_value_is_not_kept_as_override(self):
        self.put(("ru", "deep_per_day", 15))
        self.put(("ru", "deep_per_day", 10))
        self.assertEqual(limit_settings.load(), {})


class HitsTests(SettingsCase):
    def test_hits_are_counted_per_role_group(self):
        user = _User()
        for _ in range(10):
            run = quotas.RUNS.start(user, user.role, "deep")
            quotas.RUNS.enter_heavy(run)
            quotas.RUNS.finish(run, "ok", depth="deep")
        with self.assertRaises(quotas.Refused):
            quotas.RUNS.start(user, user.role, "deep")
        entry = journal.write({"role": "aup_npo", "question": "q", "verdict": "ok", "depth": "analyze"})
        journal.set_run_stats(entry, stop_reason="лимит времени", truncated=1)
        found = limit_settings.hits(7)
        self.assertEqual(found[("ru", "deep_per_day")], 1)
        self.assertEqual(found[("npo", "seconds_analyze")], 1)
        self.assertEqual(found[("npo", "rows_analyze")], 1)
        self.current = self.admin
        deep = next(p for p in self.client.get("/api/ai/admin/limits").json()["params"] if p["key"] == "deep_per_day")
        self.assertEqual(deep["hits"]["ru"], 1)


class AlertTests(SettingsCase):
    def test_alert_goes_once_to_the_chosen_recipient(self):
        self.current = self.admin
        self.put(("general", "daily_alert", 10), ("general", "alert_recipient", "Boss@Example.com"))
        user = _User(uid=9)
        for _ in range(12):
            quotas.RUNS.finish(quotas.RUNS.start(user, user.role, "auto", actor=user.email), "ok")
        for _ in range(50):
            if self.mail:
                break
            time.sleep(0.02)
        time.sleep(0.1)
        self.assertEqual(len(self.mail), 1)
        subject, text, to = self.mail[0]
        self.assertEqual(to, "boss@example.com")
        self.assertIn("user9@example.com", subject)
        self.assertIn("11 вопросов", text)


if __name__ == "__main__":
    unittest.main()
