"""Единый стиль текста ответа ИИ: русские названия, без технических скобок.

Модель иногда пишет имена колонок латиницей («vd_per_client»), период
и числа в скобках («(сентябрь 2026)», «(71.46%)») и десятичную точку.
Код приводит текст к одному виду, не меняя смысла и чисел:

- имена колонок и показателей латиницей → русское название из витрины;
  в скобках — убираются, неизвестные технические имена — тоже;
- «(сентябрь 2026)» → «в сентябре 2026», «(2026-09)» → «в сентябре 2026»;
- «(71.46%)» → «— 71,46 %», после числа — «, или −23,5 %»;
- минус перед числом — типографский «−»;
- десятичная запятая и пробел перед «%».

Сверка чисел (grounding) и разметка типов делаются до этой правки: числа
остаются теми же, меняется только запись.
"""
from __future__ import annotations

import json
import re

LATIN_ID = r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+"
LATIN_ID_RE = re.compile(r"(?<![\w-])" + LATIN_ID + r"(?![\w-])")
LATIN_PAREN_RE = re.compile(r"\s*\(\s*" + LATIN_ID + r"(?:\s*[,;/]\s*" + LATIN_ID + r")*\s*\)")
MONTHS = (
    ("январ", "январе"), ("феврал", "феврале"), ("март", "марте"), ("апрел", "апреле"),
    ("ма", "мае"), ("июн", "июне"), ("июл", "июле"), ("август", "августе"),
    ("сентябр", "сентябре"), ("октябр", "октябре"), ("ноябр", "ноябре"), ("декабр", "декабре"),
)
MONTH_WORDS = r"январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*"
PERIOD_PAREN_RE = re.compile(r"\s*\(\s*(" + MONTH_WORDS + r")\s+(\d{4})(?:\s*г(?:ода|\.)?)?\s*\)", re.IGNORECASE)
ISO_PAREN_RE = re.compile(r"\s*\(\s*(\d{4})-(\d{2})\s*\)")
UNIT = r"%|п\.\s?п\.|₽|руб\.?|тыс\.\s?₽|млн\s?₽|млрд\s?₽|шт\.?|л|т"
# Только в конце фразы: «…лидеров (71,46 %).» Посреди фразы скобки оставляем — там тире ломает смысл.
NUMBER_PAREN_RE = re.compile(r"\s*\(\s*([-−–]?\d[\d  ]*(?:[.,]\d+)?\s*(?:" + UNIT + r")?)\s*\)(?=\s*[.,;:!?]|\s*$)")
DECIMAL_RE = re.compile(r"(?<![\w.,])(\d+)\.(\d+)(?![\w]|\.\d)")
PERCENT_RE = re.compile(r"(\d)\s?%")
MINUS_RE = re.compile(r"(?<![\w\d−–-])[-–](?=\d)")
JSONISH_RE = re.compile(r'^\s*[\[{]|"headline"\s*:')
DESCRIPTION_LINE_RE = re.compile(r"^\s+([a-z_][a-z0-9_]*)\s+[a-z][a-z0-9_ ]*(?:\(\d+(?:,\d+)?\))?\s{2,}(.+?)\s*$")


_CACHE: dict[tuple[int, int], tuple[dict, dict]] = {}


def _glossary_for(semantic, catalog) -> tuple[dict, dict]:
    """Два словаря: для текста (имя → название) и для колонок (имя → «название, единица»)."""
    key = (id(semantic), id(catalog))
    if key in _CACHE:
        return _CACHE[key]
    text: dict[str, str] = {}
    columns: dict[str, str] = {}
    description = getattr(catalog, "description", "") or ""
    for line in description.splitlines():
        match = DESCRIPTION_LINE_RE.match(line)
        if match:
            name, title = match.group(1).lower(), match.group(2).strip()
            columns.setdefault(name, title)
            text.setdefault(name, title.split(",")[0].strip())
    for dim in getattr(semantic, "dimensions", {}).values():
        for name in (dim.key, dim.column):
            if name:
                text[name.lower()] = dim.title
                columns[name.lower()] = dim.title
    for metric in getattr(semantic, "metrics", {}).values():
        text[metric.key.lower()] = metric.title
        unit = (metric.unit or "").strip()
        columns[metric.key.lower()] = f"{metric.title}, {unit}" if unit and unit not in metric.title else metric.title
    _CACHE[key] = (text, columns)
    return text, columns


def glossary(semantic=None, catalog=None) -> tuple[dict, dict]:
    if semantic is None or catalog is None:
        from .catalog import CATALOG
        from .semantic import SEMANTIC
        semantic = semantic or SEMANTIC
        catalog = catalog or CATALOG
    return _glossary_for(semantic, catalog)


def column_title(name: str, semantic=None, catalog=None) -> str:
    """Русский заголовок колонки: «vd_per_client» → «ВД на клиента, руб/чек»; прочие — как есть."""
    _, columns = glossary(semantic, catalog)
    return columns.get(str(name or "").strip().lower(), name)


def rename_columns(names: list[str], semantic=None, catalog=None) -> list[str]:
    """Заголовки результата по-русски; повторы не допускаются (к повтору дописывается имя)."""
    out: list[str] = []
    for name in names:
        title = column_title(name, semantic, catalog)
        if title in out:
            title = name
        out.append(title)
    return out


def _month_phrase(word: str, year: str) -> str:
    lowered = word.lower()
    for stem, prepositional in MONTHS:
        if lowered.startswith(stem):
            return f" в {prepositional} {year}"
    return f" в {word} {year}"


def clean(text: str, semantic=None, catalog=None) -> str:
    """Привести текст пункта к единому стилю (см. описание модуля)."""
    if not text:
        return text
    words, _ = glossary(semantic, catalog)
    s = str(text)
    s = LATIN_PAREN_RE.sub("", s)

    def latin(match: re.Match) -> str:
        title = words.get(match.group(0).lower()) or ""
        before = match.string[:match.start()].rstrip()
        mid_sentence = bool(before) and before[-1] not in ".!?:—-"
        if title and mid_sentence and title[1:2].islower():
            title = title[0].lower() + title[1:]
        return title

    s = LATIN_ID_RE.sub(latin, s)
    s = PERIOD_PAREN_RE.sub(lambda m: _month_phrase(m.group(1), m.group(2)), s)
    s = ISO_PAREN_RE.sub(lambda m: _month_phrase(MONTHS[int(m.group(2)) - 1][0] if 1 <= int(m.group(2)) <= 12 else m.group(2),
                                                 m.group(1)), s)
    def number_paren(match: re.Match) -> str:
        before = match.string[:match.start()].rstrip()
        # «на −4 825 626 (−23,5 %)» → «на −4 825 626, или −23,5 %»; «лидеров (71,46 %)» → «лидеров — 71,46 %».
        joiner = ", или " if before[-1:].isdigit() or before.endswith(("%", "₽", "шт", "руб.")) else " — "
        return joiner + match.group(1).strip()

    s = NUMBER_PAREN_RE.sub(number_paren, s)
    s = MINUS_RE.sub("−", s)
    s = DECIMAL_RE.sub(r"\1,\2", s)
    s = PERCENT_RE.sub(r"\1 %", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"([,;:])(?=[^\s\d])", r"\1 ", s)
    s = re.sub(r"\(\s*\)", "", s)
    return s.strip()


def looks_like_json(text: str) -> bool:
    return bool(JSONISH_RE.search(text or ""))


def headline(text: str, fallback: str = "", semantic=None, catalog=None) -> str:
    """Заголовок: одна-две фразы, без JSON и технических скобок, с точкой в конце."""
    s = (text or "").strip()
    if not s or looks_like_json(s):
        s = (fallback or "").strip()
    s = clean(s, semantic, catalog)
    sentences = re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«])", s)
    s = " ".join(sentences[:2]).strip()
    if len(s) > 320:
        s = s[:319].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
    if s and s[-1] not in ".!?…":
        s += "."
    return s


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
