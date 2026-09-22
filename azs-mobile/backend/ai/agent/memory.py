"""Память диалога: рамки прошлых вопросов и эвристики разбора.

Рамка (frame) — компактное описание того, о чём был вопрос: показатели,
период, фильтры, главный вывод. Она хранится вместе с ответом и подаётся
разбору следующего вопроса, чтобы «а только Москва?» стал полным вопросом.
Переписки целиком модели не передаётся: только рамки последних ходов.
"""
from __future__ import annotations

import re

MAX_TURNS = 6

FOLLOWUP_STARTS = ("а ", "и ", "только", "теперь", "тогда", "также", "ещё", "еще", "то же", "тоже", "но ")
FOLLOWUP_WORDS = {"это", "этого", "этот", "эти", "там", "их", "него", "неё", "нее", "них", "они", "оно",
                  "она", "он", "такое", "такой", "таких", "тех", "той", "том", "почему"}

ANALYSIS_WORDS = (
    "почему", "причин", "сравн", "относительно", "по сравнению", "динамик", "тренд", "изменил", "измени",
    "снизил", "упал", "вырос", "рост", "падени", "аномал", "выброс", "хуже", "лучше", "потенциал",
    "точки роста", "что если", "что будет", "сценар", "прогноз", "сезон", "структур", "вклад", "драйвер",
    "график", "постро", "просел", "провал", "отстают", "лидер", "разрыв", "медиан", "перцентил",
    "корреляц", "зависим", "эластичн", "сильнее всего", "больше всего", "меньше всего",
)
LOOKUP_STARTS = ("сколько", "какая", "какой", "какое", "каков", "покажи", "выведи", "дай", "назови", "что такое",
                 "есть ли", "выручка", "объем", "объём", "количество", "число", "средн", "топ", "список")


def is_followup(question: str) -> bool:
    text = " ".join((question or "").lower().split())
    if not text:
        return False
    if text.startswith(FOLLOWUP_STARTS):
        return True
    words = set(re.findall(r"[а-яёa-z]+", text))
    if words & FOLLOWUP_WORDS and len(words) <= 8:
        return True
    return len(words) <= 3


def looks_like_lookup(question: str) -> bool:
    """Очевидный факт-вопрос: одно число или простая выборка без анализа."""
    text = " ".join((question or "").lower().split())
    if any(word in text for word in ANALYSIS_WORDS):
        return False
    return text.startswith(LOOKUP_STARTS) or len(text.split()) <= 6


def frame_from_answer(question: str, answer: dict) -> dict:
    """Рамка хода по сохранённому ответу — своя для агента и для FAST."""
    existing = answer.get("frame") if isinstance(answer, dict) else None
    if isinstance(existing, dict) and existing:
        return existing
    frame = {"question": question, "standalone": question}
    if not isinstance(answer, dict):
        return frame
    if answer.get("sql"):
        frame["sql"] = " ".join(str(answer["sql"]).split())[:400]
    if answer.get("summary"):
        frame["headline"] = str(answer["summary"])[:240]
    if answer.get("columns"):
        frame["columns"] = list(answer["columns"])[:8]
    if answer.get("error"):
        frame["outcome"] = "отказ: " + str(answer.get("rule") or "")
    return frame


def history_block(history: list[dict], limit: int = MAX_TURNS) -> str:
    """Текст для разбора: последние ходы, от старых к новым."""
    lines = []
    for turn in (history or [])[-limit:]:
        frame = turn.get("frame") or frame_from_answer(turn.get("question", ""), turn.get("answer") or {})
        piece = f"- Вопрос: {turn.get('question', '')}"
        standalone = frame.get("standalone")
        if standalone and standalone != turn.get("question"):
            piece += f" (полностью: {standalone})"
        details = []
        if frame.get("metrics"):
            details.append("показатели: " + ", ".join(map(str, frame["metrics"])))
        if frame.get("period"):
            details.append("период: " + str(frame["period"]))
        if frame.get("filters"):
            details.append("фильтры: " + ", ".join(map(str, frame["filters"])))
        if frame.get("headline"):
            details.append("вывод: " + str(frame["headline"])[:160])
        if frame.get("outcome"):
            details.append(str(frame["outcome"]))
        if details:
            piece += " — " + "; ".join(details)
        lines.append(piece)
    return "\n".join(lines)


def build_frame(question: str, plan, analysis, main_sql: str | None) -> dict:
    frame = {
        "question": question,
        "standalone": plan.standalone_question or question,
        "taskType": plan.task_type,
        "metrics": list(plan.metrics)[:6],
        "period": plan.period,
        "filters": list(plan.filters)[:6],
    }
    if analysis is not None and getattr(analysis, "headline", ""):
        frame["headline"] = analysis.headline[:240]
    if main_sql:
        frame["sql"] = " ".join(main_sql.split())[:400]
    return frame
