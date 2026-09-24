"""Текст справки от ИИ (СП-06) и сборщик на маке: сверка чисел, откат к шаблону, повторная проверка на сервере.

Модель здесь не вызывается: ответ модели — готовая строка JSON, как её вернул бы
сборщик на маке. Стенд — синтетическая витрина ОХД из report_stand (36 объектов,
7005 — падение топлива на 40 %, 7007 — три дня без продаж).
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import tempfile
import unittest
import urllib.error
from datetime import date, datetime
from unittest import mock

import openpyxl

from backend.reports import narrative
from backend.reports.publish import MSK
from backend.tests.test_reports import StandMixin

GOOD = {
    "headline": [
        "Реализация топлива за неделю 14–20 сентября 2026 — 1 221 980 т: −1,7 % к прошлой неделе "
        "и −1,8 % к той же неделе прошлого года.",
        "Выручка НТУ — 3,2 млн ₽, −1,7 % к прошлой неделе.",
        "Лучшая динамика топлива у ОНПО «Центр» (0,0 %), слабее всех — ОНПО «Север» (−2,7 %).",
        "В зоне внимания 2 объекта.",
    ],
    "attention": [
        "АЗС № 10007 (ОНПО «Север») потеряла 41,6 % топлива и 3 дня не продавала.",
        "АЗС № 10005 (ОНПО «Юг») — −40,0 % к прошлой неделе.",
    ],
    "recommendations": [
        {"action": "Стоит проверить работу АЗС № 10007 в дни без продаж.",
         "basis": "3 дня без продаж и −41,6 % топлива к прошлой неделе.",
         "effect": "Станет понятно, нужен ли ремонт оборудования.",
         "limits": "Не подходит, если объект закрыт по плану."},
        {"action": "Можно запустить акцию со скидкой на топливо на АЗС № 10005.",
         "basis": "−40,0 % топлива к прошлой неделе.", "effect": "Вернуть клиентов.", "limits": "Нет ограничений."},
    ],
}


def raw(**changes) -> str:
    data = copy.deepcopy(GOOD)
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


class NarrativeTests(StandMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.model = self.build()

    def test_good_text_passes_and_promo_is_withheld(self):
        text = narrative.apply(self.model, raw(), model_name="qwen-test", duration_ms=900)
        self.assertEqual(text["source"], narrative.AI, text.get("reason"))
        self.assertEqual(len(text["headline"]), 4)
        self.assertEqual([r["action"][:14] for r in text["recommendations"]], ["Стоит проверит"])
        self.assertTrue(any("Акции" in w["reason"] for w in text["withheld"]))
        self.assertNotEqual(text["recommendations"][0]["confidence"], "высокая")

    def test_every_number_must_come_from_the_issue(self):
        cases = {
            "выдуманное число": ["Реализация топлива — 1 300 000 л.", "Выручка НТУ — 3,2 млн ₽."],
            "процент как литры": ["Реализация топлива снизилась на 1,7 л.", "Выручка НТУ — 3,2 млн ₽."],
            "посчитанная разность": ["Топливо снизилось на 29 020 л.", "Выручка НТУ — 3,2 млн ₽."],
            "малый выдуманный процент": ["ОНПО «Центр» прибавило 0,4 %.", "Выручка НТУ — 3,2 млн ₽."],
        }
        for name, headline in cases.items():
            with self.subTest(name):
                text = narrative.apply(self.model, raw(headline=headline))
                self.assertEqual(text["source"], narrative.TEMPLATE)
                self.assertIn("числа не из выпуска", text["reason"])
                self.assertEqual(narrative.headline(dict(self.model, narrative=text)), self.model["headline"])

    def test_causes_unknown_names_and_broken_json_fall_back_to_template(self):
        cases = {
            "причина": raw(headline=["Топливо — 1 221 980 л, вероятно из-за погоды.", "Выручка НТУ — 3,2 млн ₽."]),
            "чужое ОНПО": raw(headline=["ОНПО «Восток» — −1,7 %.", "Выручка НТУ — 3,2 млн ₽."]),
            "не JSON": "Вот текст справки",
            "одна фраза": raw(headline=["Топливо — 1 221 980 л."]),
        }
        for name, content in cases.items():
            with self.subTest(name):
                self.assertEqual(narrative.apply(self.model, content)["source"], narrative.TEMPLATE)

    def test_dates_and_rounded_sums_are_allowed(self):
        headline = ["Неделя 14.09–20.09.2026: топливо 1,22 млн л.", "Выручка всего — 76,5 млн ₽, выручка НТУ — 3 207 698 ₽."]
        self.assertEqual(narrative.apply(self.model, raw(headline=headline))["source"], narrative.AI)

    def test_no_recommendations_without_attention_zone(self):
        quiet = copy.deepcopy(self.model)
        quiet["attention"].update(drops=[], dropsTotal=0, dropKeys=[], noSales=[])
        text = narrative.apply(quiet, raw(attention=[], headline=GOOD["headline"][:3]))
        self.assertEqual(text["source"], narrative.AI, text.get("reason"))
        self.assertEqual(text["recommendations"], [])

    def test_task_carries_only_the_issue_summary(self):
        task = narrative.task(self.model)
        user = task["messages"][1]["content"]
        self.assertIn("14–20 сентября 2026", user)
        self.assertIn("АЗС № 10007", user)
        self.assertNotIn("SELECT", user)
        self.assertEqual(task["format"]["required"], ["headline", "attention", "recommendations"])
        self.assertEqual(narrative.digest(self.model), narrative.digest(self.build()))


class ServerRecheckTests(StandMixin, unittest.TestCase):
    """Сервер не верит присланному тексту: сверяет его ещё раз."""

    def setUp(self):
        super().setUp()
        self.model = self.build()

    def test_honest_text_survives_and_tampered_text_becomes_template(self):
        text = narrative.apply(self.model, raw(), model_name="qwen-test")
        self.assertEqual(narrative.recheck(dict(self.model, narrative=text))["source"], narrative.AI)
        tampered = dict(text, headline=["Реализация топлива — 1 300 000 т.", "Выручка НТУ — 3,2 млн ₽."])
        checked = narrative.recheck(dict(self.model, narrative=tampered))
        self.assertEqual(checked["source"], narrative.TEMPLATE)
        self.assertIn("не прошёл проверку на сервере", checked["reason"])

    def test_template_headline_with_plan_passes_the_same_check(self):
        """Шаблонные фразы (с планом месяца) — эталон честного текста: сверка их не отклоняет."""
        content = json.dumps({"headline": self.model["headline"], "attention": [], "recommendations": []},
                             ensure_ascii=False)
        text = narrative.apply(self.model, content)
        self.assertEqual(text["source"], narrative.AI, text.get("reason"))
        self.assertIn("План сентября 2026", " ".join(self.model["headline"]))
        self.assertIn("план месяца", narrative.task(self.model)["messages"][1]["content"])


class FakeServer:
    """Сайт для сборщика: down — не отвечает, reject — отклоняет выпуск по существу."""

    def __init__(self, published=False, request=None, down="", reject=""):
        self.published, self.request, self.down, self.reject = published, request, down, reject
        self.calls, self.models = [], []

    def _fail(self):
        from backend.reports.publish import ServerUnavailable

        if self.down:
            raise ServerUnavailable(self.down)

    def status(self, week):
        self.calls.append(("status", week))
        self._fail()
        return {"published": self.published, "request": self.request}

    def publish(self, model, request_id=None):
        from backend.reports.publish import ServerRejected

        self.calls.append(("publish", model["week"]["iso"], request_id, model["narrative"]["source"]))
        self._fail()
        if self.reject:
            raise ServerRejected(self.reject)
        self.models.append(model)
        self.published = True
        return {"outcome": "published", "issue": {"version": 2 if request_id else 1}}

    def not_formed(self, week, reason, request_id=None):
        self.calls.append(("not_formed", week, reason, request_id))
        self._fail()
        return {"outcome": "not_formed"}


MONDAY_MORNING = datetime(2026, 9, 28, 10, 0, tzinfo=MSK)


class PublishTests(StandMixin, unittest.TestCase):
    """Сборщик на маке: очередь, вопрос к сайту, сборка по витрине, текст, копия у владельца, отправка."""

    def setUp(self):
        super().setUp()
        from backend.reports import publish

        self.outbox = publish.Outbox(self.folder / "Справки АЗС")
        self.notes, self.builds = [], []

    def run_publish(self, server, ask=None, week="2026-W38", now=MONDAY_MORNING):
        from backend.reports import publish

        def build(w):
            self.builds.append(w.iso)
            return self.build(week=w)
        lines = []
        results = publish.run(server, week=week, build=build, now=now, log=lines.append, outbox=self.outbox,
                              notify=self.notes.append,
                              ask=ask or (lambda task: (raw(), "qwen-test", 1500)))
        return results, lines

    def test_unpublished_week_is_built_written_and_sent(self):
        server = FakeServer()
        results, _ = self.run_publish(server)
        self.assertEqual(results[0]["outcome"], "published")
        self.assertEqual(server.calls[-1], ("publish", "2026-W38", None, "ai"))

    def test_published_week_is_not_rebuilt(self):
        server = FakeServer(published=True)
        results, _ = self.run_publish(server)
        self.assertEqual(results, [{"outcome": "exists", "week": "2026-W38"}])
        self.assertEqual([c[0] for c in server.calls], ["status"])
        self.assertEqual(self.builds, [])

    def test_reissue_request_is_built_with_its_number(self):
        server = FakeServer(published=True, request={"id": 5, "week": "2026-W38", "reason": "правка"})
        self.run_publish(server)
        self.assertEqual(server.calls[-1], ("publish", "2026-W38", 5, "ai"))

    def test_model_down_gives_template_and_missing_data_is_reported(self):
        def down(task):
            raise OSError("Connection refused")
        server = FakeServer()
        self.run_publish(server, ask=down)
        self.assertEqual(server.calls[-1][-1], "template")
        server = FakeServer()
        results, _ = self.run_publish(server, week="2026-W39")
        self.assertEqual(results[0]["outcome"], "not_formed")
        self.assertIn("Нет данных за 27.09.2026", server.calls[-1][2])

    def test_rejected_text_is_retried_once_then_template(self):
        answers = iter([raw(headline=["Топливо — 1 300 000 т.", "Выручка НТУ — 3,2 млн ₽."]), raw()])
        temperatures = []

        def ask(task):
            temperatures.append(task["options"]["temperature"])
            return next(answers), "qwen-test", 100
        server = FakeServer()
        self.run_publish(server, ask=ask)
        self.assertEqual(temperatures, [0, 0.3])
        self.assertEqual(server.calls[-1][-1], "ai")

    def test_copy_is_kept_and_queue_is_empty_after_publishing(self):
        server = FakeServer()
        self.run_publish(server)
        folder = self.outbox.root / "2026-W38 (14–20 сентября 2026)"
        self.assertTrue((folder / "Справка_сеть_2026-W38.pdf").stat().st_size > 10_000)
        book = openpyxl.load_workbook(folder / "Справка_сеть_2026-W38.xlsx")
        cells = [str(c.value) for row in book.worksheets[0].iter_rows() for c in row if c.value is not None]
        self.assertTrue(any("копия с компьютера, до публикации" in c for c in cells))
        self.assertNotIn("localCopy", server.models[-1])  # пометка копии на сайт не уходит
        self.assertEqual(self.outbox.items(), [])
        self.assertTrue(self.outbox.week_state("2026-W38")["sentAt"])
        self.assertEqual(self.notes, ["Справка за 14–20 сентября 2026 опубликована в приложении, версия 1."])
        # Повторная сборка той же недели не затирает прежнюю копию.
        server.published, server.request = True, {"id": 7, "week": "2026-W38", "reason": "правка"}
        self.run_publish(server)
        self.assertTrue((folder / "Справка_сеть_2026-W38_2.pdf").exists())
        self.assertTrue((folder / "Справка_сеть_2026-W38.pdf").exists())

    def test_app_down_keeps_copy_and_queue_without_rebuilding(self):
        server = FakeServer(down="сайт azs-classifier.ru не отвечает (timed out)")
        results, _ = self.run_publish(server)
        self.assertEqual(results, [{"outcome": "queued", "week": "2026-W38", "reason": server.down}])
        self.assertEqual(len(self.outbox.items()), 1)
        self.assertEqual(len(list(self.outbox.root.glob("2026-W38 */Справка_сеть_2026-W38.pdf"))), 1)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("в приложение не ушла", self.notes[0])
        # Час спустя приложение всё ещё недоступно: из ОХД заново не собираем и повторно не уведомляем.
        results, _ = self.run_publish(server)
        self.assertEqual(results[-1]["outcome"], "queued")
        self.assertEqual(self.builds, ["2026-W38"])
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.outbox.items()[0]["attempts"], 2)
        # Приложение вернулось: уходит выпуск из очереди, сборки нет.
        server.down = ""
        results, _ = self.run_publish(server)
        self.assertEqual(results[0]["outcome"], "published")
        self.assertEqual(results[-1], {"outcome": "exists", "week": "2026-W38"})
        self.assertEqual(self.builds, ["2026-W38"])
        self.assertEqual(self.outbox.items(), [])
        self.assertIn("опубликована", self.notes[-1])

    def test_rejected_issue_is_set_aside(self):
        server = FakeServer(reject="сайт отклонил: в модели нет разделов: metrics")
        results, _ = self.run_publish(server)
        self.assertEqual(results[0]["outcome"], "rejected")
        self.assertEqual(self.outbox.items(), [])
        self.assertEqual(len(self.outbox.rejected_items()), 1)
        self.assertIn("сайт отклонил", self.notes[-1])

    def test_missing_data_is_alerted_only_after_monday_deadline(self):
        server = FakeServer()
        self.run_publish(server, week="2026-W39")
        self.assertEqual(self.notes, [])  # понедельник 10:00 — данные за воскресенье ещё могут прийти
        self.run_publish(server, week="2026-W39", now=datetime(2026, 9, 28, 14, 0, tzinfo=MSK))
        self.run_publish(server, week="2026-W39", now=datetime(2026, 9, 28, 15, 0, tzinfo=MSK))
        self.assertEqual(len(self.notes), 1)
        self.assertIn("Справка за 21–27 сентября 2026 не собрана: Нет данных за 27.09.2026", self.notes[0])

    def test_dry_run_saves_and_sends_nothing(self):
        from backend.reports import publish

        results = publish.run(None, week="2026-W38", dry_run=True, build=lambda w: self.build(week=w),
                              ask=lambda task: (raw(), "qwen-test", 1), log=lambda line: None)
        self.assertEqual(results[0]["outcome"], "dry_run")
        self.assertFalse(self.outbox.root.exists())


class ServerAnswerTests(unittest.TestCase):
    """Ответы сайта делятся на «подождать» (выпуск остаётся в очереди) и «отклонён» (повтор не поможет)."""

    def test_answers_are_sorted_into_wait_and_reject(self):
        from backend.reports import publish

        server = publish.Server("https://example.test", "token")
        cases = [(publish.HttpError(404, "Not Found"), publish.ServerUnavailable, "нужен деплой"),
                 (publish.HttpError(401, "bad token"), publish.ServerUnavailable, "токен"),
                 (publish.HttpError(503, '{"detail":"Reports import token is not configured"}'),
                  publish.ServerUnavailable, "не задан токен"),
                 (publish.HttpError(502, "Bad Gateway"), publish.ServerUnavailable, "502"),
                 (publish.HttpError(400, "в модели нет разделов"), publish.ServerRejected, "нет разделов"),
                 (urllib.error.URLError("timed out"), publish.ServerUnavailable, "example.test не отвечает"),
                 (ConnectionResetError("reset"), publish.ServerUnavailable, "не отвечает"),
                 (json.JSONDecodeError("x", "<html>", 0), publish.ServerUnavailable, "старая версия")]
        for raised, expected, words in cases:
            with self.subTest(raised=repr(raised)), mock.patch.object(publish, "_post", side_effect=raised):
                with self.assertRaises(expected) as caught:
                    server.status("2026-W38")
                self.assertIn(words, str(caught.exception))

    def test_local_app_gets_local_advice(self):
        from backend.reports import publish

        server = publish.Server("http://127.0.0.1:8000", "token")
        cases = [(publish.HttpError(404, "Not Found"), "в запущенном приложении нет приёма справок — перезапустите"),
                 (publish.HttpError(403, "invalid"), "приложение не приняло токен справок — перезапустите"),
                 (urllib.error.URLError("[Errno 61] Connection refused"),
                  "приложение 127.0.0.1:8000 не запущено ([Errno 61] Connection refused) — запустите его: npm start")]
        for raised, words in cases:
            with self.subTest(raised=repr(raised)), mock.patch.object(publish, "_post", side_effect=raised):
                with self.assertRaises(publish.ServerUnavailable) as caught:
                    server.publish({}, None)
                self.assertIn(words, str(caught.exception))
                self.assertNotIn("deploy", str(caught.exception))


class CheckTests(unittest.TestCase):
    """publish --check: по каждому звену — работает или нет и что сделать."""

    def setUp(self):
        from backend.reports import publish

        self._tmp = tempfile.TemporaryDirectory()
        self.outbox = publish.Outbox(pathlib.Path(self._tmp.name) / "Справки АЗС")

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, server, latest=lambda today: date(2026, 9, 27), models=lambda host: ["qwen3:8b"]):
        from backend.reports import publish

        lines = []
        env = {"REPORTS_IMPORT_TOKEN": "t", "REPORTS_IMPORT_URL": "https://example.test", "AI_MODEL": "qwen3:8b",
               "AI_OLLAMA_HOST": "http://127.0.0.1:11434"}
        with mock.patch.dict(os.environ, env):
            code = publish.check(server=server, outbox=self.outbox, latest=latest, models=models,
                                 schedule=lambda: None, now=datetime(2026, 9, 28, 14, 0, tzinfo=MSK),
                                 log=lines.append)
        return code, "\n".join(lines)

    def test_everything_works(self):
        code, text = self.check(FakeServer())
        self.assertEqual(code, 0)
        self.assertIn("✓ Витрина ОХД: доступна; последний день — 27.09.2026", text)
        self.assertIn("✓ Локальная модель: Ollama отвечает, модель qwen3:8b есть", text)
        self.assertIn("✓ Приложение: example.test принимает справки, токен принят; справка за 21–27 сентября 2026 "
                      "ещё не опубликована", text)
        self.assertIn("✓ Очередь: пуста", text)
        self.assertIn("Итог: справка соберётся и уйдёт в приложение по расписанию.", text)

    def test_each_problem_is_named_with_a_way_out(self):
        def no_vpn(today):
            raise OSError("could not connect to server: timed out")
        code, text = self.check(FakeServer(down="сайт example.test не отвечает (timed out)"), latest=no_vpn,
                                models=lambda host: [])
        self.assertEqual(code, 1)
        self.assertIn("✗ Витрина ОХД: нет доступа — could not connect to server: timed out. Нужен подключённый VPN", text)
        self.assertIn("! Локальная модель: в Ollama нет модели qwen3:8b: ollama pull qwen3:8b", text)
        self.assertIn("✗ Приложение: сайт example.test не отвечает (timed out). Если включён корпоративный VPN", text)
        self.assertIn("Итог: недоступны и витрина, и приложение.", text)

    def test_queue_and_late_data_are_shown(self):
        self.outbox.put({"week": {"iso": "2026-W38", "label": "14–20 сентября 2026"}}, None)
        code, text = self.check(FakeServer(down="на сайте ещё нет приёма справок — нужен деплой: bash deploy/deploy.sh"),
                                latest=lambda today: date(2026, 9, 26))
        self.assertEqual(code, 1)
        self.assertIn("! Витрина ОХД: доступна; последний день — 26.09.2026. Данных за 27.09.2026 ещё нет", text)
        self.assertIn("! Очередь: ждут отправки 1: 14–20 сентября 2026", text)
        self.assertIn("Итог: справка соберётся и сохранится", text)


if __name__ == "__main__":
    unittest.main()
