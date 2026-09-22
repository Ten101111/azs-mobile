"""Раздел «Качество ответов»: сводка, лента, разбор, срез по версиям.

Считаются здесь не красивые числа, а те, по которым принимают решения:
доля отказов отделена от доли технических ошибок, дата первичной реакции
ставится один раз, а выгрузка не даёт больше, чем экран.
"""
import os
import pathlib
import tempfile
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ai import journal
from backend.ai import dialogs as store
from backend.ai import api as ai_api
from backend.ai import quality


class _Admin:
    id = 7
    role = "admin"
    isAdmin = True
    email = "admin@example.com"
    name = "Манохин А. А."
    roleTitle = "Администратор"
    roleBinding = ""
    scopeLabel = "вся сеть"
    aiDialog = True


class AiQualityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (journal.JOURNAL_DB, store.JOURNAL_DB)
        self._flag = os.environ.get("AI_DEMO_ENABLED")
        os.environ["AI_DEMO_ENABLED"] = "1"
        path = pathlib.Path(self._tmp.name) / "journal.db"
        journal.JOURNAL_DB = path
        store.JOURNAL_DB = path

        self.dialog = store.create_dialog(7)
        self.ids = {}
        for verdict, rule, rating in (
            ("ok", None, 5), ("ok", None, 2),
            ("rejected", "unknown_table", 3),
            ("execution_error", "execution", 1),
        ):
            jid = journal.write({
                "actor": "u@example.com", "role": "tm", "binding": "территория 58",
                "scope_label": "территория 58", "question": "Конверсия", "model": "qwen3:8b",
                "sql_raw": "SELECT 1", "sql_final": "SELECT 1", "verdict": verdict,
                "rule": rule, "message": None, "row_count": 1,
                "model_ms": 2000, "sql_ms": 1000, "prompt_version": "aaaa1111",
            })
            mid = store.append_message(
                self.dialog["id"], 7, f"Вопрос {verdict}", {"ok": verdict == "ok"}, jid)["id"]
            store.save_feedback(mid, 7, rating, "Причина" if rating <= 3 else "")
            self.ids[verdict + str(rating)] = mid
        # запрос на другой версии инструкции, без оценки
        journal.write({
            "actor": "u@example.com", "role": "tm", "binding": "территория 58",
            "scope_label": "территория 58", "question": "Ещё", "model": "qwen3:8b",
            "sql_raw": "SELECT 1", "sql_final": "SELECT 1", "verdict": "ok", "rule": None,
            "message": None, "row_count": 1, "model_ms": 500, "sql_ms": 200,
            "prompt_version": "bbbb2222",
        })

        app = FastAPI()
        app.include_router(ai_api.build_router(lambda: _Admin(), lambda: _Admin()))
        self.client = TestClient(app)

    def tearDown(self):
        journal.JOURNAL_DB, store.JOURNAL_DB = self._saved
        if self._flag is None:
            os.environ.pop("AI_DEMO_ENABLED", None)
        else:
            os.environ["AI_DEMO_ENABLED"] = self._flag
        self._tmp.cleanup()

    def test_refusal_and_technical_failure_are_counted_apart(self):
        # Отказ означает, что граница области данных сработала как задумано;
        # техническая ошибка — что сломался контур. Смешивать их нельзя:
        # лечатся они совершенно по-разному.
        summary = self.client.get("/api/ai/quality?days=30").json()["summary"]
        self.assertEqual(summary["asked"], 5)
        self.assertEqual(summary["answered"], 3)
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["rated"], 4)
        self.assertEqual(summary["average"], 2.75)
        self.assertEqual(summary["spread"]["5"], 1)

    def test_filters_narrow_the_feed(self):
        get = lambda q: self.client.get(f"/api/ai/quality?{q}").json()["entries"]
        self.assertEqual(len(get("days=30")), 4)
        self.assertEqual(len(get("rating=1")), 1)
        self.assertEqual(len(get("verdict=refused")), 1)
        self.assertEqual(len(get("verdict=failed")), 1)
        self.assertEqual(len(get("verdict=ok")), 2)
        self.assertEqual(len(get("role=tm")), 4)
        self.assertEqual(len(get("promptVersion=aaaa1111")), 4)
        self.assertEqual(len(get("promptVersion=bbbb2222")), 0)

    def test_first_response_date_is_set_once(self):
        # Иначе срок первичной реакции можно было бы «обновить» повторным
        # нажатием, и просроченных разборов в отчёте просто не стало бы.
        message_id = self.ids["execution_error1"]
        first = self.client.post(f"/api/ai/quality/{message_id}/review",
                                 json={"status": "review"}).json()["first_seen"]
        self.assertTrue(first)
        time.sleep(1.1)
        again = self.client.post(f"/api/ai/quality/{message_id}/review",
                                 json={"status": "resolved"}).json()
        self.assertEqual(again["first_seen"], first)
        self.assertEqual(again["status"], "resolved")

    def test_question_goes_to_the_golden_set_in_one_action(self):
        message_id = self.ids["ok2"]
        result = self.client.post(f"/api/ai/quality/{message_id}/review",
                                  json={"inGolden": True}).json()
        self.assertEqual(result["in_golden"], 1)
        entry = next(e for e in self.client.get("/api/ai/quality").json()["entries"]
                     if e["message_id"] == message_id)
        self.assertEqual(entry["in_golden"], 1)

    def test_unknown_status_and_unknown_message_are_refused(self):
        self.assertEqual(self.client.post("/api/ai/quality/999999/review",
                                          json={"status": "review"}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/ai/quality/{self.ids['ok5']}/review",
                                          json={"status": "чепуха"}).status_code, 400)

    def test_version_slice_separates_instruction_versions(self):
        versions = {v["version"]: v for v in self.client.get("/api/ai/quality").json()["versions"]}
        self.assertEqual(versions["aaaa1111"]["asked"], 4)
        self.assertEqual(versions["aaaa1111"]["rated"], 4)
        self.assertEqual(versions["aaaa1111"]["low"], 3)
        self.assertEqual(versions["bbbb2222"]["asked"], 1)
        self.assertIsNone(versions["bbbb2222"]["average"])

    def test_export_is_an_xlsx_workbook(self):
        response = self.client.get("/api/ai/quality/export?days=30")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content[:2], b"PK")
        self.assertIn("ai-quality-", response.headers["content-disposition"])

    def test_prompt_version_follows_the_instruction_text(self):
        from backend.ai import contract
        before = contract.prompt_version()
        saved = contract.RULES
        try:
            contract.RULES = saved + "\nНовое правило."
            self.assertNotEqual(contract.prompt_version(), before)
        finally:
            contract.RULES = saved
        self.assertEqual(contract.prompt_version(), before)


if __name__ == "__main__":
    unittest.main()
