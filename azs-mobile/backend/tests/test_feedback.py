"""Обратная связь из «Контроля» (общий бэклог №30): замечание доходит до администратора.

Раньше запись оставалась только в браузере. Проверяется: сохранение на сервере,
досылка старых записей без дублей, защита от цикла отправки, список и счётчики
для администратора, разбор по статусам с отметкой, кто разобрал, и отказ
не-администратору.
"""
from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import feedback


class _User:
    def __init__(self, uid, email, admin=False):
        self.id = uid
        self.email = email
        self.name = "Иванов Иван"
        self.isAdmin = admin


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = pathlib.Path(self._tmp.name) / "auth.sqlite3"

        def connection():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn

        self.user = _User(7, "tm@example.com")
        self.admin = _User(1, "admin@example.com", admin=True)
        self.current = self.user

        def require_admin():
            if not self.current.isAdmin:
                raise HTTPException(status_code=403, detail="Только администратор")
            return self.current

        app = FastAPI()
        app.include_router(feedback.build_router(lambda: self.current, require_admin, connection))
        self.client = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()

    def send(self, **body):
        body.setdefault("message", "Неверный телефон управляющего")
        return self.client.post("/api/feedback", json=body)

    def test_feedback_reaches_admin_with_author_and_status(self):
        response = self.send(station="2707", field="Телефон")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/admin/feedback").status_code, 403)
        self.current = self.admin
        body = self.client.get("/api/admin/feedback?status=new").json()
        self.assertEqual(body["counts"]["new"], 1)
        item = body["items"][0]
        self.assertEqual((item["station"], item["field"], item["userEmail"], item["status"]),
                         ("2707", "Телефон", "tm@example.com", "new"))

    def test_review_changes_status_and_records_reviewer(self):
        item_id = self.send().json()["id"]
        self.current = self.admin
        reviewed = self.client.patch(f"/api/admin/feedback/{item_id}",
                                     json={"status": "done", "note": "Телефон исправлен в реестре"}).json()
        self.assertEqual((reviewed["status"], reviewed["handledBy"]), ("done", "admin@example.com"))
        self.assertEqual(reviewed["note"], "Телефон исправлен в реестре")
        counts = self.client.get("/api/admin/feedback").json()["counts"]
        self.assertEqual((counts["new"], counts["done"]), (0, 1))
        self.assertEqual(self.client.patch(f"/api/admin/feedback/{item_id}", json={"status": "?"}).status_code, 400)
        self.assertEqual(self.client.patch("/api/admin/feedback/999", json={"status": "done"}).status_code, 404)

    def test_legacy_browser_entries_are_not_duplicated(self):
        first = self.send(clientId="local-2026-09-01-abc", createdAt="2026-09-01T10:00:00.000Z").json()
        again = self.send(clientId="local-2026-09-01-abc", createdAt="2026-09-01T10:00:00.000Z").json()
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["duplicate"])
        self.current = self.admin
        items = self.client.get("/api/admin/feedback").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "browser")
        self.assertEqual(items[0]["createdAt"], 1788256800)        # время из браузера сохранено

    def test_future_time_from_browser_is_ignored_and_empty_message_refused(self):
        self.send(clientId="x", createdAt="2099-01-01T00:00:00Z")
        self.assertEqual(self.send(message="   ").status_code, 422)
        self.current = self.admin
        created = self.client.get("/api/admin/feedback").json()["items"][0]["createdAt"]
        self.assertLess(created, 4_000_000_000)

    def test_hourly_limit_protects_from_a_sending_loop(self):
        with mock.patch.object(feedback, "HOURLY_LIMIT", 2):
            self.assertEqual(self.send().status_code, 200)
            self.assertEqual(self.send().status_code, 200)
            self.assertEqual(self.send().status_code, 429)


if __name__ == "__main__":
    unittest.main()
