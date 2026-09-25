"""Разметка утверждений ответа (ИИ-25): факт, расчёт, гипотеза, рекомендация.

Тип ставит код по происхождению чисел, а не модель:

- «Факт» — числа пункта — ячейки или число строк результата запроса
  к витрине; по нажатию видно, из какого результата и какого столбца;
- «Расчёт» — хотя бы одно число получено из данных: разность, отношение,
  сумма, столбец-отклонение в запросе, вывод Python или порог расчёта;
  показывается формула с исходными числами;
- «Гипотеза» — пункт «почему», пункт с причинной или предположительной
  формулировкой и пункт без чисел, который не привязать к результату;
  числа в гипотезе сверены с данными так же, как в фактах (grounding);
- «Рекомендация» — пункт «что можно сделать», правила ИИ-16 в recommend.py.

Факта без ссылки на результат не бывает: такой пункт становится гипотезой,
а пункт с неподтверждённым числом снимается.
"""
from __future__ import annotations

import re
from collections import Counter

from . import grounding, recommend
from .state import Analysis, Workspace

FACT, CALC, HYPOTHESIS, RECOMMENDATION = "fact", "calc", "hypothesis", "recommendation"
TYPES = (FACT, CALC, HYPOTHESIS, RECOMMENDATION)
TITLES = {FACT: "Факт", CALC: "Расчёт", HYPOTHESIS: "Гипотеза", RECOMMENDATION: "Рекомендация"}
SECTIONS = ("happened", "why", "where")

# Предположение или причинная связь: такие пункты — гипотезы, где бы они ни стояли.
HYPOTHESIS_RE = re.compile(
    r"(?<![\w-])(возможно|вероятно|предположительно|по-видимому|видимо|скорее всего|"
    r"может|могут|мог|могла|могло|могли|из-за|вследствие|благодаря|"
    r"объясня\w*|связан\w*|причин\w*|вызван\w*|повлия\w*|обусловлен\w*|привел\w*|привёл|сказал\w*)(?![\w-])",
    re.IGNORECASE,
)
CHECK_WHY = "Подтвердит: тот же расчёт по группе сопоставимых АЗС и за тот же период прошлого года."
CHECK_FREE = "В пункте нет чисел и он не привязан к результату — сверьте его с таблицей ответа."
NOTE_CAUSE = "Совместное движение показателей не доказывает причину."
NOTE_LINKED = "Чисел в пункте нет: он опирается на указанный результат."
MAX_ENTITIES = 400


class Match:
    """Число из текста, признак процента и происхождение числа."""

    __slots__ = ("value", "percent", "proof")

    def __init__(self, value: float, percent: bool, proof):
        self.value, self.percent, self.proof = value, percent, proof

    @property
    def kind(self) -> str:
        return self.proof.kind


class Context:
    """Всё, что нужно для разметки: результаты, их подписи, поиск чисел."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.locator = grounding.Locator(workspace)
        self.entities: list[tuple[str, str]] = []
        self.stems: list[tuple[str, str]] = []
        for rs in workspace.results.values():
            if rs.source in ("python", "file_text") or not rs.rows:
                continue
            seen = 0
            for row in rs.rows:
                for value in row:
                    if isinstance(value, str) and grounding._as_float(value) is None and len(value.strip()) >= 3:
                        self.entities.append((value.strip().lower(), rs.id))
                        seen += 1
                if seen >= MAX_ENTITIES:
                    break
            for name in rs.columns:
                stem = (name or "").split(",")[0].strip().lower()
                if len(stem) >= 4:
                    self.stems.append((stem, rs.id))

    def source(self, result_id: str) -> dict:
        rs = self.workspace.results.get((result_id or "").lower())
        if rs is None:
            return {"id": result_id, "title": "вычисление", "kind": "python"}
        title = rs.purpose or ("вычисление" if rs.source == "python" else
                               "файл" if rs.source in ("file", "file_text") else "запрос к витрине")
        return {"id": rs.id, "title": title, "kind": rs.source, "rows": rs.row_count}

    def sources_of(self, found) -> list[dict]:
        ids = list(dict.fromkeys(m.proof.source for m in found if m.proof.source))
        return [self.source(i) for i in ids]

    def numbers(self, text: str):
        """Происхождение чисел пункта и сколько из них не нашлось."""
        found: list[Match] = []
        unmatched = 0
        for value, percent in grounding.numbers_with_units(self.masked(text)):
            if grounding.YEAR_MIN <= value <= grounding.YEAR_MAX and float(value).is_integer() and not percent:
                continue
            # Малые целые сверка не проверяет («6 месяцев», «3 региона»): их тип
            # берём только по прямому совпадению, проценты — по любому.
            trivial = grounding._trivial(value, text)
            proof = self.locator.locate(value, with_pairs=not trivial or percent)
            if proof is None:
                unmatched += 0 if trivial else 1
                continue
            if trivial and not percent and proof.kind != FACT:
                continue
            found.append(Match(value, percent, proof))
        return found, unmatched

    def masked(self, text: str) -> str:
        """Текст без названий объектов из результатов: «АЗС № 01003» — не число."""
        lowered = (text or "").lower()
        out = text or ""
        for value, _ in self.entities:
            if value in lowered and any(ch.isdigit() for ch in value):
                start = lowered.find(value)
                while start >= 0:
                    out = out[:start] + " " * len(value) + out[start + len(value):]
                    lowered = lowered[:start] + " " * len(value) + lowered[start + len(value):]
                    start = lowered.find(value)
        return out

    def formula(self, match: "Match") -> str:
        """Формула для расчёта: явная арифметика над ячейками, если она есть."""
        proof = match.proof
        if proof.op in ("python", "sum", "mean", "column"):
            explicit = self.locator.arithmetic(match.value, match.percent)
            if explicit is not None:
                return explicit.formula(match.value)
        return proof.formula(match.value)

    def link(self, text: str) -> list[dict]:
        """Пункт без чисел: объект или показатель из результата, названный в тексте."""
        lowered = (text or "").lower()
        ids = [rid for value, rid in self.entities if value in lowered]
        if not ids:
            ids = [rid for stem, rid in self.stems if stem in lowered]
        return [self.source(i) for i in dict.fromkeys(ids)][:2]

    def classify(self, text: str, section: str) -> dict | None:
        found, unmatched = self.numbers(text)
        if unmatched:
            return None
        sources = self.sources_of(found)
        if section == "why" or HYPOTHESIS_RE.search(text):
            claim = {"type": HYPOTHESIS, "sources": sources}
            if section == "why":
                claim["note"] = NOTE_CAUSE
            return claim
        derived = [m for m in found if m.kind in (grounding.CALC, grounding.PARAM)]
        if derived:
            formulas = list(dict.fromkeys(f for f in (self.formula(m) for m in derived) if f))
            facts = list(dict.fromkeys(m.proof.label for m in found if m.kind == FACT and m.proof.label))
            claim = {"type": CALC, "sources": sources, "formula": formulas[:3]}
            if facts:
                claim["columns"] = facts[:3]
            return claim
        if found:
            columns = list(dict.fromkeys(m.proof.label for m in found if m.proof.label))
            claim = {"type": FACT, "sources": sources}
            if columns:
                claim["columns"] = columns[:3]
            return claim
        linked = self.link(text)
        if linked:
            return {"type": FACT, "sources": linked, "note": NOTE_LINKED}
        return {"type": HYPOTHESIS, "sources": [], "check": CHECK_FREE}


def annotate(analysis: Analysis, workspace: Workspace, asked: bool = True) -> dict:
    """Проставить типы пунктам и проверить рекомендации; вернуть сводку для журнала.

    `asked` — просил ли пользователь рекомендаций (recommend.requested).
    """
    ctx = Context(workspace)
    claims: dict[str, list[dict]] = {}
    dropped = 0
    for section in SECTIONS:
        kept, marks = [], []
        for text in getattr(analysis, section):
            claim = ctx.classify(text, section)
            if claim is None:
                dropped += 1
                continue
            if claim["type"] == HYPOTHESIS and "check" not in claim:
                claim["check"] = analysis.checks.get(text) or CHECK_WHY
            kept.append(text)
            marks.append(claim)
        setattr(analysis, section, kept)
        claims[section] = marks
    if dropped and grounding.UNVERIFIED_NOTE not in analysis.limitations:
        analysis.limitations.append(grounding.UNVERIFIED_NOTE)

    items = analysis.recs or [{"action": a} for a in analysis.actions]
    review = recommend.review(items, ctx, asked=asked)
    analysis.recommendations = review.kept
    analysis.recs_asked = asked
    analysis.actions = [r["action"] for r in review.kept]
    analysis.withheld = len(review.withheld)
    analysis.claims = claims

    counts = Counter(c["type"] for marks in claims.values() for c in marks)
    counts[RECOMMENDATION] = len(review.kept)
    return {
        "counts": {t: counts.get(t, 0) for t in TYPES},
        "withheld": [dict(w) for w in review.withheld][:6],
        "asked": asked,
        "rulesVersion": recommend.rules().get("version", ""),
    }
