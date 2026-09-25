"""Скорость ответа ИИ (25.09.2026): бюджет «Среднего» у администратора, кэш
контекста Ollama, время модели в журнале.

Модель подменена сценарием: проверяется, что переписка с моделью только
дописывается (Ollama берёт её начало из кэша и читает лишь новое), сжимается
редко и разом, а время чтения, письма и загрузки модели доходит до журнала.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from backend.ai import generator, journal, limits, semantic
from backend.ai.agent import llm, loop, prompts, tools
from backend.ai.agent.state import Budget, Plan
from backend.tests.test_ai_agent import StandCase

BIG_SQL = 'SELECT metric_date AS "Дата", ksss AS "АЗС", revenue AS "Выручка, ₽" FROM station_kpi_daily ORDER BY metric_date, ksss'


def _script(sql_steps: int) -> list:
    steps = [{"tool": "run_sql", "arguments": {"sql": BIG_SQL, "purpose": f"шаг {i + 1}"}} for i in range(sql_steps)]
    steps.append({"tool": "finish", "arguments": {"headline": "Итог по стенду.", "happened": [], "main_result": "r1"}})
    return steps


def _is_prefix(shorter: list[dict], longer: list[dict]) -> bool:
    if len(shorter) > len(longer):
        return False
    return all(a.get("role") == b.get("role") and a.get("content") == b.get("content")
               for a, b in zip(shorter, longer))


class AdminAnalyzeBudgetTests(unittest.TestCase):
    def test_admin_medium_gets_usual_steps_but_keeps_time_and_rows(self):
        run = limits.for_run("admin", "analyze")
        budget = Budget.for_depth("analyze", run)
        usual = Budget.for_depth("analyze")
        self.assertEqual((budget.sql_calls, budget.python_calls, budget.charts, budget.model_turns),
                         (usual.sql_calls, usual.python_calls, usual.charts, usual.model_turns))
        self.assertEqual(budget.row_limit, limits.ADMIN.agent_rows)   # строки — без предела
        self.assertEqual(run.max_seconds, 0)                           # время — без предела

    def test_admin_high_stays_unlimited(self):
        budget = Budget.for_depth("deep", limits.for_run("admin", "deep"))
        self.assertEqual(budget.sql_calls, limits.ADMIN.sql_calls)
        self.assertEqual(budget.model_turns, limits.ADMIN.model_turns)

    def test_switch_returns_old_behaviour(self):
        saved = limits.ADMIN_ANALYZE_UNLIMITED
        limits.ADMIN_ANALYZE_UNLIMITED = True
        try:
            budget = Budget.for_depth("analyze", limits.for_run("admin", "analyze"))
            self.assertEqual(budget.model_turns, limits.ADMIN.model_turns)
        finally:
            limits.ADMIN_ANALYZE_UNLIMITED = saved

    def test_other_roles_unchanged(self):
        budget = Budget.for_depth("analyze", limits.for_run("aup_npo", "analyze"))
        self.assertEqual(budget.model_turns, Budget.for_depth("analyze").model_turns)


class ContextCacheTests(StandCase):
    def _run(self, steps: int, limit_chars: int | None = None):
        saved = loop.context_limit_chars
        if limit_chars is not None:
            loop.context_limit_chars = lambda reply_tokens=None: limit_chars
        try:
            model = llm.ScriptedModel(_script(steps))
            plan = Plan(standalone_question="Выручка по дням", task_type="compare", depth="deep", steps=["a"])
            outcome = loop.run("Выручка по дням", self.scope, plan=plan, model=model,
                               catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        finally:
            loop.context_limit_chars = saved
        return outcome, model.transcript[:steps + 1]   # ходы цикла, без итогового шага

    def test_conversation_only_grows_while_it_fits(self):
        outcome, turns = self._run(8)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.context_trims, 0)
        for before, after in zip(turns, turns[1:]):
            self.assertTrue(_is_prefix(before, after), "начало переписки изменилось — Ollama перечитает контекст")

    def test_old_results_are_squeezed_at_once_near_the_limit(self):
        _, probe = self._run(5)
        specs = [spec.as_ollama() for spec in tools.specs()] + [prompts.FINISH_TOOL]
        limit = loop.context_chars(probe[-1], specs) + 500   # после пяти результатов предел пройден
        outcome, turns = self._run(8, limit_chars=limit)
        # Сжатие — один раз за восемь шагов, а не на каждом ходе, как было до 25.09.2026.
        self.assertEqual(outcome.context_trims, 1)
        breaks = sum(1 for before, after in zip(turns, turns[1:]) if not _is_prefix(before, after))
        self.assertEqual(breaks, 1)                           # начало меняется только при сжатии
        last = turns[-1]
        tools_seen = [m for m in last if m.get("role") == "tool"]
        briefed = [m["content"].endswith(loop.BRIEF_MARK) for m in tools_seen]
        self.assertTrue(briefed[0])                                    # старые — сжаты
        self.assertEqual(briefed, sorted(briefed, reverse=True))       # сжато только начало, подряд
        self.assertFalse(any(briefed[-loop.KEEP_RECENT_RESULTS:]))     # последние — целиком

    def test_trim_does_nothing_below_the_limit(self):
        messages = [{"role": "system", "content": "с"}, {"role": "user", "content": "в"},
                    {"role": "tool", "content": json.dumps({"id": "r1", "rows": [[1] * 200]})}]
        self.assertFalse(loop._trim(messages, limit_chars=10_000))
        self.assertTrue(loop._trim(messages, limit_chars=10, keep_recent=0))
        self.assertTrue(messages[2]["content"].endswith(loop.BRIEF_MARK))
        self.assertFalse(loop._trim(messages, limit_chars=10, keep_recent=0))   # повторно не трогает

    def test_limit_follows_context_size(self):
        self.assertGreater(loop.context_limit_chars(1500), loop.context_limit_chars(4096))
        self.assertEqual(llm.NUM_CTX, generator.NUM_CTX)


class NoThinkMarkTests(unittest.TestCase):
    def test_mark_stays_in_every_user_message(self):
        chat = llm.OllamaChat(model="qwen3.8:latest")
        first = [{"role": "system", "content": "с"}, {"role": "user", "content": "вопрос"}]
        second = first + [{"role": "assistant", "content": "…"}, {"role": "user", "content": "продолжай"}]
        a, b = chat._prepare(first), chat._prepare(second)
        self.assertEqual(a, b[:2])                                     # начало не изменилось
        self.assertTrue(all(m["content"].endswith("/no_think") for m in b if m["role"] == "user"))
        self.assertEqual(first[1]["content"], "вопрос")                # исходные сообщения не тронуты


class ModelTimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = journal.JOURNAL_DB
        journal.JOURNAL_DB = pathlib.Path(self._tmp.name) / "journal.sqlite3"

    def tearDown(self):
        journal.JOURNAL_DB = self._db
        self._tmp.cleanup()

    def test_usage_counts_read_write_and_load_time(self):
        usage = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "trace": []}
        token = generator.USAGE.set(usage)
        try:
            generator._count_usage({"prompt_eval_count": 9000, "prompt_eval_duration": 30_000_000_000,
                                    "eval_count": 300, "eval_duration": 12_000_000_000, "load_duration": 5_000_000_000})
            generator._count_usage({"prompt_eval_count": 800, "prompt_eval_duration": 2_000_000_000,
                                    "eval_count": 200, "eval_duration": 8_000_000_000, "load_duration": 40_000_000})
        finally:
            generator.USAGE.reset(token)
        self.assertEqual((usage["calls"], usage["tokens_in"], usage["tokens_out"]), (2, 9800, 500))
        self.assertEqual((usage["prompt_ms"], usage["eval_ms"], usage["load_ms"]), (32_000, 20_000, 5_040))
        self.assertEqual(usage["trace"][1], {"in": 800, "out": 200, "promptMs": 2000, "evalMs": 8000, "loadMs": 40})

    def test_journal_keeps_model_time_and_summarises_it(self):
        entry = journal.write({"actor": "a@b", "role": "admin", "question": "q", "verdict": "ok", "depth": "analyze"})
        journal.set_run_stats(entry, total_ms=90_000, tokens_in=12_000, tokens_out=900, model_calls=6,
                              model_prompt_ms=20_000, model_eval_ms=50_000, model_load_ms=0,
                              model_trace=json.dumps({"calls": [], "trims": 1}))
        timing = journal.model_timings(7)
        self.assertEqual(len(timing), 1)
        item = timing[0]
        self.assertEqual((item["depth"], item["answers"], item["promptMs"], item["evalMs"]), ("analyze", 1, 20_000, 50_000))
        self.assertEqual((item["callsPerAnswer"], item["tokensInPerCall"]), (6.0, 2000))


if __name__ == "__main__":
    unittest.main()
