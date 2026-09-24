"""Потоковая отдача ответа ИИ.

Здесь проверяется транспорт, а не работа модели: конвейер подменён, чтобы
тест не зависел от Ollama. Порядок и состав событий важен — на нём держится
строка рассуждения в интерфейсе.

Чего эти тесты не проверяют: что события действительно уходят по мере
появления, а не пачкой в конце. TestClient собирает тело целиком, поэтому
разброс во времени здесь не измерить — это проверяется прогоном через
настоящий сервер (см. «Аудит_визуала_раздела_ИИ.md», раздел о потоке).
"""
import json
import os
import pathlib
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ai import journal
from backend.ai import dialogs as store
from backend.ai import api as ai_api


class _Answer:
    ok = True
    question = "Конверсия НТУ за сентябрь"
    scope_label = "территория 58 · 18 АЗС"
    summary = "38,2 %"
    sql = "SELECT 1"
    sql_raw = "SELECT 1"
    columns = ["Конверсия, %"]
    rows = [[38.2]]
    notes = []
    truncated = False
    model = "qwen3:8b"
    model_ms = 2100
    narrate_ms = 300
    sql_ms = 1200
    attempts = 1
    error = None
    rule = None
    journal_id = None


class _User:
    def __init__(self, role="tm", is_admin=False):
        self.id = 7
        self.role = role
        self.isAdmin = is_admin
        self.email = "user@example.com"
        self.name = "Манохин А. А."
        self.roleTitle = "Территориальный менеджер"
        self.roleBinding = "территория 58"
        self.scopeLabel = "территория 58 · 18 АЗС"
        self.aiDialog = True


def _fake_ask(question, role, binding, actor, model=None, on_stage=None, **kwargs):
    for key in ("draft", "check", "read", "write"):
        on_stage({"key": key, "state": "active", "label": f"Идёт {key}"})
        on_stage({"key": key, "state": "done", "label": f"Готов {key}", "ms": 100})
    return _Answer()


def _frames(response):
    out = []
    for block in response.text.split("\n\n"):
        if not block.strip():
            continue
        name = next((l[6:].strip() for l in block.split("\n") if l.startswith("event:")), "")
        data = next((l[5:].strip() for l in block.split("\n") if l.startswith("data:")), "{}")
        out.append((name, json.loads(data)))
    return out


class AiStreamTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_db = (journal.JOURNAL_DB, store.JOURNAL_DB)
        self._saved_flag = os.environ.get("AI_DEMO_ENABLED")
        self._saved_ask = ai_api.pipeline.ask
        os.environ["AI_DEMO_ENABLED"] = "1"
        path = pathlib.Path(self._tmp.name) / "journal.db"
        journal.JOURNAL_DB = path
        store.JOURNAL_DB = path
        ai_api.pipeline.ask = _fake_ask
        self.current = _User()
        app = FastAPI()
        app.include_router(ai_api.build_router(self._who, self._who))
        self.client = TestClient(app)

    def tearDown(self):
        journal.JOURNAL_DB, store.JOURNAL_DB = self._saved_db
        ai_api.pipeline.ask = self._saved_ask
        if self._saved_flag is None:
            os.environ.pop("AI_DEMO_ENABLED", None)
        else:
            os.environ["AI_DEMO_ENABLED"] = self._saved_flag
        self._tmp.cleanup()

    def _who(self):
        return self.current

    def _ask(self):
        return self.client.post("/api/ai/ask/stream", json={"question": "Конверсия НТУ за сентябрь"})

    def test_stream_reports_every_stage_twice_and_in_order(self):
        response = self._ask()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        stages = [(d["key"], d["state"]) for n, d in _frames(response) if n == "stage"]
        self.assertEqual(stages, [
            ("draft", "active"), ("draft", "done"),
            ("check", "active"), ("check", "done"),
            ("read", "active"), ("read", "done"),
            ("write", "active"), ("write", "done"),
        ])

    def test_stream_ends_with_the_answer_and_saves_it(self):
        answers = [d for n, d in _frames(self._ask()) if n == "answer"]
        self.assertEqual(len(answers), 1)
        answer = answers[0]
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["summary"], "38,2 %")
        self.assertIsNotNone(answer["dialogId"])
        saved = store.messages(answer["dialogId"], 7)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["id"], answer["messageId"])

    def test_sql_is_stripped_for_roles_without_the_right(self):
        answer = [d for n, d in _frames(self._ask()) if n == "answer"][0]
        self.assertIsNone(answer["sql"])
        self.assertIsNone(answer["sqlRaw"])
        self.current = _User(role="admin", is_admin=True)
        answer = [d for n, d in _frames(self._ask()) if n == "answer"][0]
        self.assertEqual(answer["sql"], "SELECT 1")

    def test_refusal_comes_before_the_stream_opens(self):
        # Отказ по правам должен приходить обычным кодом ответа: иначе клиент
        # увидит успешно открытый поток и ошибку внутри него.
        self.current = _User(role="")
        response = self._ask()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.headers["content-type"].startswith("text/event-stream"))

    def test_pipeline_failure_arrives_as_a_failed_event(self):
        def boom(*args, **kwargs):
            raise RuntimeError("Витрина недоступна")
        ai_api.pipeline.ask = boom
        frames = _frames(self._ask())
        # Первым идёт номер запуска (ИИ-03: по нему вопрос можно остановить).
        self.assertEqual([n for n, _ in frames], ["run", "failed"])
        self.assertEqual(frames[1][1]["detail"], "Витрина недоступна")


if __name__ == "__main__":
    unittest.main()
