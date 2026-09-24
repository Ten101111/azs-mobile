"""СП-01. Реестр типов справок.

Тип справки = уровень (сеть, ОНПО, территория, объект) + область данных +
построитель. Сейчас включён один тип — справка по всей сети, еженедельно.
Справки ОНПО, территории и объекта из Макетов v0.3 добавятся сюда как новые
типы на том же каркасе (СП-08).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import weekly


@dataclass(frozen=True)
class ReportType:
    code: str
    title: str
    level: str          # network | npo | territory | station
    scope_key: str
    period: str         # week
    builder: Callable
    description: str = ""


REPORT_TYPES: dict[str, ReportType] = {
    weekly.TYPE_CODE: ReportType(
        code=weekly.TYPE_CODE, title="Справка по сети", level="network", scope_key="network", period="week",
        builder=weekly.build,
        description="Вся сеть: неделя к прошлой неделе и к той же неделе прошлого года",
    ),
}
DEFAULT_TYPE = weekly.TYPE_CODE


def get(code: str | None) -> ReportType:
    return REPORT_TYPES.get(code or DEFAULT_TYPE) or REPORT_TYPES[DEFAULT_TYPE]


def allowed(user) -> bool:
    """Решение Р-13: сначала справка видна только администратору; после проверки
    первых выпусков — субадминистратору и АУП сети (тогда расширить здесь)."""
    return bool(getattr(user, "isAdmin", False))
