"""Реестр инструментов агента и контекст их выполнения.

Инструмент — функция `(ctx, **args) -> dict`. Всё, что она вернёт, уходит
модели как результат вызова; исключения переводятся в `{"error": ...}`, чтобы
модель могла исправиться, а не уронить ответ. Аргументы проверяются по
схеме до вызова: модель — недоверенный источник и здесь тоже.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import executor
from ..catalog import CATALOG, Catalog
from ..semantic import SEMANTIC, Semantic
from ..validator import Scope
from .state import Budget, Step, Workspace


@dataclass
class ToolContext:
    question: str
    scope: Scope
    budget: Budget
    workspace: Workspace = field(default_factory=Workspace)
    catalog: Catalog = field(default_factory=lambda: CATALOG)
    semantic: Semantic = field(default_factory=lambda: SEMANTIC)
    # Точки подмены для тестов и стенда: прежний генератор SQL и исполнитель.
    generate_sql: Callable[[str], str] | None = None
    run_query: Callable[[str, int], executor.Result] = executor.run
    on_step: Callable[[Step, str], None] | None = None
    sql_ms: int = 0
    # Предел одного расчёта в песочнице; None — AI_PY_TIMEOUT (у администратора больше).
    python_timeout_s: float | None = None
    # ИИ-07: текстовые части файлов пользователя (t1, t2…) — file_tools.read_file.
    file_texts: dict = field(default_factory=dict)
    _range: tuple | None = None

    def emit(self, step: Step, state: str) -> None:
        if self.on_step is None:
            return
        try:
            self.on_step(step, state)
        except Exception:  # noqa: BLE001 - слушатель не роняет ответ
            pass

    def qualified(self, table: str) -> str:
        # Схема своя у каждой таблицы: витрины — dm, справочники РУ/ТМ — bds.
        if "." in table:
            return table
        schema = (self.catalog.table_schemas.get(table) or self.catalog.schema or "").strip()
        return f"{schema}.{table}" if schema else table


class ToolError(Exception):
    """Ошибка, о которой модели полезно узнать текстом."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., dict]
    kind: str = "schema"        # schema | sql | python | chart | finish

    def as_ollama(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict, kind: str = "schema"):
    def wrap(handler):
        REGISTRY[name] = ToolSpec(name, description, parameters, handler, kind)
        return handler
    return wrap


def specs(names: list[str] | None = None) -> list[ToolSpec]:
    if names is None:
        return list(REGISTRY.values())
    return [REGISTRY[n] for n in names if n in REGISTRY]


# --- проверка аргументов ---------------------------------------------------

def _coerce(value: Any, schema: dict) -> Any:
    kind = schema.get("type")
    if kind == "string":
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if kind == "integer":
        try:
            return int(value)
        except (TypeError, ValueError) as err:
            raise ToolError(f"ожидалось целое число, получено {value!r}") from err
    if kind == "number":
        try:
            return float(value)
        except (TypeError, ValueError) as err:
            raise ToolError(f"ожидалось число, получено {value!r}") from err
    if kind == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "да"}
        return bool(value)
    if kind == "array":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list):
            raise ToolError(f"ожидался список, получено {value!r}")
        item_schema = schema.get("items") or {}
        return [_coerce(v, item_schema) if item_schema else v for v in value]
    if kind == "object":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as err:
                raise ToolError("ожидался объект JSON") from err
        if not isinstance(value, dict):
            raise ToolError(f"ожидался объект, получено {value!r}")
        return value
    return value


def validate_args(spec: ToolSpec, raw: dict | None) -> dict:
    raw = dict(raw or {})
    props = spec.parameters.get("properties", {})
    required = spec.parameters.get("required", [])
    clean: dict[str, Any] = {}
    for name, schema in props.items():
        if name in raw and raw[name] is not None:
            clean[name] = _coerce(raw[name], schema)
        elif name in required:
            raise ToolError(f"не хватает обязательного аргумента «{name}»")
    unknown = set(raw) - set(props)
    if unknown:
        # Лишние аргументы не ошибка — модель часто добавляет пояснения. Игнорируем.
        pass
    for name, schema in props.items():
        allowed = schema.get("enum")
        if allowed and name in clean and clean[name] not in allowed:
            raise ToolError(f"«{name}» должно быть одним из: {', '.join(map(str, allowed))}")
    return clean


def call(ctx: ToolContext, name: str, raw_args: dict | None) -> dict:
    """Вызвать инструмент по имени. Любая ошибка — в виде словаря для модели."""
    spec = REGISTRY.get(name)
    if spec is None:
        return {"error": f"инструмента «{name}» нет; доступны: {', '.join(REGISTRY)}"}
    started = time.monotonic()
    try:
        args = validate_args(spec, raw_args)
        result = spec.handler(ctx, **args)
    except ToolError as err:
        result = {"error": str(err)}
    except Exception as err:  # noqa: BLE001 - модель должна увидеть причину
        result = {"error": f"{type(err).__name__}: {err}"}
    if isinstance(result, dict):
        result.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
    return result


def compact(result: dict, limit: int = 6000) -> str:
    """Результат инструмента в виде строки для модели, с ограничением длины."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    trimmed = dict(result)
    if isinstance(trimmed.get("rows"), list):
        rows = trimmed["rows"]
        keep = max(3, len(rows) // 2)
        while keep >= 3:
            trimmed["rows"] = rows[:keep]
            trimmed["rows_shown"] = keep
            text = json.dumps(trimmed, ensure_ascii=False, default=str)
            if len(text) <= limit:
                return text
            keep //= 2
    return text[: limit - 20] + "… (обрезано)"


# Регистрация инструментов: модули ниже добавляют себя в REGISTRY при импорте.
from . import schema_tools, sql_tool, sandbox, charts, file_tools  # noqa: E402,F401
