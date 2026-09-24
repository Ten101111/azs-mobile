"""Папки и архив диалогов (ИИ-10) и сроки хранения истории (ИИ-02).

Проверяется: только личные папки (чужая недоступна), перенос, архив и
восстановление, удаление папки с выбором судьбы диалогов, лимиты роли
(активные, закреплённые, папки) с понятным отказом, ночная очистка по сроку
роли (закреплённые не удаляются, журнал аудита живёт дольше истории),
пометка за 7 дней до удаления, память диалога по роли и отказ API 409.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ai import api as ai_api
from backend.ai import dialogs as store
from backend.ai import journal, quality, quotas, retention

OWNER, STRANGER = 7, 8
RU = "regional_manager"
DAY = 86400


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (journal.JOURNAL_DB, store.JOURNAL_DB)
        path = pathlib.Path(self._tmp.name) / "journal.db"
        journal.JOURNAL_DB = path
        store.JOURNAL_DB = path
        quotas.set_overrides({})
        quotas.refresh()

    def tearDown(self):
        journal.JOURNAL_DB, store.JOURNAL_DB = self._saved
        quotas.set_overrides({})
        quotas.refresh()
        self._tmp.cleanup()

    def dialog(self, user=OWNER, role=RU, question="Выручка НТУ за август"):
        d = store.create_dialog(user, role=role)
        store.append_message(d["id"], user, question, {"ok": True}, None)
        return d["id"]

    def age(self, dialog_id, days):
        conn = sqlite3.connect(journal.JOURNAL_DB)
        conn.execute("UPDATE ai_dialogs SET updated_at = ? WHERE id = ?", (int(time.time()) - days * DAY, dialog_id))
        conn.commit()
        conn.close()


class FolderTests(Case):
    def test_personal_folders_move_and_foreign_access(self):
        folder = store.create_folder(OWNER, " Бюджет 2027 ", role=RU)
        self.assertEqual(folder["title"], "Бюджет 2027")
        with self.assertRaises(store.Invalid):
            store.create_folder(OWNER, "бюджет 2027", role=RU)        # то же имя без учёта регистра
        dialog_id = self.dialog()
        store.move_dialog(dialog_id, OWNER, folder["id"])
        self.assertEqual(store.list_folders(OWNER)[0]["dialogs"], 1)
        self.assertEqual(store.list_dialogs(OWNER, role=RU)[0]["folder_id"], folder["id"])
        # Р-6: папки только личные — чужую не видно и в неё не перенести.
        self.assertEqual(store.list_folders(STRANGER), [])
        other = self.dialog(user=STRANGER)
        with self.assertRaises(store.NotFound):
            store.move_dialog(other, STRANGER, folder["id"])
        with self.assertRaises(store.NotFound):
            store.rename_folder(folder["id"], STRANGER, "Моё")
        with self.assertRaises(store.NotFound):
            store.delete_folder(folder["id"], STRANGER)

    def test_delete_folder_moves_or_deletes_dialogs(self):
        keep = store.create_folder(OWNER, "Проверки", role=RU)
        gone = store.create_folder(OWNER, "Черновики", role=RU)
        a, b = self.dialog(), self.dialog()
        store.move_dialog(a, OWNER, keep["id"])
        store.move_dialog(b, OWNER, gone["id"])
        store.delete_folder(keep["id"], OWNER, "move")
        store.delete_folder(gone["id"], OWNER, "delete")
        dialogs = {d["id"]: d for d in store.list_dialogs(OWNER, role=RU)}
        self.assertIsNone(dialogs[a]["folder_id"])       # диалог остался «без папки»
        self.assertNotIn(b, dialogs)                     # удалён вместе с папкой
        self.assertEqual(store.list_folders(OWNER), [])

    def test_archive_hides_unpins_and_restores(self):
        dialog_id = self.dialog()
        store.set_pinned(dialog_id, OWNER, True, role=RU)
        store.set_archived(dialog_id, OWNER, True, role=RU)
        item = store.list_dialogs(OWNER, role=RU)[0]
        self.assertTrue(item["archived"])
        self.assertEqual(item["pinned"], 0)
        store.set_archived(dialog_id, OWNER, False, role=RU)
        self.assertFalse(store.list_dialogs(OWNER, role=RU)[0]["archived"])
        # Новый вопрос в архивном диалоге возвращает его в список.
        store.set_archived(dialog_id, OWNER, True, role=RU)
        store.append_message(dialog_id, OWNER, "Ещё вопрос", {"ok": True}, None)
        self.assertFalse(store.list_dialogs(OWNER, role=RU)[0]["archived"])


class LimitTests(Case):
    def test_active_pinned_and_folder_limits(self):
        quotas.set_overrides({("ru", "active_dialogs"): 2, ("ru", "pinned_dialogs"): 1, ("ru", "folders"): 1})
        first, second = self.dialog(), self.dialog()
        with self.assertRaises(store.Limit) as caught:
            store.create_dialog(OWNER, role=RU)
        self.assertEqual((caught.exception.param, caught.exception.action), ("active_dialogs", "archive_oldest"))
        self.assertIn("2 из 2", caught.exception.message)
        self.assertEqual(store.archive_oldest(OWNER, 1), 1)
        store.create_dialog(OWNER, role=RU)                 # место освободилось
        store.set_pinned(second, OWNER, True, role=RU)
        with self.assertRaises(store.Limit):
            store.set_pinned(self.dialog(user=OWNER, role=None), OWNER, True, role=RU)
        store.create_folder(OWNER, "Одна", role=RU)
        with self.assertRaises(store.Limit):
            store.create_folder(OWNER, "Вторая", role=RU)
        # Восстановление из архива — тоже в пределах лимита активных.
        with self.assertRaises(store.Limit):
            store.set_archived(first, OWNER, False, role=RU)


class RetentionTests(Case):
    def test_cleanup_follows_role_period_and_keeps_pinned_and_audit(self):
        old = self.dialog()
        fresh = self.dialog()
        pinned = self.dialog()
        unknown = self.dialog(role=None)
        store.set_pinned(pinned, OWNER, True, role=RU)
        for dialog_id, days in ((old, 100), (fresh, 50), (pinned, 100), (unknown, 100)):
            self.age(dialog_id, days)
        # Оценка по удаляемому ответу остаётся в журнале качества.
        jid = journal.write({"role": RU, "question": "Выручка НТУ за август", "verdict": "ok"})
        mid = store.append_message(old, OWNER, "Выручка НТУ за август", {"ok": True}, jid)["id"]
        store.save_feedback(mid, OWNER, 2, "Не та неделя")
        self.age(old, 100)
        result = retention.cleanup()
        ids = {d["id"] for d in store.list_dialogs(OWNER)}
        self.assertNotIn(old, ids)                   # РУ: 90 дней
        self.assertIn(fresh, ids)
        self.assertIn(pinned, ids)                   # закреплённый не удаляется
        self.assertIn(unknown, ids)                  # роль неизвестна — самый долгий срок
        self.assertEqual(result["dialogs"], 1)
        entries = quality.entries(days=30)
        self.assertEqual(entries[0]["question"], "Выручка НТУ за август")

    def test_audit_journal_has_its_own_period(self):
        jid = journal.write({"role": RU, "question": "Старый вопрос", "verdict": "ok"})
        conn = sqlite3.connect(journal.JOURNAL_DB)
        conn.execute("UPDATE ai_queries SET created_at = ? WHERE id = ?", (int(time.time()) - 400 * DAY, jid))
        conn.commit()
        conn.close()
        journal.write({"role": RU, "question": "Свежий вопрос", "verdict": "ok"})
        self.assertEqual(retention.cleanup()["audit"], 1)
        self.assertEqual([r["question"] for r in journal.recent()], ["Свежий вопрос"])

    def test_warning_seven_days_before_removal_and_storage_report(self):
        soon = self.dialog()
        self.age(soon, 85)
        later = self.dialog()
        items = {d["id"]: d for d in store.list_dialogs(OWNER, role=RU)}
        self.assertTrue(items[soon]["expires_soon"])
        self.assertFalse(items[later]["expires_soon"])
        report = retention.storage_report()
        ru = next(g for g in report["groups"] if g["code"] == "ru")
        self.assertEqual((ru["users"], ru["dialogs"], ru["expiringSoon"]), (1, 2, 1))
        self.assertTrue(retention.due())
        retention.cleanup()
        self.assertFalse(retention.due())


class _User:
    def __init__(self, uid=OWNER, role=RU):
        self.id = uid
        self.role = role
        self.isAdmin = False
        self.email = "ru@example.com"
        self.name = "Тест"
        self.aiDialog = True


class ApiTests(Case):
    def setUp(self):
        super().setUp()
        self._flag = os.environ.get("AI_DEMO_ENABLED")
        os.environ["AI_DEMO_ENABLED"] = "1"
        self._saved_ask = ai_api.pipeline.ask
        self.histories = []

        class _Answer:
            ok = True; question = "q"; scope_label = "РУ"; summary = "Итог"; sql = None; sql_raw = None
            columns = []; rows = []; notes = []; truncated = False; model = "m"; model_ms = 1; narrate_ms = 0
            sql_ms = 0; attempts = 1; error = None; rule = None; journal_id = None; depth = "fast"

        def fake_ask(question, role, binding, actor, model=None, on_stage=None, depth="auto", history=None,
                     control=None):
            self.histories.append(len(history or []))
            return _Answer()

        ai_api.pipeline.ask = fake_ask
        self.current = _User()
        app = FastAPI()
        app.include_router(ai_api.build_router(lambda: self.current, lambda: self.current))
        self.client = TestClient(app)

    def tearDown(self):
        ai_api.pipeline.ask = self._saved_ask
        if self._flag is None:
            os.environ.pop("AI_DEMO_ENABLED", None)
        else:
            os.environ["AI_DEMO_ENABLED"] = self._flag
        super().tearDown()

    def test_active_limit_refuses_new_dialog_before_the_model(self):
        quotas.set_overrides({("ru", "active_dialogs"): 1})
        first = self.client.post("/api/ai/ask", json={"question": "Выручка"}).json()
        response = self.client.post("/api/ai/ask/stream", json={"question": "Ещё"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["X-AI-Action"], "archive_oldest")
        self.assertEqual(len(self.histories), 1)            # модель не вызывалась
        # В существующем диалоге спрашивать можно.
        again = self.client.post("/api/ai/ask", json={"question": "Ещё", "dialogId": first["dialogId"]})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.client.post("/api/ai/dialogs/archive-oldest", json={"count": 5}).json()["archived"], 1)
        self.assertEqual(self.client.post("/api/ai/ask", json={"question": "Новый"}).status_code, 200)

    def test_dialog_memory_follows_role(self):
        quotas.set_overrides({("ru", "dialog_memory"): 2})
        dialog_id = self.client.post("/api/ai/ask", json={"question": "1"}).json()["dialogId"]
        for text in ("2", "3", "4"):
            self.client.post("/api/ai/ask", json={"question": text, "dialogId": dialog_id})
        self.assertEqual(self.histories, [0, 1, 2, 2])

    def test_folder_api_and_foreign_folder(self):
        folder = self.client.post("/api/ai/folders", json={"title": "Проверки"}).json()
        body = self.client.get("/api/ai/dialogs").json()
        self.assertEqual(body["folders"][0]["title"], "Проверки")
        self.assertEqual(body["limits"]["historyDays"], 90)
        self.current = _User(uid=STRANGER)
        self.assertEqual(self.client.patch(f"/api/ai/folders/{folder['id']}", json={"title": "x"}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/ai/folders/{folder['id']}").status_code, 404)
        self.assertEqual(self.client.post("/api/ai/folders", json={"title": " "}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
