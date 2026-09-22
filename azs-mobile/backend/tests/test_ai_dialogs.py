"""Диалоги ИИ: владение, обязательный комментарий, видимость SQL.

Эти три правила — не удобство интерфейса, а требования ИБ и разбора качества,
поэтому проверяются на уровне хранилища: спрятать кнопку вёрсткой недостаточно.
"""
import pathlib
import tempfile
import unittest

from backend.ai import journal
from backend.ai import dialogs as store
from backend.ai import quality
from backend.ai.api import _may_see_sql

ANSWER = {
    "ok": True, "question": "Выручка НТУ", "scopeLabel": "территория 58",
    "summary": "48,7 млн ₽", "sql": "SELECT 1", "columns": ["Выручка"],
    "rows": [[48714902.4]], "modelMs": 2100, "narrateMs": 300, "sqlMs": 1200,
    "rowCount": 1, "attempts": 1,
}

OWNER = 7
STRANGER = 8


class _User:
    def __init__(self, role, is_admin=False):
        self.role = role
        self.isAdmin = is_admin


class AiDialogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (journal.JOURNAL_DB, store.JOURNAL_DB)
        path = pathlib.Path(self._tmp.name) / "journal.db"
        journal.JOURNAL_DB = path
        store.JOURNAL_DB = path
        self.dialog = store.create_dialog(OWNER)
        self.message = store.append_message(
            self.dialog["id"], OWNER,
            "Выручка НТУ по моим АЗС за сентябрь 2026", ANSWER, None,
        )

    def tearDown(self):
        journal.JOURNAL_DB, store.JOURNAL_DB = self._saved
        self._tmp.cleanup()

    def test_dialog_takes_its_name_from_the_first_question(self):
        self.assertEqual(self.dialog["title"], "Новый диалог")
        self.assertTrue(self.message["title"].startswith("Выручка НТУ"))

    def test_another_user_can_neither_read_nor_rate(self):
        with self.assertRaises(store.NotFound):
            store.messages(self.dialog["id"], STRANGER)
        with self.assertRaises(store.NotFound):
            store.save_feedback(self.message["id"], STRANGER, 5, "")
        self.assertEqual(store.list_dialogs(STRANGER), [])

    def test_low_rating_requires_a_reason(self):
        with self.assertRaises(store.Invalid):
            store.save_feedback(self.message["id"], OWNER, 2, "   ")
        store.save_feedback(self.message["id"], OWNER, 2, "Период не тот")
        self.assertEqual(store.messages(self.dialog["id"], OWNER)[0]["rating"], 2)

    def test_high_rating_needs_no_reason_and_replaces_the_previous_one(self):
        store.save_feedback(self.message["id"], OWNER, 2, "Период не тот")
        store.save_feedback(self.message["id"], OWNER, 5, "")
        rows = store.messages(self.dialog["id"], OWNER)
        self.assertEqual(rows[0]["rating"], 5)
        self.assertEqual(rows[0]["comment"], "")

    def test_rename_pin_and_delete(self):
        store.set_pinned(self.dialog["id"], OWNER, True)
        self.assertEqual(store.list_dialogs(OWNER)[0]["pinned"], 1)
        store.rename_dialog(self.dialog["id"], OWNER, "  Выручка сентября  ")
        self.assertEqual(store.list_dialogs(OWNER)[0]["title"], "Выручка сентября")
        with self.assertRaises(store.Invalid):
            store.rename_dialog(self.dialog["id"], OWNER, "   ")
        store.save_feedback(self.message["id"], OWNER, 5, "")
        store.delete_dialog(self.dialog["id"], OWNER)
        self.assertEqual(store.list_dialogs(OWNER), [])
        # Удаление диалога уносит и оценки: иначе разбор качества ссылался бы
        # на вопрос, которого уже нет.
        self.assertEqual(quality.summary()["rated"], 0)

    def test_sql_is_visible_only_to_administrators(self):
        self.assertTrue(_may_see_sql(_User("admin")))
        self.assertTrue(_may_see_sql(_User("subadmin")))
        self.assertFalse(_may_see_sql(_User("tm")))
        self.assertFalse(_may_see_sql(_User("mng")))


if __name__ == "__main__":
    unittest.main()
