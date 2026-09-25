"""Генерация SQL локальной моделью.

Модель подключается через Ollama по HTTP. Это недоверенный генератор:
всё, что он вернёт, проходит через валидатор, и только потом исполняется.
Смена модели — смена значения AI_MODEL, остальной контур не меняется.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from contextvars import ContextVar
from dataclasses import dataclass

from .contract import NARRATION_SYSTEM, narration_prompt, system_prompt, user_prompt

OLLAMA_HOST = os.environ.get("AI_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("AI_MODEL", "qwen3:8b")
# Рассуждающие модели (Qwen3 и подобные) по умолчанию печатают ход мысли.
# Для генерации SQL он не нужен и только удлиняет ответ.
THINKING = (os.environ.get("AI_MODEL_THINK") or "0").strip().lower() in {"1", "true", "yes", "on"}
TIMEOUT_S = float(os.environ.get("AI_MODEL_TIMEOUT", "120"))
# Один размер контекста для всех вызовов модели. При разном num_ctx Ollama
# перезагружает модель на каждом переключении «Лёгкий» ↔ агент: это лишние
# секунды и пик памяти, при нехватке которой Ollama отвечает ошибкой 500.
# 25.09.2026: 16 384 → 32 768. Агенту на «Среднем» и «Высоком» 16 тыс. токенов
# не хватало — старые результаты приходилось сжимать почти на каждом шаге, и
# модель перечитывала контекст заново. Цена — несколько ГБ памяти под контекст.
NUM_CTX = int(os.environ.get("AI_NUM_CTX") or os.environ.get("AI_AGENT_NUM_CTX") or "32768")
# Сколько Ollama держит модель в памяти после последнего вопроса (по умолчанию у
# Ollama — 5 мин, потом крупная модель грузится заново десятки секунд).
# «24h», «30m», число секунд; «-1» — не выгружать, пока Ollama работает.
KEEP_ALIVE_RAW = (os.environ.get("AI_KEEP_ALIVE") or "24h").strip()
KEEP_ALIVE: str | int = int(KEEP_ALIVE_RAW) if KEEP_ALIVE_RAW.lstrip("-").isdigit() else KEEP_ALIVE_RAW
# Пояснение текстом можно отключить, если нужен только голый результат.
NARRATE = (os.environ.get("AI_NARRATE") or "1").strip().lower() in {"1", "true", "yes", "on"}
# Сколько строк результата показывать модели при составлении пояснения.
NARRATE_MAX_ROWS = int(os.environ.get("AI_NARRATE_MAX_ROWS", "30"))

FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)
START_RE = re.compile(r"\b(WITH|SELECT)\b", re.I)
# Блок рассуждения Qwen3; закрывающего тега может не быть при обрыве ответа.
THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.S | re.I)


class ModelUnavailable(Exception):
    pass


@dataclass
class Generated:
    sql: str
    raw: str
    model: str
    elapsed_ms: int


# Счётчик вызовов модели и токенов одного ответа (ИИ-03, журнал). Конвейер
# кладёт сюда словарь на время ответа; вызовы модели идут в том же потоке.
USAGE: ContextVar[dict | None] = ContextVar("ai_model_usage", default=None)


MAX_TRACE_CALLS = 100


def _ms(nanoseconds) -> int:
    try:
        return int(nanoseconds or 0) // 1_000_000
    except (TypeError, ValueError):
        return 0


def _count_usage(data: dict) -> None:
    """Токены и время модели по каждому вызову (25.09.2026 — чтение, письмо, загрузка).

    Ollama отдаёт: prompt_eval_count — размер контекста вызова в токенах (вместе с
    частью, взятой из кэша); prompt_eval_duration — время чтения новой части (при
    попадании в кэш — доли секунды); eval_count/duration — сколько модель написала
    и за сколько; load_duration — загрузка модели в память. Замер 25.09.2026:
    контекст 7,5 тыс. токенов из кэша читается за ~0,5 с, без кэша — ~13 с.
    """
    usage = USAGE.get()
    if usage is None or not isinstance(data, dict):
        return
    tokens_in = int(data.get("prompt_eval_count") or 0)
    tokens_out = int(data.get("eval_count") or 0)
    prompt_ms, eval_ms, load_ms = (_ms(data.get("prompt_eval_duration")), _ms(data.get("eval_duration")),
                                   _ms(data.get("load_duration")))
    usage["calls"] = usage.get("calls", 0) + 1
    usage["tokens_in"] = usage.get("tokens_in", 0) + tokens_in
    usage["tokens_out"] = usage.get("tokens_out", 0) + tokens_out
    usage["prompt_ms"] = usage.get("prompt_ms", 0) + prompt_ms
    usage["eval_ms"] = usage.get("eval_ms", 0) + eval_ms
    usage["load_ms"] = usage.get("load_ms", 0) + load_ms
    trace = usage.setdefault("trace", [])
    if len(trace) < MAX_TRACE_CALLS:
        trace.append({"in": tokens_in, "out": tokens_out, "promptMs": prompt_ms, "evalMs": eval_ms, "loadMs": load_ms})


def _post(path: str, payload: dict, timeout: float | None = None) -> dict:
    request = urllib.request.Request(
        f"{OLLAMA_HOST}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8"))
        _count_usage(data)
        return data
    except urllib.error.HTTPError as err:
        # Ollama запущена, но не смогла ответить: причина — в теле ответа
        # («не хватает памяти», «модель не найдена», «процесс завершился»).
        detail = _error_detail(err)
        raise ModelUnavailable(
            f"Модель по адресу {OLLAMA_HOST} ответила ошибкой {err.code}: {detail or err.reason}. {_hint(detail)}"
        ) from err
    except urllib.error.URLError as err:
        raise ModelUnavailable(
            f"Модель недоступна по адресу {OLLAMA_HOST}: {err.reason}. "
            "Проверьте, запущена ли Ollama и загружена ли модель."
        ) from err
    except TimeoutError as err:
        raise ModelUnavailable(
            f"Модель не ответила за {timeout or TIMEOUT_S:.0f} с"
        ) from err


def _error_detail(err: urllib.error.HTTPError) -> str:
    try:
        body = err.read(4000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - тело ошибки может быть недоступно
        return ""
    try:
        detail = json.loads(body).get("error") or ""
    except (ValueError, AttributeError):
        detail = body
    return " ".join(str(detail).split())[:300]


def _hint(detail: str) -> str:
    text = (detail or "").lower()
    if "memory" in text or "памят" in text:
        return (f"Не хватает памяти для модели: закройте тяжёлые приложения или уменьшите AI_NUM_CTX "
                f"(сейчас {NUM_CTX}) и перезапустите Ollama.")
    if "not found" in text or "pull" in text:
        return f"Модель не загружена: выполните «ollama pull {MODEL}»."
    if any(word in text for word in ("terminated", "exit status", "eof", "signal", "killed")):
        return "Процесс модели завершился — перезапустите Ollama и повторите вопрос."
    return "Подробности — в журнале Ollama: ~/.ollama/logs/server.log."


def extract_sql(text: str) -> str:
    """Достаёт запрос из ответа модели.

    Модели склонны добавлять пояснения и разметку даже при прямом запрете,
    поэтому текст разбирается, а не принимается как есть.
    """
    candidate = THINK_RE.sub(" ", text or "").strip()
    fenced = FENCE_RE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    match = START_RE.search(candidate)
    if match:
        candidate = candidate[match.start():]
    candidate = candidate.split(";")[0]
    return candidate.strip()


def installed_models() -> list[str]:
    """Список моделей, загруженных в Ollama. Пустой список — Ollama не отвечает."""
    try:
        request = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", method="GET")
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return sorted(
        str(item.get("name") or "") for item in payload.get("models", []) if item.get("name")
    )


def available(model: str | None = None) -> bool:
    """Модель доступна, если Ollama отвечает и нужная модель в ней загружена."""
    models = installed_models()
    if not models:
        return False
    wanted = model or MODEL
    return any(name == wanted or name.split(":")[0] == wanted.split(":")[0] for name in models)


def _user_content(question: str, model: str, context: str = "") -> str:
    content = user_prompt(question, context)
    if not THINKING and model.lower().startswith("qwen3"):
        # Qwen3 понимает эту директиву и отвечает без блока рассуждения.
        content = f"{content}\n/no_think"
    return content


def generate(question: str, feedback: str | None = None, model: str | None = None,
             context: str = "", timeout: float | None = None) -> Generated:
    model = (model or MODEL).strip()
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": _user_content(question, model, context)},
    ]
    if feedback:
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Предыдущий запрос отклонён проверкой: {feedback}. "
                    "Верни исправленный SELECT без пояснений."
                ),
            }
        )

    started = time.monotonic()
    data = _post(
        "/api/chat",
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": THINKING,
            "options": {"temperature": 0, "num_predict": 1024, "num_ctx": NUM_CTX},
            "keep_alive": KEEP_ALIVE,
        },
        timeout=timeout,
    )
    raw = (data.get("message") or {}).get("content", "")
    return Generated(
        sql=extract_sql(raw),
        raw=raw,
        model=model,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _chat(messages: list[dict], model: str, max_tokens: int, timeout: float | None = None) -> tuple[str, int]:
    started = time.monotonic()
    data = _post(
        "/api/chat",
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": THINKING,
            "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": NUM_CTX},
            "keep_alive": KEEP_ALIVE,
        },
        timeout=timeout,
    )
    content = (data.get("message") or {}).get("content", "")
    return content, int((time.monotonic() - started) * 1000)


def narrate(question: str, scope: str, columns: list[str], rows: list,
            model: str | None = None, timeout: float | None = None) -> tuple[str, int]:
    """Короткое пояснение к результату на человеческом языке.

    Модель видит только уже посчитанные строки — новых обращений к данным
    не происходит, и придумать число мимо результата ей неоткуда. Если
    пояснение не получилось, возвращается пустая строка: ответ с цифрами
    от этого не страдает.
    """
    if not NARRATE or not rows:
        return "", 0
    model = (model or MODEL).strip()
    shown = rows[:NARRATE_MAX_ROWS]
    content = narration_prompt(question, scope, columns, shown)
    if not THINKING and model.lower().startswith("qwen3"):
        content = f"{content}\n/no_think"
    try:
        text, elapsed = _chat(
            [{"role": "system", "content": NARRATION_SYSTEM},
             {"role": "user", "content": content}],
            model, 400, timeout,
        )
    except ModelUnavailable:
        return "", 0
    text = THINK_RE.sub(" ", text or "").strip()
    if len(rows) > len(shown):
        text = f"{text} Показаны первые {len(shown)} строк из {len(rows)}."
    return text.strip(), elapsed
