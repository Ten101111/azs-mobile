"""Запись чисел справки по-русски: разряды — неразрывным пробелом, запятая, «₽», «п. п.»."""
from __future__ import annotations

NBSP = " "
UNITS = {"руб": "₽", "шт": "шт.", "л": "л", "т": "т", "%": "%"}


def number(value, decimals: int = 0) -> str:
    if value is None:
        return "—"
    text = f"{float(value):,.{decimals}f}".replace(",", NBSP).replace(".", ",")
    return text.replace("-", "−")


def unit(code: str) -> str:
    return UNITS.get(code, code)


def value(value, unit_code: str, decimals: int = 0) -> str:
    if value is None:
        return "—"
    if not unit_code:  # средняя оценка, качество сервиса — единица в названии показателя
        return number(value, decimals)
    if unit_code == "%":
        return f"{number(value, decimals)}{NBSP}%"
    return f"{number(value, decimals)}{NBSP}{unit(unit_code)}"


def delta(value, share: bool = False, decimals: int = 1) -> str:
    """Изменение со знаком: «+3,1 %», «−0,4 п. п.»; нет данных — «—»."""
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    suffix = f"{NBSP}п.{NBSP}п." if share else f"{NBSP}%"
    return f"{sign}{number(value, decimals)}{suffix}"


def signed(value, decimals: int = 0) -> str:
    """Разность со знаком без единицы: «+3», «−0,012»; нет данных — «—»."""
    if value is None:
        return "—"
    return f"{'+' if value > 0 else ''}{number(value, decimals)}"


def change(value, kind: str, decimals: int = 1) -> str:
    """Изменение по виду: pct — «+3,1 %», pp — «+0,12 п. п.», abs — «−0,004»."""
    if kind == "pp":
        return delta(value, share=True, decimals=decimals)
    if kind == "abs":
        return signed(value, decimals)
    return delta(value)


def compact(value, unit_code: str) -> str:
    """Крупные суммы: «12,4 млн ₽», «1,2 млрд ₽»; остальное — как есть."""
    if value is None:
        return "—"
    v = float(value)
    for limit, word in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс.")):
        if abs(v) >= limit * 10 or (word != "тыс." and abs(v) >= limit):
            return f"{number(v / limit, 1)}{NBSP}{word}{NBSP}{unit(unit_code)}"
    return value_text(v, unit_code)


def value_text(v, unit_code: str) -> str:
    return value(v, unit_code, 0 if abs(float(v)) >= 100 else 1)


def days(n: int) -> str:
    """«1 день», «2 дня», «5 дней»."""
    n = int(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} дня"
    return f"{n} дней"
