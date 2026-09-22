"""Проверки качества результата запроса.

Выполняются после каждого `run_sql` и возвращаются модели вместе с данными:
пустой результат, пустые значения, повторы строк, обрезка по лимиту, период,
подозрительные проценты. Это не запрет — это предупреждение, которое модель
обязана учесть, а трасса — сохранить.
"""
from __future__ import annotations

import re
from typing import Any

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _is_dateish(values: list[Any]) -> bool:
    sample = [v for v in values if v is not None][:20]
    if not sample:
        return False
    return all(isinstance(v, str) and (DATE_RE.match(v) or MONTH_RE.match(v)) for v in sample)


def inspect(columns: list[str], rows: list[list[Any]], truncated: bool, row_limit: int,
            data_range: tuple[str | None, str | None] | None = None) -> list[str]:
    warnings: list[str] = []
    hint = ""
    if data_range and data_range[0] and data_range[1]:
        hint = f" Данные в витрине есть с {data_range[0]} по {data_range[1]}."
    if not rows:
        warnings.append(
            "Результат пуст: по этим условиям строк нет. Проверь период и фильтры;"
            " если период вне диапазона данных — так и скажи пользователю." + hint
        )
        return warnings
    if all(all(v is None for v in row) for row in rows):
        warnings.append(
            "Все значения пустые: агрегат по условию не нашёл ни одной строки. "
            "Это не ноль, а отсутствие данных — скажи об этом пользователю." + hint
        )
        return warnings

    if truncated:
        warnings.append(
            f"Обрезано по лимиту {row_limit} строк — итоги по такому набору неполные;"
            " агрегируй в SQL или сузь выборку."
        )

    total = len(rows)
    for index, column in enumerate(columns):
        values = [row[index] if index < len(row) else None for row in rows]
        nulls = sum(1 for v in values if v is None)
        if nulls:
            warnings.append(f"Колонка «{column}»: {nulls} из {total} значений пустые (NULL).")

    seen: set[tuple] = set()
    duplicates = 0
    for row in rows:
        key = tuple(_hashable(v) for v in row)
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    if duplicates:
        warnings.append(
            f"Повторяющихся строк: {duplicates} — возможно, соединение задвоило строки;"
            " проверь ключ JOIN и GROUP BY."
        )

    for index, column in enumerate(columns):
        values = [row[index] if index < len(row) else None for row in rows]
        if _is_dateish(values):
            present = sorted(v for v in values if v is not None)
            if present:
                warnings.append(f"Период в результате: с {present[0]} по {present[-1]} ({len(set(present))} знач.).")
                last_day = data_range[1] if data_range else None
                if last_day and MONTH_RE.match(present[-1]) and str(last_day).startswith(present[-1]) \
                        and not str(last_day).endswith(("-28", "-29", "-30", "-31")):
                    warnings.append(
                        f"Последний месяц {present[-1]} неполный: данные по {last_day}. "
                        "Не сравнивай его с полными месяцами напрямую — возьми тот же отрезок дней или показатель в день."
                    )
            break

    for index, column in enumerate(columns):
        name = column.lower()
        if "%" in name or "доля" in name or "конверс" in name or "динамик" in name:
            numbers = [v for v in (row[index] if index < len(row) else None for row in rows)
                       if isinstance(v, (int, float))]
            if numbers and max(abs(n) for n in numbers) > 1000:
                warnings.append(
                    f"Колонка «{column}» похожа на процент, но значения больше 1000 —"
                    " проверь формулу (умножение на 100 дважды или неверный знаменатель)."
                )
    return warnings


def _hashable(value: Any):
    if isinstance(value, (list, dict, set)):
        return repr(value)
    return value
