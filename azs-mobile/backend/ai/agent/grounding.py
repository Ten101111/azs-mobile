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
# Поля рекомендации, которые пишет модель: их числа сверяются так же, как текст.
REC_TEXT_FIELDS = ("action", "basis", "effect", "limits")
UNVERIFIED_NOTE = "Часть формулировок опущена: их числа не подтвердились расчётом."
MAX_PAIR_VALUES = 400
# Результат до стольких строк — «небольшой»: совпадение с его ячейкой — сильное свидетельство.
SMALL_RESULT = 50
# Явная формула над ячейками показывается только для таблиц до стольких строк.
ARITHMETIC_ROWS = 12


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


def numbers_with_units(text: str) -> list[tuple[float, bool]]:
    """Числа текста и признак «это проценты или п. п.» (по знаку сразу после числа)."""
    out = []
    for match in NUMBER_RE.finditer(text or ""):
        values = numbers_in_text(match.group(0))
        if not values:
            continue
        tail = (text[match.end():match.end() + 6]).lstrip("  ")
        out.append((values[0], tail.startswith("%") or tail.lower().startswith(("п.п", "п. п"))))
    return out


def _exact(value: float, candidate: float) -> bool:
    """Совпадение с точностью до знаков, показанных в тексте (23,5 ↔ 23,47)."""
    text = repr(abs(value))
    decimals = 0 if float(value).is_integer() else len(text.split(".")[1]) if "." in text and "e" not in text else 6
    return round(abs(candidate), decimals) == round(abs(value), decimals)


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


# Происхождение числа: откуда оно взялось. По нему ИИ-25 ставит тип
# утверждения — «Факт» (ячейка или число строк результата запроса),
# «Расчёт» (разность, отношение, сумма, вывод Python) или параметр расчёта
# (порог из текста запроса). Порядок списка — от прямых свидетельств к
# производным: первое совпадение и есть самое точное объяснение числа.
FACT, CALC, PARAM = "fact", "calc", "param"
DERIVED_COLUMN_RE = re.compile(r"%|Δ|измен|отклон|дол[яи]|темп|прирост|разниц|динамик|к прошл|к пр\.", re.IGNORECASE)


class Proof:
    """Число-свидетельство и его происхождение (формула строится по запросу)."""

    __slots__ = ("value", "kind", "source", "op", "label", "a", "b", "la", "lb")

    def __init__(self, value: float, kind: str, source: str = "", op: str = "", label: str = "",
                 a: float | None = None, b: float | None = None, la: str = "", lb: str = ""):
        self.value, self.kind, self.source, self.op, self.label = value, kind, source, op, label
        self.a, self.b, self.la, self.lb = a, b, la, lb

    def formula(self, target: float | None = None) -> str:
        """Формула с исходными числами; `target` — число из текста (для знака разности)."""
        v, a, b = _human(self.value), _human(self.a), _human(self.b)
        names = f" ({self.la} и {self.lb})" if self.la and self.lb else ""
        if self.op == "diff":
            if target is not None and self.value and (target < 0) != (self.value < 0):
                names = f" ({self.lb} и {self.la})" if self.la and self.lb else ""
                return f"{b} − {a} = {_human(-self.value)}{names}"
            return f"{a} − {b} = {v}{names}"
        if self.op == "pct":
            return f"({a} / {b} − 1) × 100 % = {v} %{names}"
        if self.op == "share":
            return f"{a} / {b} × 100 % = {v} %{names}"
        if self.op == "ratio":
            return f"{a} / {b} = {v}{names}"
        if self.op == "sum":
            return f"сумма «{self.label}» = {v}"
        if self.op == "mean":
            return f"среднее «{self.label}» = {v}"
        if self.op == "column":
            return f"столбец «{self.label}» — считается в запросе"
        if self.op == "python":
            return f"вычисление «{self.label}»" if self.label else "вычисление"
        if self.op == "param":
            return f"условие расчёта: {v}"
        if self.op == "count":
            return f"число строк результата: {v}"
        return ""


def _human(value: float | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", " ")
    text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return text.rstrip("0").rstrip(",")


def _row_label(rs, row_index: int) -> str:
    for value in rs.rows[row_index]:
        if isinstance(value, str) and _as_float(value) is None and value.strip():
            return value.strip()[:40]
    return f"строка {row_index + 1}"


def base_proofs(workspace: Workspace) -> list[Proof]:
    """Прямые свидетельства и дешёвые производные — без попарных сравнений.

    Порядок — по силе свидетельства: ячейки небольших результатов запроса,
    вывод вычислений Python, число строк, столбцы-отклонения, суммы и средние,
    пороги из текста запроса и лишь затем ячейки больших таблиц — в тысячах
    строк случайное совпадение числа слишком вероятно.
    """
    small_facts: list[Proof] = []
    python: list[Proof] = []
    counts: list[Proof] = []
    small_derived: list[Proof] = []
    aggregates: list[Proof] = []
    params: list[Proof] = []
    large_facts: list[Proof] = []
    large_derived: list[Proof] = []
    for rs in workspace.results.values():
        from_sql = rs.source != "python"
        small = rs.row_count <= SMALL_RESULT
        for index, name in enumerate(rs.columns):
            derived = from_sql and bool(DERIVED_COLUMN_RE.search(name or ""))
            present = []
            for row in rs.rows:
                value = _as_float(row[index]) if index < len(row) else None
                if value is None:
                    continue
                present.append(value)
                if not from_sql:
                    python.append(Proof(value, CALC, rs.id, "python", rs.purpose or name))
                elif derived:
                    (small_derived if small else large_derived).append(Proof(value, CALC, rs.id, "column", name))
                else:
                    (small_facts if small else large_facts).append(Proof(value, FACT, rs.id, "cell", name))
            if present:
                aggregates.append(Proof(sum(present), CALC, rs.id, "sum", name))
                aggregates.append(Proof(sum(present) / len(present), CALC, rs.id, "mean", name))
        # «855 из 1843 объектов»: число строк и заполненных значений — тоже факт данных.
        count_kind = FACT if from_sql else CALC
        counts.append(Proof(float(rs.row_count), count_kind, rs.id, "count"))
        for index in range(len(rs.columns)):
            column = [_as_float(row[index]) if index < len(row) else None for row in rs.rows]
            if any(v is not None for v in column):
                filled = sum(1 for v in column if v is not None)
                counts.append(Proof(float(filled), count_kind, rs.id, "count"))
                counts.append(Proof(float(rs.row_count - filled), count_kind, rs.id, "count"))
    for step in workspace.steps:
        if step.output:
            for value in numbers_in_text(step.output):
                python.append(Proof(value, CALC, step.result_id or "", "python", step.purpose or step.label))
        # Пороги и константы расчёта («не меньше 500 чеков», «медиана × 1,5»)
        # законно берутся из самого запроса или кода, а не из данных.
        for source in (step.sql, step.code):
            if source:
                for value in numbers_in_text(source):
                    params.append(Proof(value, PARAM, step.result_id or "", "param"))
    return small_facts + python + counts + small_derived + aggregates + params + large_facts + large_derived


def pair_proofs(workspace: Workspace, max_rows: int = MAX_PAIR_VALUES):
    """Разности и отношения внутри строки (сравнение колонок) и внутри колонки (динамика).

    Генератор: попарных значений много, объекты создаются по мере перебора.
    """
    for rs in workspace.results.values():
        if rs.row_count > max_rows:
            continue
        numeric: list[tuple[str, list[float | None]]] = []
        for index, name in enumerate(rs.columns):
            column = [_as_float(row[index]) if index < len(row) else None for row in rs.rows]
            if any(v is not None for v in column):
                numeric.append((name, column))
        if rs.row_count > MAX_PAIR_VALUES or len(numeric) > 12:
            continue
        for row_index in range(rs.row_count):
            cells = [(f"«{name}»", column[row_index]) for name, column in numeric if column[row_index] is not None]
            yield from _pair_items(cells, rs.id)
        for name, column in numeric:
            cells = [(f"«{name}» {_row_label(rs, i)}", v) for i, v in enumerate(column) if v is not None][:60]
            yield from _pair_items(cells, rs.id)


def _pair_items(cells: list[tuple[str, float]], source: str):
    for i, (la, a) in enumerate(cells):
        for lb, b in cells[i + 1:]:
            yield Proof(a - b, CALC, source, "diff", a=a, b=b, la=la, lb=lb)
            for x, y, lx, ly in ((a, b, la, lb), (b, a, lb, la)):
                if y:
                    yield Proof((x / y - 1.0) * 100.0, CALC, source, "pct", a=x, b=y, la=lx, lb=ly)
                    yield Proof(x / y * 100.0, CALC, source, "share", a=x, b=y, la=lx, lb=ly)
                    yield Proof(x / y, CALC, source, "ratio", a=x, b=y, la=lx, lb=ly)


def evidence(workspace: Workspace, extra_text: Iterable[str] = ()) -> list[float]:
    """Множество чисел, которыми можно подтвердить текст ответа.

    Вывод вычислений, пороги из SQL и кода, значения результатов, их суммы
    и средние, число строк, а для небольших таблиц — разности и отношения
    внутри строки и внутри колонки («−5,3 %», «на 1 200 меньше»).
    """
    values: list[float] = []
    for text in extra_text:
        values.extend(numbers_in_text(text or ""))
    values.extend(p.value for p in base_proofs(workspace))
    values.extend(p.value for p in pair_proofs(workspace))
    return values


class Locator:
    """Находит самое прямое объяснение числа: сначала ячейки, потом производные."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.base = base_proofs(workspace)
        self._cache: dict[float, Proof | None] = {}
        self._pairs: dict[tuple, Proof | None] = {}

    def locate(self, value: float, with_pairs: bool = True) -> Proof | None:
        """Сначала точное совпадение (с точностью текста), затем приближённое:
        точная разность двух ячеек объясняет число лучше, чем случайно близкая ячейка."""
        key = round(value, 6)
        if key in self._cache and (self._cache[key] is not None or not with_pairs):
            return self._cache[key]
        found = next((p for p in self.base if _exact(value, p.value)), None)
        if found is None and with_pairs:
            found = next((p for p in pair_proofs(self.workspace) if _exact(value, p.value)), None)
        if found is None:
            found = next((p for p in self.base if _close(value, p.value)), None)
        if found is None and with_pairs:
            found = next((p for p in pair_proofs(self.workspace) if _close(value, p.value)), None)
        self._cache[key] = found
        return found

    def arithmetic(self, value: float, percent: bool) -> Proof | None:
        """Явная арифметика над ячейками небольших таблиц — чтобы показать формулу.

        Только точное совпадение и только подходящая операция: для процентов —
        темп или доля, для остальных чисел — разность; иначе совпадение
        случайно и формула ввела бы в заблуждение.
        """
        key = (round(value, 6), percent)
        if key not in self._pairs:
            ops = ("pct", "share") if percent else ("diff",)
            self._pairs[key] = next(
                (p for p in pair_proofs(self.workspace, max_rows=ARITHMETIC_ROWS)
                 if p.op in ops and _exact(value, p.value)), None)
        return self._pairs[key]


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
    why = filter_list(analysis.why)
    recs = []
    for rec in analysis.recs:
        if all(ok(str(rec.get(name) or "")) for name in REC_TEXT_FIELDS):
            recs.append(rec)
        else:
            removed.append(str(rec.get("action") or ""))
    cleaned = Analysis(
        headline=headline,
        happened=filter_list(analysis.happened),
        why=why,
        where=filter_list(analysis.where),
        actions=[str(r.get("action") or "") for r in recs] if analysis.recs else filter_list(analysis.actions),
        limitations=list(analysis.limitations),
        checks={t: c for t, c in analysis.checks.items() if t in why},
        recs=recs,
    )
    if not cleaned.headline:
        cleaned.headline = cleaned.happened[0] if cleaned.happened else "Ответ сформирован по данным ниже."
    if removed:
        cleaned.limitations.append(UNVERIFIED_NOTE)
    return cleaned, removed


def _sentences(analysis: Analysis) -> list[str]:
    actions = ([str(rec.get(name) or "") for rec in analysis.recs for name in REC_TEXT_FIELDS]
               if analysis.recs else list(analysis.actions))
    return [analysis.headline, *analysis.happened, *analysis.why, *analysis.where, *actions]


def _format(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")
