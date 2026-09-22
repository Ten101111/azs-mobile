"""Приведение типов на выходе исполнителя SQL.

Сюда вынесено единственное место, где значения драйвера превращаются
в то, что можно отдать в JSON. Повод — ошибка «Object of type Decimal is
not JSON serializable»: psycopg2 отдаёт NUMERIC как Decimal, и первый же
запрос с суммой выручки на витрине падал, хотя на стенде SQLite тот же
вопрос отрабатывал (там такого типа просто нет).

Тесты держат два условия:
  1. ответ переживает json.dumps и последующий JSON.parse в браузере;
  2. одинаковый вопрос даёт одинаковые типы на обоих исполнителях —
     иначе ответ на витрине будет отличаться от ответа на стенде.
"""
import datetime as dt
import json
import math
import unittest
import uuid
from decimal import Decimal

from backend.ai.executor import _normalize


def dumps(value):
    """json.dumps в том же режиме, что и в потоке ответа."""
    return json.dumps(value, ensure_ascii=False)


class NormalizeTests(unittest.TestCase):
    def test_decimal_s_drobyu_stanovitsya_float(self):
        self.assertEqual(_normalize(Decimal("1234.56")), 1234.56)
        self.assertIsInstance(_normalize(Decimal("1234.56")), float)

    def test_tseloe_decimal_stanovitsya_int(self):
        """SUM по целочисленной колонке на стенде даёт int — держим так же."""
        self.assertEqual(_normalize(Decimal("42")), 42)
        self.assertIsInstance(_normalize(Decimal("42")), int)
        # «42.00» из NUMERIC(10,2) — то же число, а не 42.0
        self.assertEqual(_normalize(Decimal("42.00")), 42)

    def test_nan_i_beskonechnost_stanovyatsya_none(self):
        """json.dumps пишет NaN литералом, и JSON.parse в браузере падает."""
        for bad in (Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf")):
            self.assertIsNone(_normalize(bad), bad)
        self.assertEqual(dumps(_normalize(float("nan"))), "null")

    def test_ogromnoe_tseloe_uhodit_strokoy(self):
        """За 2^53 браузер молча округлит число — лучше строка, чем ложь."""
        big = 2 ** 53 + 1
        self.assertEqual(_normalize(big), str(big))
        self.assertEqual(_normalize(Decimal(big)), str(big))
        self.assertEqual(_normalize(2 ** 53 - 1), 2 ** 53 - 1)

    def test_daty_uhodyat_strokoy(self):
        self.assertEqual(_normalize(dt.date(2026, 9, 21)), "2026-09-21")
        self.assertEqual(
            _normalize(dt.datetime(2026, 9, 21, 14, 30, 5)), "2026-09-21 14:30:05"
        )
        self.assertEqual(_normalize(dt.time(14, 30, 5)), "14:30:05")

    def test_uuid_i_dvoichnye_dannye(self):
        value = uuid.UUID("0b1d3b8e-0000-4000-8000-000000000001")
        self.assertEqual(_normalize(value), str(value))
        self.assertEqual(_normalize(b"\x00\x01\x02"), "<3 байт>")

    def test_vlozhennye_znacheniya_iz_jsonb(self):
        """Колонка jsonb приезжает готовым dict, но с Decimal внутри."""
        value = {"НТУ": Decimal("10.5"), "дни": [Decimal("1"), dt.date(2026, 1, 1)]}
        self.assertEqual(_normalize(value), {"НТУ": 10.5, "дни": [1, "2026-01-01"]})

    def test_prostye_znacheniya_ne_trogayem(self):
        for value in (None, True, False, "АЗС 58-041", 0, -17, 3.5):
            self.assertEqual(_normalize(value), value)
        # None и False должны остаться собой, а не превратиться в 0
        self.assertIs(_normalize(None), None)
        self.assertIs(_normalize(False), False)


class RowsSurviveJsonTests(unittest.TestCase):
    """То, ради чего всё это: строка ответа проходит сериализацию."""

    def test_stroka_s_vitriny_serializuetsya(self):
        raw = (
            "58-041",
            Decimal("18427364.55"),   # выручка НТУ
            Decimal("0"),             # план не заведён
            dt.date(2026, 9, 1),      # начало периода
            None,
        )
        row = [_normalize(v) for v in raw]
        restored = json.loads(dumps({"rows": [row]}))["rows"][0]
        self.assertEqual(restored, ["58-041", 18427364.55, 0, "2026-09-01", None])

    def test_bez_privedeniya_upalo_by(self):
        """Фиксируем сам дефект: без _normalize json.dumps не проходит."""
        with self.assertRaises(TypeError) as ctx:
            dumps({"rows": [[Decimal("1.5")]]})
        self.assertIn("Decimal", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
