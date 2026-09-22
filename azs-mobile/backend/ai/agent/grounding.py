"""Сверка чисел финального текста с данными.

Каждое число в ответе должно прослеживаться до результата запроса или
вычисления. Числа из текста сравниваются с множеством «доказательств»:
значения результатов, их разности и отношения внутри одного набора,
числа из вывода Python. Не найденное — не выдумка автоматически (модель
могла округлить иначе), но повод для одной попытки исправления, а затем
для удаления фразы: лучше короче, чем с неподтверждённой цифрой.
"""
from __future__ import annotations

import math
import re
from typing import Iterable

from .state import Analysis, Workspace

NUMBER_RE = re.compile(r"(?<![\w.])[-−–]?\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|(?<![\w.])[-−–]?\d+(?:[.,]\d+)?")
YEAR_MIN, YEAR_MAX = 2000, 2100
MAX_EVIDENCE_VALUES = 2500
MAX_PAIR_VALUES = 400


def numbers_in_text(text: str) -> list[float]:
    values = []
    for match in NUMBER_RE.finditer(text or ""):
        raw = match.group(0).replace(" ", " ").replace(" ", "").replace(",", ".")
        raw = raw.replace("−", "-").replace("–", "-")
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


def _trivial(value: float, text: str) -> bool:
    """Числа, которые не требуют подтверждения: годы, дни, малые счётчики."""
    if YEAR_MIN <= value <= YEAR_MAX and float(value).is_integer():
        return True
    if abs(value) <= 31 and float(value).is_integer():
        return True
    return False


def _close(a: float, b: float) -> bool:
    # Знак в прозе передаётся словом («снизилось на 5 %»), поэтому сравниваем модули.
    a, b = abs(a), abs(b)
    if a == b:
        return True
    if b == 0:
        return abs(a) < 0.5
    rel = abs(a - b) / max(abs(a), abs(b))
    if rel <= 0.006:
        return True
    # Округление до целых тысяч/миллионов: 12 345 678 → 12,3 млн.
    for scale in (1e3, 1e6, 1e9):
        scaled = b / scale
        if abs(scaled) >= 1 and (abs(a - scaled) / max(abs(a), abs(scaled)) <= 0.006 or abs(a - round(scaled, 1)) < 1e-9):
            return True
    return False


def _as_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        cleaned = value.replace(" ", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def evidence(workspace: Workspace, extra_text: Iterable[str] = ()) -> list[float]:
    """Множество чисел, которыми можно подтвердить текст ответа.

    Сначала — вывод вычислений, пороги из SQL и кода: это самые точные
    свидетельства. Затем значения результатов, их суммы и средние, число
    строк, а для небольших таблиц — разности и отношения внутри строки и
    внутри колонки («−5,3 %», «на 1 200 меньше»).
    """
    values: list[float] = []
    for text in extra_text:
        values.extend(numbers_in_text(text or ""))
    for step in workspace.steps:
        if step.output:
            values.extend(numbers_in_text(step.output))
        # Пороги и константы расчёта («не меньше 500 чеков», «медиана × 1,5»)
        # законно берутся из самого запроса или кода, а не из данных.
        for source in (step.sql, step.code):
            if source:
                values.extend(numbers_in_text(source))
    for rs in workspace.results.values():
        columns: list[list[float | None]] = []
        for index in range(len(rs.columns)):
            column = [_as_float(row[index]) if index < len(row) else None for row in rs.rows]
            if any(v is not None for v in column):
                columns.append(column)
                values.extend(v for v in column if v is not None)
        for column in columns:
            present = [v for v in column if v is not None]
            if present:
                values.append(sum(present))
                values.append(sum(present) / len(present))
        # «855 из 1843 объектов»: число строк и заполненных значений — тоже факт данных.
        values.append(float(rs.row_count))
        for column in columns:
            filled = sum(1 for v in column if v is not None)
            values.append(float(filled))
            values.append(float(rs.row_count - filled))
        if rs.row_count <= MAX_PAIR_VALUES and len(columns) <= 12:
            # Пары внутри строки (сравнение колонок) и внутри колонки (динамика).
            for row_index in range(rs.row_count):
                cells = [c[row_index] for c in columns if c[row_index] is not None]
                _pairs(cells, values)
            for column in columns:
                _pairs([v for v in column if v is not None][:60], values)
    return values


def _pairs(cells: list[float], values: list[float]) -> None:
    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            values.append(a - b)
            if b:
                values.append((a / b - 1.0) * 100.0)
                values.append(a / b * 100.0)
                values.append(a / b)
            if a:
                values.append((b / a - 1.0) * 100.0)
                values.append(b / a * 100.0)
                values.append(b / a)


def check(analysis: Analysis, workspace: Workspace) -> dict:
    """Какие числа текста не нашлись в данных."""
    pool = evidence(workspace)
    unverified: list[str] = []
    checked = 0
    for text in _sentences(analysis):
        for value in numbers_in_text(text):
            if _trivial(value, text):
                continue
            checked += 1
            if not any(_close(value, candidate) for candidate in pool):
                unverified.append(_format(value))
    return {"checked": checked, "unverified": sorted(set(unverified), key=unverified.index)}


def strip_unverified(analysis: Analysis, workspace: Workspace) -> tuple[Analysis, list[str]]:
    """Убрать пункты с неподтверждёнными числами; вернуть, что убрано."""
    pool = evidence(workspace)
    removed: list[str] = []

    def ok(text: str) -> bool:
        for value in numbers_in_text(text):
            if _trivial(value, text):
                continue
            if not any(_close(value, candidate) for candidate in pool):
                return False
        return True

    def filter_list(items: list[str]) -> list[str]:
        kept = []
        for item in items:
            if ok(item):
                kept.append(item)
            else:
                removed.append(item)
        return kept

    headline = analysis.headline
    if not ok(headline):
        sentences = re.split(r"(?<=[.!?])\s+", headline)
        good = [s for s in sentences if ok(s)]
        removed.append(headline)
        headline = " ".join(good).strip()
    cleaned = Analysis(
        headline=headline,
        happened=filter_list(analysis.happened),
        why=filter_list(analysis.why),
        where=filter_list(analysis.where),
        actions=filter_list(analysis.actions),
        limitations=list(analysis.limitations),
    )
    if not cleaned.headline:
        cleaned.headline = cleaned.happened[0] if cleaned.happened else "Ответ сформирован по данным ниже."
    if removed:
        cleaned.limitations.append(
            "Часть формулировок опущена: их числа не подтвердились расчётом."
        )
    return cleaned, removed


def _sentences(analysis: Analysis) -> list[str]:
    return [analysis.headline, *analysis.happened, *analysis.why, *analysis.where, *analysis.actions]


def _format(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")
