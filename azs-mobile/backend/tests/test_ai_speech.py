"""ИИ-01: голосовой ввод — разбор записи, нормализация текста, очередь, журнал, API.

Модели распознавания здесь нет: движок подменён функцией. Проверяется, что звук
разбирается в памяти, тишина не уходит в модель, термины и номера АЗС приводятся к
принятому виду, очередь не пускает лишних, в журнале только текст и длительность.
"""
from __future__ import annotations

import io
import math
import os
import pathlib
import sqlite3
import struct
import tempfile
import threading
import unittest
import wave

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ai import api as ai_api
from backend.ai import dialogs as store
from backend.ai import journal, speech


def wav_bytes(seconds: float = 1.5, rate: int = 16000, channels: int = 1, amplitude: float = 0.3) -> bytes:
    frames = int(seconds * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        samples = []
        for i in range(frames):
            value = int(amplitude * 32767 * math.sin(2 * math.pi * 220 * i / rate))
            samples.extend([value] * channels)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


class NormalizeTests(unittest.TestCase):
    def test_owner_example(self):
        self.assertEqual(speech.normalize("выручка нту по пятьдесят восемь сто двадцать три за август"),
                         "Выручка НТУ по 58-123 за август")

    def test_terms(self):
        self.assertEqual(speech.normalize("сравни ру и тм по онпо юг, кссс и вд"), "Сравни РУ и ТМ по ОНПО юг, КССС и ВД")
        self.assertEqual(speech.normalize("выручка эн тэ у на а зэ эс"), "Выручка НТУ на АЗС")

    def test_station_numbers_but_not_amounts(self):
        self.assertEqual(speech.normalize("АЗС № 58 140 конверсия"), "АЗС № 58-140 конверсия")
        self.assertEqual(speech.normalize("средний чек по 12 400 рублей"), "Средний чек по 12 400 рублей")
        self.assertEqual(speech.normalize("КССС 10005"), "КССС 10005")      # код КССС — не номер на вывеске

    def test_numbers_in_words(self):
        self.assertEqual(speech.normalize("за сентябрь две тысячи двадцать шесть"), "За сентябрь 2026")
        self.assertEqual(speech.normalize("двадцать пятое сентября"), "Двадцать пятое сентября")   # порядковые не трогаем
        self.assertEqual(speech.normalize("три АЗС"), "Три АЗС")                                   # одно слово — словом

    def test_silence_phrases_are_dropped(self):
        self.assertEqual(speech.normalize("Продолжение следует..."), "")
        self.assertEqual(speech.normalize("Субтитры сделал DimaTorzok"), "")


class AudioTests(unittest.TestCase):
    def setUp(self):
        self._saved = (speech.engine, speech.RUNNER, speech._slots, speech.WAIT_SECONDS)
        speech.engine = lambda: "fake"
        self.calls = []

        def runner(kind, audio):
            self.calls.append((kind, len(audio)))
            return "выручка нту по пятьдесят восемь сто двадцать три"
        speech.RUNNER = runner

    def tearDown(self):
        speech.engine, speech.RUNNER, speech._slots, speech.WAIT_SECONDS = self._saved

    def test_wav_is_read_in_memory_and_resampled(self):
        audio, seconds = speech.decode_wav(wav_bytes(2.0, rate=48000, channels=2))
        self.assertAlmostEqual(seconds, 2.0, places=2)
        self.assertEqual(len(audio), 32000)                   # 16 кГц моно

    def test_limits(self):
        with self.assertRaises(speech.TooLong):
            speech.decode_wav(wav_bytes(speech.MAX_SECONDS + 3, rate=8000))
        with self.assertRaises(speech.SpeechError):
            speech.decode_wav(wav_bytes(0.1))
        with self.assertRaises(speech.SpeechError):
            speech.decode_wav(b"not a wav at all")

    def test_transcribe(self):
        result = speech.transcribe_wav(wav_bytes())
        self.assertEqual(result.text, "Выручка НТУ по 58-123")
        self.assertEqual(result.seconds, 1.5)
        self.assertEqual(self.calls, [("fake", 24000)])

    def test_silence_does_not_reach_the_model(self):
        result = speech.transcribe_wav(wav_bytes(amplitude=0.0))
        self.assertEqual(result.text, "")
        self.assertIn("Не слышно речи", result.empty_reason)
        self.assertEqual(self.calls, [])

    def test_queue_limits_concurrent_jobs(self):
        speech._slots = threading.BoundedSemaphore(1)
        speech._slots.acquire()                               # место занято другой записью
        speech.WAIT_SECONDS = 0.05
        with self.assertRaises(speech.Busy):
            speech.transcribe_wav(wav_bytes())

    def test_no_engine(self):
        speech.engine = lambda: ""
        with self.assertRaises(speech.Unavailable):
            speech.transcribe_wav(wav_bytes())


class _User:
    def __init__(self, uid, role):
        self.id, self.role, self.isAdmin = uid, role, False
        self.email = f"user{uid}@example.com"
        self.name, self.roleTitle, self.roleBinding, self.scopeLabel = "Тест", "", "", ""
        self.aiDialog = True


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (journal.JOURNAL_DB, store.JOURNAL_DB, os.environ.get("AI_DEMO_ENABLED"),
                       speech.engine, speech.RUNNER)
        os.environ["AI_DEMO_ENABLED"] = "1"
        journal.JOURNAL_DB = store.JOURNAL_DB = pathlib.Path(self._tmp.name) / "journal.db"
        speech.engine = lambda: "fake"
        speech.RUNNER = lambda kind, audio: "конверсия по азс пятьдесят восемь сто сорок"
        self.current = _User(7, "aup_npo")
        app = FastAPI()
        app.include_router(ai_api.build_router(lambda: self.current, lambda: self.current))
        self.client = TestClient(app)

    def tearDown(self):
        journal.JOURNAL_DB, store.JOURNAL_DB, flag, speech.engine, speech.RUNNER = self._saved
        if flag is None:
            os.environ.pop("AI_DEMO_ENABLED", None)
        else:
            os.environ["AI_DEMO_ENABLED"] = flag
        self._tmp.cleanup()

    def post(self, data: bytes):
        return self.client.post("/api/ai/speech", content=data, headers={"Content-Type": "audio/wav"})

    def test_text_comes_back_and_journal_keeps_only_text(self):
        response = self.post(wav_bytes())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "Конверсия по АЗС 58-140")
        conn = sqlite3.connect(journal.JOURNAL_DB)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(ai_speech)")]
            row = conn.execute("SELECT actor, seconds, text FROM ai_speech").fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("user7@example.com", 1.5, "Конверсия по АЗС 58-140"))
        self.assertFalse(any("audio" in c or "wav" in c for c in columns))     # звука в журнале нет

    def test_status_tells_the_interface(self):
        speech_status = speech.status()
        self.assertIn("maxSeconds", speech_status)
        self.assertTrue(speech_status["available"])
        self.assertNotIn("setup", speech_status)

    def test_without_engine_only_admin_sees_how_to_install(self):
        speech.engine = lambda: ""
        self.assertNotIn("setup", speech.status())                         # пользователю — кнопки нет
        self.assertIn("install_speech.sh", speech.status(admin=True)["setup"])

    def test_errors(self):
        self.assertEqual(self.post(b"xx").status_code, 400)
        big = b"\0" * (speech.MAX_BYTES + 10)
        self.assertEqual(self.post(big).status_code, 413)
        speech.engine = lambda: ""
        response = self.post(wav_bytes())
        self.assertEqual(response.status_code, 503)
        self.assertIn("не установлен", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
