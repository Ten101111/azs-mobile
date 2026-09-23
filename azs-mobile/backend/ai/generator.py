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
from dataclasses import dataclass

from .contract import NARRATION_SYSTEM, narration_prompt, system_prompt, user_prompt

OLLAMA_HOST = os.environ.get("AI_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("AI_MODEL", "qwen3:8b")
# Рассуждающие модели (Qwen3 и подобные) по умолчанию печатают ход мысли.
# Для генерации SQL он не нужен и только удлиняет ответ.
THINKING = (os.environ.get("AI_MODEL_THINK") or "0").strip().lower() in {"1", "true", "yes", "on"}
TIMEOUT_S = float(os.environ.get("AI_MODEL_TIMEOUT", "120"))
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


def _post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{OLLAMA_HOST}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as err:
        raise ModelUnavailable(
            f"Модель недоступна по адресу {OLLAMA_HOST}: {err.reason}. "
            "Проверьте, запущена ли Ollama и загружена ли модель."
        ) from err
    except TimeoutError as err:
        raise ModelUnavailable(
            f"Модель не ответила за {TIMEOUT_S:.0f} с"
        ) from err


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
             context: str = "") -> Generated:
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
            "options": {"temperature": 0, "num_predict": 1024},
        },
    )
    raw = (data.get("message") or {}).get("content", "")
    return Generated(
        sql=extract_sql(raw),
        raw=raw,
        model=model,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _chat(messages: list[dict], model: str, max_tokens: int) -> tuple[str, int]:
    started = time.monotonic()
    data = _post(
        "/api/chat",
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": THINKING,
            "options": {"temperature": 0, "num_predict": max_tokens},
        },
    )
    content = (data.get("message") or {}).get("content", "")
    return content, int((time.monotonic() - started) * 1000)


def narrate(question: str, scope: str, columns: list[str], rows: list,
            model: str | None = None) -> tuple[str, int]:
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
            model, 400,
        )
    except ModelUnavailable:
        return "", 0
    text = THINK_RE.sub(" ", text or "").strip()
    if len(rows) > len(shown):
        text = f"{text} Показаны первые {len(shown)} строк из {len(rows)}."
    return text.strip(), elapsed
