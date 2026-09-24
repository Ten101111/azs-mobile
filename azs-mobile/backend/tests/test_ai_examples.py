"""Примеры вопросов по роли (ИИ-08): по каждому примеру должен быть ответ.

Проверяется по семантическому слою витрины ОХД: показатель примера находится
поиском по слою, в примере нет того, чего в витрине нет, нет месяцев и годов,
а роль с ограниченной областью не спрашивает о сети и чужих обществах.
"""
from __future__ import annotations

import re
import unittest

from backend import roles
from backend.ai import examples, semantic

DATE_WORDS = re.compile(r"\b(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|июн\w*|июл\w*|август\w*|сентябр\w*|"
                        r"октябр\w*|ноябр\w*|декабр\w*|20\d\d)\b", re.I)
WIDE_WORDS = re.compile(r"\b(сет[иь]|обществам|всех обществ|соседн\w*)\b", re.I)
OWN_WORDS = re.compile(r"\b(мо[йяеи]\w*|наш\w*)\b", re.I)


class ExamplesTests(unittest.TestCase):
    def test_every_role_gets_three_short_examples(self):
        for code in roles.ROLE_ORDER:
            with self.subTest(role=code):
                items = examples.for_role(code)
                self.assertEqual(len(items), 3)
                self.assertEqual(len({i["ask"] for i in items}), 3)
                for item in items:
                    self.assertLessEqual(len(item["hint"]), 24, item["hint"])
                    self.assertLessEqual(len(item["ask"]), 200, item["ask"])
        self.assertEqual(examples.for_role(""), examples.for_role("territory_manager"))  # роль ещё не назначена

    def test_examples_ask_about_what_the_mart_has(self):
        layer = semantic.DWH
        for code in roles.ROLE_ORDER:
            for item in examples.examples_of(code):
                with self.subTest(role=code, ask=item.ask):
                    self.assertIn(item.metric, layer.metrics)
                    found = layer.find(item.ask)
                    self.assertEqual(found["absent"], [])
                    # Показатель примера — среди двух первых находок слоя: агент увидит его сразу.
                    self.assertIn(item.metric, [m["key"] for m in found["metrics"][:2]])
                    self.assertIsNone(DATE_WORDS.search(item.ask), "пример устареет: в нём месяц или год")

    def test_scope_wording_matches_the_role(self):
        for code in roles.ROLE_ORDER:
            spec = roles.ROLES[code]
            for item in examples.examples_of(code):
                with self.subTest(role=code, ask=item.ask):
                    if spec.unrestricted:
                        self.assertIsNone(OWN_WORDS.search(item.ask), "у неограниченной роли нет «своих» объектов")
                    else:
                        self.assertIsNone(WIDE_WORDS.search(item.ask), "ограниченная роль не спрашивает о сети")

    def test_managers_compare_only_with_their_own_level(self):
        management = " ".join(item.ask for item in examples.examples_of("regional_manager"))
        self.assertIn("территориальным менеджерам", management)
        self.assertNotIn("руководител", management)
        self.assertEqual(examples.for_role("admin"), examples.for_role("aup_network"))


if __name__ == "__main__":
    unittest.main()
