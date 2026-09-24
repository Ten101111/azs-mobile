"""Пределы ИИ-контура по ролям.

Решение владельца от 23.09.2026: у администратора ограничений в рамках лимитов
нет — ни бюджета шагов агента, ни общего предела времени; ответы модели и
таблицы большие, запросы к витрине и расчёты могут идти дольше.

Лимитами не считаются и остаются для всех: область данных, валидатор SQL,
доступ только на чтение и песочница расчётов — это защита данных. Остаётся и
страховка от зацикливания: число ходов модели велико, но конечно.

Роль берётся действующая: администратор, который смотрит ответы «от имени»
другой роли, получает её пределы — так проверка показывает то, что увидит она.
Значения меняются переменными окружения AI_ADMIN_* без правки кода.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


UNLIMITED_ROLES = {r.strip() for r in (os.environ.get("AI_UNLIMITED_ROLES") or "admin").split(",") if r.strip()}


@dataclass(frozen=True)
class Limits:
    """None — действует обычное значение модуля (validator, executor, llm, loop)."""

    unlimited: bool = False
    fast_rows: int | None = None          # строк в ответе «Лёгкого» уровня
    agent_rows: int | None = None         # строк в результате запроса агента
    sql_timeout_s: float | None = None    # один запрос к витрине
    python_timeout_s: float | None = None # один расчёт в песочнице
    turn_tokens: int | None = None        # ответ модели на одном ходе агента
    final_tokens: int | None = None       # итоговый ответ
    model_timeout_s: float | None = None  # один вызов модели
    max_seconds: float | None = None      # весь прогон агента; 0 — без предела
    sql_calls: int | None = None
    python_calls: int | None = None
    charts: int | None = None
    model_turns: int | None = None


STANDARD = Limits()

ADMIN = Limits(
    unlimited=True,
    fast_rows=_int("AI_ADMIN_ROWS", 10_000),
    agent_rows=_int("AI_ADMIN_ROWS", 10_000),
    sql_timeout_s=_float("AI_ADMIN_SQL_TIMEOUT", 120),
    python_timeout_s=_float("AI_ADMIN_PY_TIMEOUT", 120),
    turn_tokens=_int("AI_ADMIN_TOKENS", 4096),
    final_tokens=_int("AI_ADMIN_TOKENS", 4096),
    model_timeout_s=_float("AI_ADMIN_MODEL_TIMEOUT", 900),
    max_seconds=_float("AI_ADMIN_MAX_SECONDS", 0),
    sql_calls=_int("AI_ADMIN_SQL", 40),
    python_calls=_int("AI_ADMIN_PY", 15),
    charts=_int("AI_ADMIN_CHARTS", 8),
    # Страховка от зацикливания, а не лимит: столько ходов не нужно ни одному вопросу.
    model_turns=_int("AI_ADMIN_TURNS", 60),
)


def for_role(role: str | None) -> Limits:
    return ADMIN if (role or "").strip() in UNLIMITED_ROLES else STANDARD
