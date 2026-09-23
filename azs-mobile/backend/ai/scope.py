"""Область данных для ИИ-контура.

Роли и привязки описаны в backend.roles — там же, где их берут экраны.
Список объектов по привязке вычисляется так:

  * витрина ОХД (AI_DB_BACKEND=postgres) — по справочникам самой витрины,
    см. dwh_scope (решение владельца 23.09.2026: ИИ живёт в ДВХ). Экраны при
    этом продолжают работать по stations.json; возможные расхождения —
    задача З-29 (бэклог), до её решения допустимы;
  * стенд SQLite — по справочнику приложения (backend.roles.resolve).
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import roles
from . import dwh_scope
from .validator import Scope

DATA = Path(__file__).resolve().parents[2] / "data"
REFERENCE_DB = Path(os.environ.get("AI_REFERENCE_DB") or DATA / "ai_reference.sqlite3")

UNRESTRICTED_ROLES = {code for code, spec in roles.ROLES.items() if spec.unrestricted}


def build(role: str, value: str | None = None) -> Scope:
    """Область данных по роли и привязке в виде, понятном валидатору."""
    if dwh_scope.enabled():
        return dwh_scope.build(role, value or "")
    resolved = roles.resolve(role, value or "")
    if resolved.unrestricted:
        return Scope.all_network()
    if not roles.ROLES.get(role):
        raise ValueError(f"Неизвестная роль: {role!r}")
    return Scope.for_stations(resolved.ksss, resolved.label)


def from_user(user) -> Scope:
    """Область данных текущего пользователя — без участия запроса."""
    return build(getattr(user, "role", "") or "", getattr(user, "roleBinding", "") or "")
