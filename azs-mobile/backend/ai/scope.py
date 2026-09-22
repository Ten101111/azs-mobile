"""Область данных для ИИ-контура.

Собственной логики здесь нет: роль и привязка разбираются модулем
backend.roles — тем же, по которому работают экраны приложения.
Два разных представления о том, что видит пользователь, недопустимы.
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import roles
from .validator import Scope

DATA = Path(__file__).resolve().parents[2] / "data"
REFERENCE_DB = Path(os.environ.get("AI_REFERENCE_DB") or DATA / "ai_reference.sqlite3")

UNRESTRICTED_ROLES = {code for code, spec in roles.ROLES.items() if spec.unrestricted}


def build(role: str, value: str | None = None) -> Scope:
    """Область данных по роли и привязке в виде, понятном валидатору."""
    resolved = roles.resolve(role, value or "")
    if resolved.unrestricted:
        return Scope.all_network()
    if not roles.ROLES.get(role):
        raise ValueError(f"Неизвестная роль: {role!r}")
    return Scope.for_stations(resolved.ksss, resolved.label)


def from_user(user) -> Scope:
    """Область данных текущего пользователя — без участия запроса."""
    return build(getattr(user, "role", "") or "", getattr(user, "roleBinding", "") or "")
