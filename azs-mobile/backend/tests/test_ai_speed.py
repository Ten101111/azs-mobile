"""Скорость «Среднего» и «Высокого» (28.09.2026): запрет повторов и сжатие переписки с запасом.

По журналу: 94 % времени ответа — модель, проверки — меньше 1 %. Потери — повторы
(один расчёт Python 11 раз подряд, ≈4 минуты) и сжатие переписки почти на каждом ходе
(каждое — перечитывание 15–17 тыс. токенов, 16–36 с). Модель подменена сценарием.
"""
from __future__ import annotations

import json
import unittest

from backend.ai import semantic
from backend.ai.agent import llm, loop
from backend.ai.agent.llm import Reply, ToolCall
from backend.ai.agent.state import Plan
from backend.tests.test_ai_agent import StandCase

SQL = "SELECT ROUND(SUM(revenue_ntu)) AS \"Выручка НТУ\" FROM station_kpi_daily WHERE period = '2026-08'"
BAD_SQL = "SELECT nothing FROM no_such_table"
CODE = "print(1 + 1)"


def _plan():
    return Plan(standalone_question="Выручка НТУ за август", task_type="lookup", depth="deep", steps=["a"])


class RepeatTests(StandCase):
    def _run(self, script):
        model = llm.ScriptedModel(script)
        outcome = loop.run("Выручка НТУ за август", self.scope, plan=_plan(), model=model,
                           catalog=self._stand_catalog(), semantic=semantic.STAND, today="2026-09-22")
        return outcome, model

    def test_same_query_runs_once_and_collection_stops(self):
        script = [{"tool": "run_sql", "arguments": {"sql": SQL, "purpose": f"шаг {i}"}} for i in range(6)]
        script.append({"headline": "Выручка НТУ за август посчитана.", "happened": []})   # итог по собранным данным
        outcome, model = self._run(script)
        sql_steps = [s for s in outcome.workspace.steps if s.kind == "sql"]
        self.assertEqual(len(sql_steps), 1)                   # выполнен один раз
        self.assertEqual(outcome.repeats, loop.MAX_REPEATS)
        self.assertEqual(outcome.stop_reason, "повтор шагов")
        self.assertTrue(any("повторял уже сделанный шаг" in item for item in outcome.analysis.limitations))
        # Модели сказано, где готовый результат, а не выполнено заново.
        answer = json.loads(model.transcript[2][-1]["content"])
        self.assertEqual(answer["error"], "повтор")
        self.assertIn("r1", answer["message"])

    def test_purpose_and_subtask_do_not_make_a_new_query(self):
        self.assertEqual(loop._call_key("run_sql", {"sql": SQL + ";", "purpose": "а", "subtask": "s1"}),
                         loop._call_key("run_sql", {"sql": "  " + SQL.replace(" ", "  "), "purpose": "б"}))
        self.assertNotEqual(loop._call_key("run_sql", {"sql": SQL}), loop._call_key("run_sql", {"sql": SQL + " LIMIT 5"}))
        self.assertIsNone(loop._call_key("finish", {"headline": "x"}))

    def test_failed_query_may_be_retried_once(self):
        script = [{"tool": "run_sql", "arguments": {"sql": BAD_SQL}} for _ in range(5)]
        script.append({"headline": "Данных нет.", "happened": []})
        outcome, _ = self._run(script)
        self.assertEqual(outcome.repeats, loop.MAX_REPEATS)
        self.assertEqual(outcome.turns, 4)                    # две попытки, два отказа — и конец сбора

    def test_different_calls_are_not_repeats(self):
        script = [
            {"tool": "run_python", "arguments": {"code": CODE}},
            {"tool": "run_python", "arguments": {"code": "print(2 + 2)"}},
            {"tool": "finish", "arguments": {"headline": "Готово.", "happened": []}},
        ]
        outcome, _ = self._run(script)
        self.assertEqual(outcome.repeats, 0)
        self.assertEqual(outcome.stop_reason, "finish")


class TrimTests(unittest.TestCase):
    def _messages(self, results: int, size: int = 1200) -> list[dict]:
        messages = [{"role": "system", "content": "с" * 2000}, {"role": "user", "content": "вопрос"}]
        for i in range(results):
            messages.append({"role": "assistant", "content": "",
                             "tool_calls": [{"function": {"name": "run_python", "arguments": {"code": "x = 1\n" * 120}}}]})
            messages.append({"role": "tool", "content": json.dumps({"id": f"p{i}", "rows": [[i] * (size // 4)]})})
        return messages

    def test_trim_goes_below_target_so_next_turns_do_not_trim(self):
        messages = self._messages(8)
        limit = loop.context_chars(messages) - 200
        target = loop.context_target_chars(limit)
        self.assertTrue(loop._trim(messages, limit_chars=limit, target_chars=target))
        self.assertLessEqual(loop.context_chars(messages), target)
        # Ещё два хода с результатами — переписка ниже предела, сжатия и перечитывания нет.
        extra = self._messages(2)[2:]
        messages.extend(extra)
        self.assertFalse(loop._trim(messages, limit_chars=limit, target_chars=target))

    def test_last_result_and_recent_calls_stay_whole(self):
        messages = self._messages(6)
        limit = loop.context_chars(messages) - 200
        loop._trim(messages, limit_chars=limit, target_chars=loop.context_target_chars(limit))
        tool_messages = [m for m in messages if m["role"] == "tool"]
        self.assertFalse(tool_messages[-1]["content"].endswith(loop.BRIEF_MARK))
        calls = [m for m in messages if m["role"] == "assistant"]
        self.assertFalse(calls[-1]["tool_calls"][0]["function"]["arguments"]["code"].endswith(loop.BRIEF_MARK))

    def test_chars_per_token_is_measured(self):
        self.assertGreater(loop.context_limit_chars(1500, 3.5), loop.context_limit_chars(1500))
        reply = Reply(tool_calls=[ToolCall(name="get_data_range", arguments={}, id="1")], raw={"prompt_eval_count": 9000})
        self.assertEqual(loop._prompt_tokens(reply), 9000)
        self.assertEqual(loop._prompt_tokens(Reply()), 0)


if __name__ == "__main__":
    unittest.main()
