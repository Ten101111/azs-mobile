"""Уровни глубины (ИИ-20): подписи, ориентир времени по журналу, выбранный уровень в ответе."""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.ai import api as ai_api, generator, journal, pipeline
from backend.tests.test_ai_agent import StandCase


class JournalTimingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = journal.JOURNAL_DB
        journal.JOURNAL_DB = Path(self._tmp.name) / "journal.sqlite3"

    def tearDown(self):
        journal.JOURNAL_DB = self._saved
        self._tmp.cleanup()

    def add(self, depth, total_ms, verdict="ok", age_days=0):
        entry_id = journal.write({"question": "q", "verdict": verdict, "depth": depth})
        journal.set_total_ms(entry_id, total_ms)
        if age_days:
            conn = sqlite3.connect(journal.JOURNAL_DB)
            conn.execute("UPDATE ai_queries SET created_at = ? WHERE id = ?",
                         (int(time.time()) - age_days * 86400, entry_id))
            conn.commit()
            conn.close()

    def test_median_uses_only_recent_successful_answers(self):
        for ms in (10_000, 20_000, 30_000, 40_000, 50_000):
            self.add("analyze", ms)
        self.add("analyze", 1_000, verdict="unknown_table")    # отказ не занижает ориентир
        self.add("analyze", 900_000, age_days=10)              # старше недели — не считается
        self.add("fast", 4_000)
        timings = journal.depth_timings(days=7)
        self.assertEqual(timings["analyze"], {"median_ms": 30_000, "count": 5})
        self.assertEqual(timings["fast"]["count"], 1)

    def test_even_count_median(self):
        for ms in (10_000, 20_000):
            self.add("deep", ms)
        self.assertEqual(journal.depth_timings()["deep"]["median_ms"], 15_000)

    def test_depth_options_use_journal_only_with_enough_samples(self):
        for ms in (61_000, 62_000, 63_000, 64_000, 65_000):
            self.add("analyze", ms)
        self.add("deep", 120_000)
        options = {item["code"]: item for item in ai_api.depth_options()}
        self.assertEqual([item["title"] for item in ai_api.DEPTH_OPTIONS], ["Авто", "Лёгкий", "Средний", "Высокий"])
        self.assertEqual(options["analyze"]["typical"], "обычно около 1 мин 3 с")
        self.assertEqual(options["analyze"]["samples"], 5)
        self.assertEqual(options["deep"]["typical"], "до 4 мин")      # одного ответа мало для ориентира
        self.assertEqual(options["deep"]["samples"], 1)
        self.assertEqual({item["code"] for item in ai_api.DEPTH_OPTIONS}, set(pipeline.DEPTHS))


class AnswerDepthTests(StandCase):
    def test_answer_carries_requested_depth_and_total_time(self):
        def fake_generate(question, feedback=None, model=None, context="", **_):
            sql = "SELECT SUM(checks) AS checks FROM station_kpi_daily"
            return generator.Generated(sql=sql, raw=sql, model="test", elapsed_ms=1)

        with mock.patch.object(pipeline.generator, "generate", fake_generate), \
                mock.patch.object(pipeline.generator, "narrate", lambda *a, **k: ("готово", 1)):
            answer = pipeline.ask("Сколько чеков?", "admin", None, "test", depth="fast")
        self.assertTrue(answer.ok, answer.error)
        self.assertEqual(answer.depth_requested, "fast")
        self.assertGreaterEqual(answer.total_ms, 0)
        conn = sqlite3.connect(journal.JOURNAL_DB)
        stored = conn.execute("SELECT total_ms FROM ai_queries WHERE id = ?", (answer.journal_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(stored, answer.total_ms)
        response = ai_api._response(answer, show_sql=False)
        self.assertEqual(response.depthRequested, "fast")
        self.assertEqual(response.totalMs, answer.total_ms)

    def test_unknown_depth_is_reported_as_auto(self):
        with mock.patch.object(pipeline, "_ask", lambda *a, **k: pipeline.Answer(ok=False, question="q", scope_label="")):
            answer = pipeline.ask("q", "admin", None, depth="turbo")
        self.assertEqual(answer.depth_requested, "auto")


if __name__ == "__main__":
    unittest.main()
