"""ИИ-01. Голосовой ввод: распознавание речи локальной моделью семейства Whisper.

Решение Р-3: только локальный движок на сервере ИИ, аудио не хранится; браузерное
распознавание (Web Speech API) не используется — оно отправляет звук Google или Apple.

Как устроено:
  * браузер записывает голос (MediaRecorder), сам переводит запись в WAV 16 кГц моно
    (Web Audio) и отправляет её телом запроса — ffmpeg на сервере не нужен;
  * здесь WAV разбирается в памяти (модуль wave), на диск звук не пишется; после
    распознавания массив отбрасывается;
  * движок — mlx-whisper на маке с Apple Silicon (видеокарта) или faster-whisper на
    любом сервере (процессор); выбирает AI_STT_ENGINE (auto | mlx | faster | off);
    модель — AI_STT_MODEL (имя на Hugging Face или путь к скачанной папке);
  * словарь предметной области (НТУ, КССС, ВД, РУ, ТМ, ОНПО) подаётся подсказкой, после
    распознавания код приводит аббревиатуры и номера АЗС к виду «АЗС 58-123»;
  * одновременно распознаётся не больше AI_STT_CONCURRENCY записей (по умолчанию одна):
    отдельная очередь, ответы других пользователей модель ИИ не ждут; кто не дождался
    места за AI_STT_WAIT секунд — «распознавание занято»;
  * в журнал (таблица ai_speech) — только текст, длительность и время распознавания.

Функция включается сама, когда движок установлен (scripts/install_speech.sh), и
выключается AI_STT_ENGINE=off — например, до согласования ИБ на продуктивном сервере.
"""
from __future__ import annotations

import importlib.util
import io
import os
import re
import threading
import time
import wave
from dataclasses import dataclass

ENGINE_SETTING = (os.environ.get("AI_STT_ENGINE") or "auto").strip().lower()
MODEL_SETTING = (os.environ.get("AI_STT_MODEL") or "").strip()
DEFAULT_MODELS = {"mlx": "mlx-community/whisper-large-v3-turbo", "faster": "small"}
MAX_SECONDS = float(os.environ.get("AI_STT_MAX_SECONDS", "60"))
MIN_SECONDS = 0.4
SAMPLE_RATE = 16000
MAX_BYTES = int((MAX_SECONDS + 2) * SAMPLE_RATE * 2 * 2) + 4096     # с запасом на стерео и заголовок
CONCURRENCY = max(1, int(os.environ.get("AI_STT_CONCURRENCY", "1")))
WAIT_SECONDS = float(os.environ.get("AI_STT_WAIT", "20"))

# Подсказка модели: как пишутся термины предметной области.
PROMPT = ("Вопрос к аналитике сети АЗС: выручка НТУ, конверсия, средний чек, чеки, литры, ВД, КССС, "
          "РУ, ТМ, ОНПО, АЗС 58-123, план и факт за сентябрь 2026 года.")

# Фразы, которые Whisper «слышит» в тишине и шуме (из субтитров обучающих данных).
HALLUCINATIONS = ("продолжение следует", "субтитры", "редактор субтитров", "спасибо за просмотр",
                  "подписывайтесь на канал", "ставьте лайк", "dimatorzok", "корректор", "до новых встреч")

TERMS = [
    (r"\bэн[\s-]?тэ[\s-]?у\b|\bнту\b", "НТУ"),
    (r"\bка[\s-]?эс[\s-]?эс[\s-]?эс\b|\bкс{2,3}\b", "КССС"),
    (r"\bвэ[\s-]?дэ\b|\bвд\b", "ВД"),
    (r"\bэр[\s-]?у\b|\bру\b", "РУ"),
    (r"\bтэ[\s-]?эм\b|\bтм\b", "ТМ"),
    (r"\bо[\s-]?эн[\s-]?пэ[\s-]?о\b|\bонпо\b", "ОНПО"),
    (r"\bэн[\s-]?пэ[\s-]?о\b|\bнпо\b", "НПО"),
    (r"\bа[\s-]?зэ[\s-]?эс\b|\bазс\b", "АЗС"),
]
_TERMS = [(re.compile(pattern, re.IGNORECASE), value) for pattern, value in TERMS]
# Номер АЗС «58 123» / «58-123» после «АЗС», «№», «по», «на», «станция», «объект», «номер».
_STATION = re.compile(r"(?i)\b(азс|по|на|станци\w*|объект\w*|номер\w*)(\s+№)?\s+(\d{2})[\s\-–—](\d{3})(?!\d)")
_UNITS = re.compile(r"(?i)^\s*(руб|₽|р\.|тыс|млн|л\b|литр|тонн|т\b|%|процент|чек|штук|шт|км|час|мин)")


# Числительные словами → цифры: «пятьдесят восемь сто двадцать три» → «58 123» (дальше — «58-123»).
_UNITS_WORDS = {"ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
                "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
_TEENS_TENS = {"десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
               "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
               "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
               "восемьдесят": 80, "девяносто": 90}
_HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600,
             "семьсот": 700, "восемьсот": 800, "девятьсот": 900}
_THOUSAND = {"тысяча", "тысячи", "тысяч"}
_ORDINAL = re.compile(r"(?i)^(перв|втор|трет|четв[её]рт|пят|шест|седьм|восьм|девят|десят|сот|тысячн)\w*(ого|ому|ым|ом|ой|ая|ое|ый|ий|ые|ых)$")
_TOKEN = re.compile(r"\S+|\s+")


def _order(word: str) -> tuple[int, int] | None:
    if word in _UNITS_WORDS:
        return _UNITS_WORDS[word], 0
    if word in _TEENS_TENS:
        return _TEENS_TENS[word], 1
    if word in _HUNDREDS:
        return _HUNDREDS[word], 2
    return None


def words_to_digits(text: str) -> str:
    """Цепочки из двух и более числительных — цифрами; разряд, который не складывается, начинает новое число."""
    tokens = _TOKEN.findall(text or "")
    out: list[str] = []
    i = 0
    while i < len(tokens):
        word = tokens[i].lower().strip(",.;:!?")
        if _order(word) is None:
            out.append(tokens[i])
            i += 1
            continue
        # Собрать цепочку числительных (через пробелы).
        j, run = i, []
        while j < len(tokens):
            token = tokens[j]
            if token.isspace():
                j += 1
                continue
            bare = token.lower().strip(",.;:!?")
            if _order(bare) is None and bare not in _THOUSAND:
                break
            run.append((j, bare, token))
            j += 1
            if token != token.rstrip(",.;:!?"):
                break                                   # знак препинания закрывает число
        next_word = next((t for t in tokens[j:] if not t.isspace()), "")
        if len(run) < 2 or _ORDINAL.match(next_word.strip(",.;:!?")):
            out.append(tokens[i])
            i += 1
            continue
        numbers, current, last, base = [], 0, 9, 0
        for _index, bare, _token in run:
            if bare in _THOUSAND:
                base += (current or 1) * 1000
                current, last = 0, 3
                continue
            value, order = _order(bare)
            if order >= last and (current or base):
                numbers.append(base + current)
                current, base = 0, 0
            current += value
            last = order
        numbers.append(base + current)
        tail = run[-1][2][len(run[-1][2].rstrip(",.;:!?")):]
        out.append(" ".join(str(n) for n in numbers) + tail)
        # пробел после цепочки сохраняем, если он был
        i = run[-1][0] + 1
    return "".join(out)


class SpeechError(Exception):
    """Ошибка, о которой человеку сообщается как есть."""

    status = 400


class Unavailable(SpeechError):
    status = 503


class Busy(SpeechError):
    status = 429


class TooLong(SpeechError):
    status = 413


@dataclass
class Result:
    text: str
    seconds: float
    ms: int
    engine: str
    empty_reason: str = ""


# --- движок ------------------------------------------------------------------------

def engine() -> str:
    """Какой движок будет работать: mlx, faster или пустая строка (распознавания нет)."""
    if ENGINE_SETTING in {"off", "0", "no", "false"}:
        return ""
    order = ("mlx", "faster") if ENGINE_SETTING == "auto" else (ENGINE_SETTING,)
    modules = {"mlx": "mlx_whisper", "faster": "faster_whisper"}
    for name in order:
        module = modules.get(name)
        if module and importlib.util.find_spec(module) is not None:
            return name
    return ""


def model_name(kind: str | None = None) -> str:
    kind = kind or engine()
    return MODEL_SETTING or DEFAULT_MODELS.get(kind, "")


SETUP_HINT = ("Голосовой ввод ещё не установлен на сервере ИИ. На маке в папке проекта выполните "
              "./scripts/install_speech.sh (модель ≈1,6 ГБ скачается один раз) и перезапустите приложение "
              "(npm start). Пользователи увидят микрофон после установки.")
OFF_HINT = "Голосовой ввод выключен на сервере ИИ (AI_STT_ENGINE=off) — например, до согласования ИБ."


def status(admin: bool = False) -> dict:
    """Для /api/ai/status: есть ли голосовой ввод и его пределы.

    Пока движка нет, администратор получает `setup` — как его установить (кнопка у поля видна
    только ему); остальные пользователи кнопку не видят.
    """
    kind = engine()
    out = {"available": bool(kind), "engine": kind, "maxSeconds": int(MAX_SECONDS), "sampleRate": SAMPLE_RATE}
    if not kind and admin:
        out["setup"] = OFF_HINT if ENGINE_SETTING in {"off", "0", "no", "false"} else SETUP_HINT
    return out


_slots = threading.BoundedSemaphore(CONCURRENCY)
_models: dict = {}
_load_lock = threading.Lock()


def _faster_model(name: str):
    with _load_lock:
        if name not in _models:
            from faster_whisper import WhisperModel

            _models[name] = WhisperModel(name, device="auto", compute_type="int8")
        return _models[name]


def _run_engine(kind: str, audio) -> str:
    """Распознать массив float32 16 кГц моно. Возвращает сырой текст модели."""
    name = model_name(kind)
    if kind == "mlx":
        import mlx_whisper

        result = mlx_whisper.transcribe(audio, path_or_hf_repo=name, language="ru", initial_prompt=PROMPT,
                                        temperature=0.0, condition_on_previous_text=False, verbose=None)
        return str(result.get("text") or "")
    if kind == "faster":
        model = _faster_model(name)
        segments, _info = model.transcribe(audio, language="ru", initial_prompt=PROMPT, beam_size=1,
                                           temperature=0.0, condition_on_previous_text=False, vad_filter=True)
        return " ".join(segment.text for segment in segments)
    raise Unavailable("Голосовой ввод не установлен на сервере ИИ")


# Точка подмены для тестов: функция (kind, audio) -> текст.
RUNNER = _run_engine


# --- звук ----------------------------------------------------------------------------

def decode_wav(data: bytes):
    """WAV (PCM 16 бит) → массив float32 16 кГц моно и длительность. Всё — в памяти."""
    import numpy as np

    if len(data) > MAX_BYTES:
        raise TooLong(f"Запись длиннее {int(MAX_SECONDS)} секунд")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
            raw = wav.readframes(frames)
    except (wave.Error, EOFError) as err:
        raise SpeechError("Запись не читается: ожидался WAV") from err
    if width != 2 or channels not in (1, 2) or not 8000 <= rate <= 48000:
        raise SpeechError("Запись не читается: ожидался WAV PCM 16 бит, 8–48 кГц")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    seconds = len(audio) / float(rate)
    if seconds > MAX_SECONDS + 1:
        raise TooLong(f"Запись длиннее {int(MAX_SECONDS)} секунд")
    if seconds < MIN_SECONDS:
        raise SpeechError("Запись слишком короткая — скажите вопрос целиком")
    if rate != SAMPLE_RATE:
        target = int(round(seconds * SAMPLE_RATE))
        audio = np.interp(np.linspace(0, len(audio) - 1, target), np.arange(len(audio)), audio).astype(np.float32)
    return audio, seconds


def is_silence(audio) -> bool:
    import numpy as np

    if audio.size == 0:
        return True
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(np.square(audio))))
    return peak < 0.02 or rms < 0.002


# --- текст ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Текст модели → текст для поля вопроса: термины, номера АЗС, без «галлюцинаций» тишины."""
    clean = " ".join((text or "").split())
    lowered = clean.lower().strip(" .!?…")
    if not lowered or any(marker in lowered for marker in HALLUCINATIONS) and len(lowered) < 80:
        return ""
    clean = words_to_digits(clean)
    for pattern, value in _TERMS:
        clean = pattern.sub(value, clean)

    def station(match: re.Match) -> str:
        rest = match.string[match.end():]
        if _UNITS.match(rest):
            return match.group(0)                      # «по 12 400 рублей» — это сумма, не номер
        word = match.group(1)
        number = f"{match.group(3)}-{match.group(4)}"
        return f"{word}{match.group(2) or ''} {number}"

    clean = _STATION.sub(station, clean)
    clean = clean.strip()
    return clean[:1].upper() + clean[1:]


# --- распознавание -------------------------------------------------------------------

def transcribe_wav(data: bytes) -> Result:
    """Распознать запись. Звук живёт только в памяти этого вызова."""
    kind = engine()
    if not kind:
        raise Unavailable("Голосовой ввод не установлен на сервере ИИ")
    audio, seconds = decode_wav(data)
    del data
    if is_silence(audio):
        return Result(text="", seconds=round(seconds, 1), ms=0, engine=kind,
                      empty_reason="Не слышно речи — проверьте микрофон и скажите вопрос ещё раз")
    if not _slots.acquire(timeout=WAIT_SECONDS):
        raise Busy("Распознавание занято — попробуйте через несколько секунд")
    started = time.monotonic()
    try:
        raw = RUNNER(kind, audio)
    except SpeechError:
        raise
    except Exception as err:  # noqa: BLE001 - причина — в журнале сервера, человеку — понятное сообщение
        raise SpeechError(f"Не удалось распознать речь: {type(err).__name__}") from err
    finally:
        _slots.release()
        del audio
    text = normalize(raw)
    return Result(text=text, seconds=round(seconds, 1), ms=int((time.monotonic() - started) * 1000), engine=kind,
                  empty_reason="" if text else "Речь не распознана — скажите вопрос ещё раз, ближе к микрофону")


# --- журнал --------------------------------------------------------------------------

SPEECH_DDL = """
CREATE TABLE IF NOT EXISTS ai_speech (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    actor      TEXT,
    role       TEXT,
    seconds    REAL,
    ms         INTEGER,
    engine     TEXT,
    text       TEXT,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_speech_created ON ai_speech(created_at);
"""


def log(*, actor: str, role: str, result: Result | None = None, error: str = "") -> None:
    """Строка журнала: кто, сколько секунд, за сколько распознано и какой текст. Звука нет."""
    from . import journal

    try:
        conn = journal._connect()
        try:
            conn.executescript(SPEECH_DDL)
            conn.execute("INSERT INTO ai_speech (created_at, actor, role, seconds, ms, engine, text, error) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (int(time.time()), actor, role, result.seconds if result else None,
                          result.ms if result else None, result.engine if result else engine(),
                          (result.text if result else "")[:1000], error[:300]))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - журнал не мешает вводу
        pass
