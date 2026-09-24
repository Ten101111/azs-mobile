"""Маршруты раздела ИИ: доступ, оценки и сводка качества.

Стенд и витрина здесь не нужны: проверяются права и формат ответов, а не
содержательный результат запроса. Поэтому сообщение кладётся в хранилище
напрямую — /ask обращается к модели, которой в тестах нет.
"""
import os
import pathlib
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ai import journal
from backend.ai import dialogs as store
from backend.ai import api as ai_api

STORED_ANSWER = {"ok": True, "sql": "SELECT 1", "rows": [[1]], "columns": ["n"]}


class _User:
    def __init__(self, uid, role, is_admin=False):
        self.id = uid
        self.role = role
        self.isAdmin = is_admin
        self.email = f"user{uid}@example.com"
        self.name = "Манохин А. А."
        self.roleTitle = "Территориальный менеджер"
        self.roleBinding = "территория 58"
        self.scopeLabel = "территория 58 · 18 АЗС"
        self.aiDialog = True


class AiApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_db = (journal.JOURNAL_DB, store.JOURNAL_DB)
        self._saved_flag = os.environ.get("AI_DEMO_ENABLED")
        os.environ["AI_DEMO_ENABLED"] = "1"
        path = pathlib.Path(self._tmp.name) / "journal.db"
        journal.JOURNAL_DB = path
        store.JOURNAL_DB = path

        self.current = _User(7, "tm")
        app = FastAPI()
        app.include_router(ai_api.build_router(self._who, self._who))
        self.client = TestClient(app)

        self.dialog_id = self.client.post("/api/ai/dialogs", json={"title": ""}).json()["id"]
        self.message_id = store.append_message(
            self.dialog_id, 7, "Выручка НТУ за сентябрь", STORED_ANSWER, None)["id"]

    def tearDown(self):
        journal.JOURNAL_DB, store.JOURNAL_DB = self._saved_db
        if self._saved_flag is None:
            os.environ.pop("AI_DEMO_ENABLED", None)
        else:
            os.environ["AI_DEMO_ENABLED"] = self._saved_flag
        self._tmp.cleanup()

    def _who(self):
        return self.current

    def test_status_reports_role_scope_and_sql_visibility(self):
        data = self.client.get("/api/ai/status").json()
        self.assertFalse(data["maySeeSql"])
        self.assertEqual(data["commentRequiredUpTo"], store.COMMENT_REQUIRED_UPTO)
        self.assertEqual(data["ownScopeLabel"], "территория 58 · 18 АЗС")
        self.assertIsInstance(data["ownStations"], int)
        self.assertFalse(data["ownUnrestricted"])
        self.current = _User(9, "admin", is_admin=True)
        admin = self.client.get("/api/ai/status").json()
        self.assertTrue(admin["maySeeSql"])
        self.assertTrue(admin["ownUnrestricted"])  # приветствие ИИ-08: «по всей сети»
        self.assertEqual(admin["examples"][0]["hint"], "План НТУ по обществам")
        self.assertEqual(data["examples"][0]["hint"], "Выручка НТУ за месяц")  # роль без набора — «мои АЗС»
        self.assertEqual(data["examplesByRole"], {})  # примеры других ролей — только тому, кто спрашивает «от имени»

    def test_feedback_rejects_low_rating_without_a_reason(self):
        bad = self.client.post("/api/ai/feedback",
                               json={"messageId": self.message_id, "rating": 2, "comment": ""})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("до трёх звёзд", bad.json()["detail"])
        good = self.client.post("/api/ai/feedback",
                                json={"messageId": self.message_id, "rating": 2, "comment": "Период не тот"})
        self.assertEqual(good.status_code, 200)
        out_of_range = self.client.post("/api/ai/feedback",
                                        json={"messageId": self.message_id, "rating": 9, "comment": "x"})
        self.assertEqual(out_of_range.status_code, 422)

    def test_another_user_sees_nothing_of_the_dialog(self):
        self.current = _User(8, "tm")
        self.assertEqual(self.client.get("/api/ai/dialogs").json()["dialogs"], [])
        self.assertEqual(self.client.get(f"/api/ai/dialogs/{self.dialog_id}/messages").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/ai/dialogs/{self.dialog_id}").status_code, 404)
        self.assertEqual(
            self.client.post("/api/ai/feedback",
                             json={"messageId": self.message_id, "rating": 5, "comment": ""}).status_code,
            404,
        )

    def test_administrator_reads_the_digest_but_not_the_correspondence(self):
        self.client.post("/api/ai/feedback",
                         json={"messageId": self.message_id, "rating": 2, "comment": "Период не тот"})
        self.current = _User(9, "admin", is_admin=True)
        digest = self.client.get("/api/ai/quality")
        self.assertEqual(digest.status_code, 200)
        self.assertEqual(digest.json()["summary"]["rated"], 1)
        self.assertEqual(digest.json()["summary"]["spread"]["2"], 1)
        # Модель приватности ИБ-4: администратор работает с оценками,
        # а не с чужой перепиской.
        self.assertEqual(self.client.get(f"/api/ai/dialogs/{self.dialog_id}/messages").status_code, 404)

    def test_rename_and_pin_are_reflected_in_the_list(self):
        self.assertEqual(self.client.patch(f"/api/ai/dialogs/{self.dialog_id}",
                                           json={"title": "Сентябрь"}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/ai/dialogs/{self.dialog_id}/pin",
                                          json={"pinned": True}).status_code, 200)
        first = self.client.get("/api/ai/dialogs").json()["dialogs"][0]
        self.assertEqual(first["title"], "Сентябрь")
        self.assertEqual(first["pinned"], 1)

    def test_section_disappears_when_the_flag_is_off(self):
        os.environ["AI_DEMO_ENABLED"] = "0"
        self.assertEqual(self.client.get("/api/ai/status").status_code, 404)


if __name__ == "__main__":
    unittest.main()
