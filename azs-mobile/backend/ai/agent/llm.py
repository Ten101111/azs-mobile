"""Общение с моделью-оркестратором.

`OllamaChat` ходит в тот же Ollama, что и генератор SQL (`generator._post`),
но умеет два дополнительных режима: вызовы инструментов (поле `tools`
/api/chat) и ответ строго в JSON (`format: json`). Если модель не поддерживает
tool calls и пишет вызов текстом — он разбирается из содержимого.

`ScriptedModel` — модель по сценарию для тестов и офлайн-прогона: та же
сигнатура, ответы заданы заранее. На ней проверяется контур инструментов,
а не качество рассуждений.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import generator

# Тот же размер контекста, что у быстрого пути: иначе Ollama перезагружает модель.
NUM_CTX = generator.NUM_CTX
MAX_TOKENS = int(os.environ.get("AI_AGENT_MAX_TOKENS", "1500"))

FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    elapsed_ms: int = 0
    raw: dict | None = None
    model: str = ""

    @property
    def text(self) -> str:
        return generator.THINK_RE.sub(" ", self.content or "").strip()


class ModelUnavailable(generator.ModelUnavailable):
    pass


# --- разбор JSON из текста --------------------------------------------------

def extract_json(text: str) -> dict | list | None:
    """Первый JSON-объект в тексте модели, терпимый к обвязке и хвостам."""
    candidate = generator.THINK_RE.sub(" ", text or "").strip()
    fenced = FENCE_RE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        if start < 0:
            continue
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    chunk = candidate[start:index + 1]
                    parsed = _loads(chunk)
                    if parsed is not None:
                        return parsed
                    break
    return None


def _loads(chunk: str):
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        pass
    # Частые огрехи: запятая перед закрывающей скобкой, одинарные кавычки.
    cleaned = re.sub(r",\s*([}\]])", r"\1", chunk)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def calls_from_text(text: str) -> list[ToolCall]:
    """Вызов инструмента, записанный текстом: {"tool": ..., "arguments": {...}}."""
    parsed = extract_json(text)
    items = parsed if isinstance(parsed, list) else [parsed]
    calls = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("tool") or item.get("name") or item.get("function")
        if isinstance(name, dict):
            args = name.get("arguments") or name.get("args") or {}
            name = name.get("name")
        else:
            args = item.get("arguments") or item.get("args") or item.get("parameters") or {}
        if isinstance(name, str) and name:
            if isinstance(args, str):
                args = _loads(args) or {}
            calls.append(ToolCall(name=name, arguments=args if isinstance(args, dict) else {}))
    return calls


# --- Ollama -------------------------------------------------------------------

class OllamaChat:
    def __init__(self, model: str | None = None, max_tokens: int | None = None,
                 timeout_s: float | None = None):
        self.model = (model or generator.MODEL).strip()
        # Пределы роли (limits.py): у администратора ответ хода длиннее, ожидание дольше.
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    def _prepare(self, messages: list[dict]) -> list[dict]:
        if generator.THINKING or not self.model.lower().startswith("qwen3"):
            return messages
        prepared = [dict(m) for m in messages]
        for message in reversed(prepared):
            if message.get("role") == "user":
                if "/no_think" not in (message.get("content") or ""):
                    message["content"] = f"{message.get('content', '')}\n/no_think"
                break
        return prepared

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             json_mode: bool = False, max_tokens: int | None = None) -> Reply:
        max_tokens = max_tokens or self.max_tokens or MAX_TOKENS
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._prepare(messages),
            "stream": False,
            "think": generator.THINKING,
            "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": NUM_CTX},
        }
        if tools:
            payload["tools"] = tools
        if json_mode:
            payload["format"] = "json"
        started = time.monotonic()
        try:
            data = generator._post("/api/chat", payload, timeout=self.timeout_s)
        except generator.ModelUnavailable as err:
            raise ModelUnavailable(str(err)) from err
        message = data.get("message") or {}
        content = message.get("content") or ""
        calls = []
        for index, item in enumerate(message.get("tool_calls") or []):
            function = item.get("function") or {}
            args = function.get("arguments") or {}
            if isinstance(args, str):
                args = _loads(args) or {}
            calls.append(ToolCall(name=function.get("name") or "", arguments=args,
                                  id=str(item.get("id") or f"call_{index}")))
        if not calls and tools and content:
            calls = [c for c in calls_from_text(content) if c.name]
        return Reply(content=content, tool_calls=calls,
                     elapsed_ms=int((time.monotonic() - started) * 1000), raw=data, model=self.model)



def salvage_json(text: str) -> dict | None:
    """JSON-объект из ответа модели, даже если ответ оборвался на лимите токенов.

    Сначала обычный разбор; если он не удался — ответ обрезается по последнему
    завершённому значению и незакрытые скобки дописываются. Так оборванный
    итог («…"happened": ["А", "Б", "В") всё равно даёт заголовок и пункты,
    а не сырой JSON на экране.
    """
    parsed = extract_json(text)
    if isinstance(parsed, dict):
        return parsed
    start = (text or "").find("{")
    if start < 0:
        return None
    body = text[start:]
    cuts = [i + 1 for i, ch in enumerate(body) if ch in '"]}0123456789el']
    for cut in reversed(cuts[-400:]):
        candidate = _close_json(body[:cut])
        if candidate is None:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and value:
            return value
    return None


def _close_json(fragment: str) -> str | None:
    stack: list[str] = []
    in_string = escaped = False
    for ch in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
    if in_string:
        return None
    trimmed = fragment.rstrip().rstrip(",:")
    return trimmed + "".join(reversed(stack))

# --- сценарная модель ---------------------------------------------------------

class ScriptedModel:
    """Отвечает по списку: элемент — Reply, словарь вызова, строка или функция от сообщений."""

    def __init__(self, script: list, model: str = "scripted"):
        self.script = list(script)
        self.model = model
        self.calls: list[dict] = []
        self.transcript: list[list[dict]] = []

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             json_mode: bool = False, max_tokens: int = MAX_TOKENS) -> Reply:
        self.transcript.append([dict(m) for m in messages])
        self.calls.append({"tools": bool(tools), "json": json_mode})
        if not self.script:
            raise ModelUnavailable("сценарий исчерпан")
        item = self.script.pop(0)
        if callable(item):
            item = item(messages)
        if isinstance(item, Reply):
            item.model = self.model
            return item
        if isinstance(item, dict):
            if "tool" in item or "name" in item:
                name = item.get("tool") or item.get("name")
                return Reply(tool_calls=[ToolCall(name=name, arguments=item.get("arguments") or item.get("args") or {}, id="s")],
                             model=self.model)
            return Reply(content=json.dumps(item, ensure_ascii=False), model=self.model)
        if isinstance(item, list):
            return Reply(tool_calls=[ToolCall(name=c.get("tool") or c.get("name"), arguments=c.get("arguments") or c.get("args") or {}, id=f"s{i}")
                                     for i, c in enumerate(item)], model=self.model)
        return Reply(content=str(item), model=self.model)


ModelLike = Callable  # любой объект с методом chat(messages, tools, json_mode, max_tokens)
