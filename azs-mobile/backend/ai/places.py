"""Места в вопросе: регион, общество (ОНПО), город — точные значения справочника витрины.

28.09.2026, скорость «Среднего» и «Высокого». По журналу модель тратила 3–8 ходов на
поиск написания места («Уточнил значения измерения: region», «Искал в витрине: Сочи
город»), а каждый ход — это несколько секунд и рост переписки. Теперь код до обращения
к модели сверяет слова вопроса со справочником измерений и даёт модели точные значения
для фильтра — как `people.py` для РУ и ТМ.

Справочник берётся тем же запросом, что и в инструменте get_dimension_values, через
валидатор с областью данных пользователя: значения чужих объектов резолвер не видит.
Кэш — по области данных на AI_PLACES_TTL секунд (по умолчанию 30 минут). Справочник
запрашивается, только если в вопросе есть слово с заглавной буквы (кроме первого) или
аббревиатура; иначе — только по уже прогретому кэшу. Ошибка справочника не мешает
вопросу: он идёт без подсказки. AI_PLACES=0 выключает резолвер.

Сопоставление — по основе слова без падежного окончания («Пермском крае» → «Пермский
край», «в Перми» → «г. Пермь», «Нижнем Новгороде» → «г. Нижний Новгород»): у значения
должны совпасть все значимые слова (кроме «край», «обл.», «г.», «Республика» и т. п.).
Аббревиатура («ЦНП») — только точное совпадение. Где городов в витрине нет (ОХД),
город указывает на свой регион: «в Перми» → регион «Пермский край» с пометкой, что это
все АЗС региона.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable

from . import executor
from .catalog import CATALOG, Catalog
from .people import _CASE_ENDINGS
from .semantic import SEMANTIC, Semantic
from .validator import Scope, validate

ENABLED = (os.environ.get("AI_PLACES") or "1").strip() not in {"0", "false", "no", "off"}
TTL_SECONDS = float(os.environ.get("AI_PLACES_TTL", "1800"))
PLACE_DIMENSIONS = ("region", "npo", "city")
VALUES_LIMIT = 3000
MAX_PER_WORD = 3
MAX_LINES = 6

GENERIC = {
    "край", "области", "область", "обл", "республика", "респ", "автономный", "автономная", "автономного",
    "округ", "город", "г", "федеральный", "федерации", "федерация", "район", "ооо", "пао", "оао", "зао",
    "лукойл", "онпо", "нпо", "общество", "филиал", "россия", "российская", "регион", "прочее", "прочие",
    "нет", "данных",
}
# Аббревиатуры вопросов, которые не бывают местами: из-за них справочник не запрашивается.
COMMON_ABBR = {"азс", "нту", "тм", "ру", "онпо", "нпо", "кссс", "вд", "ооо", "пао", "sql", "kpi", "ии", "ссп",
               "ук", "ндс", "руб", "млн", "тыс"}
ENDINGS = set(_CASE_ENDINGS) | {"ое", "ие", "ые", "ью", "ь", "ем"}
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")


@dataclass(frozen=True)
class Place:
    word: str            # слово вопроса, как написано
    dimension: str       # ключ измерения: region | npo | city
    title: str           # название измерения для подсказки
    column: str
    table: str
    value: str           # точное значение справочника
    objects: int         # объектов в области данных пользователя
    via_city: bool = False   # город указал на свой регион (городов в витрине нет)


@dataclass
class Resolution:
    places: list[Place]

    def __bool__(self) -> bool:
        return bool(self.places)

    def prompt_block(self) -> str:
        lines = ["Места из вопроса — точные значения справочника витрины в области данных пользователя "
                 "(найдены кодом; для фильтра бери как есть, get_dimension_values по ним не нужен):"]
        for place in self.places[:MAX_LINES]:
            value = place.value.replace("'", "''")
            where = f"{place.title}: {place.table}.{place.column} = '{value}' ({place.objects} АЗС)"
            if place.via_city:
                where = f"городов в витрине нет; {where} — это все АЗС региона, скажи об этом в ответе"
            lines.append(f"  • «{place.word}» → {where}")
        lines.append("Если места нет в списке или совпадение не то — уточни через get_dimension_values.")
        return "\n".join(lines)

    def as_dict(self) -> list[dict]:
        return [{"word": p.word, "dimension": p.dimension, "value": p.value, "viaCity": p.via_city}
                for p in self.places]


def _norm(text: str) -> str:
    return (text or "").strip().lower().replace("ё", "е")


def base(word: str) -> str:
    """Основа слова без окончания: «Пермский» → «пермск», «Пермь» → «перм», «Москва» → «москв»."""
    w = _norm(word)
    for ending, replacement in (("ская", "ск"), ("ский", "ск"), ("ской", "ск"), ("ское", "ск"), ("ские", "ск"),
                                ("цкая", "цк"), ("цкий", "цк")):
        if w.endswith(ending) and len(w) > len(ending) + 2:
            return w[: -len(ending)] + replacement
    if len(w) > 5 and w.endswith(("ой", "ый", "ий", "ая", "ое", "ие", "ые")):
        return w[:-2]
    if len(w) > 4 and w.endswith(("а", "я", "ь", "й", "ы", "и", "е", "о", "у")):
        return w[:-1]
    return w


def _is_abbr(word: str) -> bool:
    return 2 <= len(word) <= 6 and word.isupper()


def _matches(word: str, stem: str) -> bool:
    """Слово вопроса — это основа значения с падежным окончанием. Строчное — только при длинной основе."""
    token = _norm(word)
    if len(stem) < 4 or not token.startswith(stem) or token[len(stem):] not in ENDINGS:
        return False
    return word[:1].isupper() or len(stem) >= 6


def _significant(value: str) -> list[str]:
    words = _WORD_RE.findall(value or "")
    return [w for w in words if _norm(w) not in GENERIC and (len(w) >= 4 or _is_abbr(w))]


def candidates(question: str) -> list[str]:
    """Слова, ради которых стоит спросить справочник: с заглавной (кроме первого слова) и аббревиатуры."""
    words = _WORD_RE.findall(question or "")
    out = []
    for index, word in enumerate(words):
        if _norm(word) in COMMON_ABBR or _norm(word) in GENERIC:
            continue
        if _is_abbr(word) or (index > 0 and word[:1].isupper() and len(word) >= 4):
            out.append(word)
    return out


_cache: dict = {}


def _dimensions(semantic: Semantic) -> list:
    return [semantic.dimensions[key] for key in PLACE_DIMENSIONS
            if key in semantic.dimensions and semantic.dimensions[key].kind == "category"]


def directory(scope: Scope, catalog: Catalog, semantic: Semantic,
              run_query: Callable[[str, int], executor.Result] | None = None, fetch: bool = True) -> dict | None:
    """Значения мест в области данных: {ключ измерения: [(значение, объектов), …]}; None — кэш холодный."""
    run_query = run_query or executor.run
    key = (catalog.source, id(catalog), semantic.profile, scope.unrestricted, scope.ksss)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        return cached[1]
    if not fetch:
        return None
    values: dict[str, list[tuple[str, int]]] = {}
    for dim in _dimensions(semantic):
        if dim.column not in catalog.tables.get(dim.table, set()):
            continue
        entity = catalog.scope_columns.get(dim.table, catalog.scope_column)
        counter = f"COUNT(DISTINCT {entity})" if entity in catalog.tables.get(dim.table, set()) else "COUNT(*)"
        sql = (f"SELECT {dim.column} AS value, {counter} AS objects FROM {catalog.qualified(dim.table)} "
               f"WHERE {dim.column} IS NOT NULL GROUP BY {dim.column} ORDER BY objects DESC")
        checked = validate(sql, scope, row_limit=VALUES_LIMIT)
        result = run_query(checked.sql, checked.row_limit)
        values[dim.key] = [(str(v).strip(), int(n or 0)) for v, n in result.rows if str(v or "").strip()]
    _cache[key] = (time.monotonic(), values)
    return values


def clear_cache() -> None:
    _cache.clear()


def find(question: str, values: dict, semantic: Semantic, catalog: Catalog) -> list[Place]:
    """Слова вопроса, совпавшие со значениями мест (все значимые слова значения — в вопросе)."""
    words = _WORD_RE.findall(question or "")
    has_city = "city" in values and bool(values["city"])
    found: list[Place] = []
    seen = set()
    for dim in _dimensions(semantic):
        rows = values.get(dim.key) or []
        table = catalog.qualified(dim.table)
        for value, objects in rows:
            parts = _significant(value)
            if not parts:
                continue
            hits = []
            for part in parts:
                if _is_abbr(part):
                    hit = next((w for w in words if w.upper() == part.upper() and _is_abbr(w)), None)
                else:
                    stem = base(part)
                    hit = next((w for w in words if _matches(w, stem)), None)
                if hit is None:
                    break
                hits.append(hit)
            else:
                word = " ".join(dict.fromkeys(hits))
                if (dim.key, value) not in seen:
                    seen.add((dim.key, value))
                    found.append(Place(word, dim.key, dim.title, dim.column, table, value, objects))
                continue
            # Городов в витрине нет: «в Перми» указывает на «Пермский край» (основа без «ск»).
            if dim.key == "region" and not has_city and len(parts) == 1:
                stem = base(parts[0])
                if stem.endswith(("ск", "цк")) and len(stem) >= 6:
                    city = stem[:-2]
                    hit = next((w for w in words if w[:1].isupper() and _matches(w, city)), None)
                    if hit and (dim.key, value) not in seen:
                        seen.add((dim.key, value))
                        found.append(Place(hit, dim.key, dim.title, dim.column, table, value, objects, via_city=True))
    # На одно слово — не больше MAX_PER_WORD значений каждого измерения (самые крупные).
    by_word: dict[tuple[str, str], list[Place]] = {}
    for place in found:
        by_word.setdefault((place.word, place.dimension), []).append(place)
    out = []
    for group in by_word.values():
        out += sorted(group, key=lambda p: -p.objects)[:MAX_PER_WORD]
    return out[:MAX_LINES]


def resolve(question: str, scope: Scope, catalog: Catalog | None = None, semantic: Semantic | None = None,
            run_query: Callable[[str, int], executor.Result] | None = None) -> Resolution | None:
    """Места в вопросе. None — мест не названо, резолвер выключен или справочник недоступен."""
    if not ENABLED:
        return None
    catalog = catalog or CATALOG
    semantic = semantic or SEMANTIC
    try:
        values = directory(scope, catalog, semantic, run_query, fetch=bool(candidates(question)))
        if not values:
            return None
        places = find(question, values, semantic, catalog)
    except Exception:  # noqa: BLE001 - справочник недоступен: вопрос идёт без подсказки
        return None
    return Resolution(places) if places else None
