"""Кто назван в вопросе: руководитель управления (РУ) или территориальный менеджер (ТМ).

Модель не угадывает роль по фамилии — её определяет код до обращения к модели,
по справочникам самой витрины и в пределах области данных пользователя:

  * витрина ОХД — `bds.l_azs_rm_dt_vers` (РУ) и `bds.l_azs_tm_dt_vers` (ТМ);
  * стенд SQLite — поля `regional_manager` и `territory_manager` таблицы `stations`.

Правила владельца (23.09.2026), которые здесь зашиты:

  1. РУ и ТМ — разные уровни оргструктуры, их между собой не сравнивают.
     Все названные люди сравниваются на одном уровне.
  2. Если фамилия есть и среди РУ, и среди ТМ (однофамильцы), речь идёт о РУ.
     Общее правило: уровень выбирается общий для всех названных; если подходят
     оба — РУ.
  3. Если один назван только как РУ, а другой только как ТМ — сравнения нет:
     каждый считается отдельно, без сопоставления и ранжирования.

Результат — подсказка для модели (точные ФИО, роль, таблица и фильтр) и короткая
пометка для пользователя. Справочник людей кэшируется по области данных на
AI_PEOPLE_TTL секунд (по умолчанию 30 минут); запрос к нему идёт через тот же
валидатор с областью данных, поэтому чужих руководителей резолвер не видит.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import executor
from .catalog import CATALOG, Catalog
from .validator import Scope, validate

ROLE_RM = "РУ"
ROLE_TM = "ТМ"
ROLE_TITLES = {ROLE_RM: "руководитель управления", ROLE_TM: "территориальный менеджер"}

TTL_SECONDS = float(os.environ.get("AI_PEOPLE_TTL", "1800"))
DIRECTORY_LIMIT = 5000

# Окончания, которые допустимы после основы фамилии в вопросе: «Черепанов|ой»,
# «Шнайдер|а», «Петров|у», «Иванов|ыми».
_CASE_ENDINGS = {
    "", "а", "я", "у", "ю", "е", "ы", "и", "ой", "ей", "ою", "ом", "ем", "ым", "им", "ого", "его",
    "ому", "ему", "ую", "юю", "ая", "яя", "ий", "ый", "ых", "их", "ов", "ев", "ами", "ями", "ах",
    "ях", "ыми", "ими", "ам", "ям",
}
_WORD_RE = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё\-]+")


@dataclass(frozen=True)
class Person:
    fio: str
    role: str        # РУ или ТМ
    objects: int     # объектов в области данных пользователя (за всё время закрепления)


@dataclass
class Mention:
    word: str                              # как слово написано в вопросе
    people: list[Person] = field(default_factory=list)


@dataclass
class Resolution:
    level: str                             # РУ, ТМ или mixed
    mentions: list[Mention]
    chosen: dict[str, list[Person]]        # слово вопроса → люди, по которым считать
    sources: dict[str, tuple[str, str]]    # роль → (таблица, поле ФИО)
    namesakes_dropped: list[str] = field(default_factory=list)  # слова, где отброшен однофамилец другой роли

    def people(self) -> list[Person]:
        seen, out = set(), []
        for group in self.chosen.values():
            for person in group:
                if (person.fio, person.role) not in seen:
                    seen.add((person.fio, person.role))
                    out.append(person)
        return out

    def note(self) -> str:
        """Пометка для пользователя: кого и в какой роли нашёл контур."""
        parts = []
        for word, group in self.chosen.items():
            names = ", ".join(f"{p.fio} ({p.role})" for p in group)
            parts.append(f"«{word}» — {names}")
        text = "Руководители определены по справочникам: " + "; ".join(parts) + "."
        if self.level == "mixed":
            text += " РУ и ТМ — разные уровни: показатели приведены отдельно, без сравнения."
        elif len(self.people()) > 1:
            text += f" Сравнение на уровне {self.level}."
        return text

    def prompt_block(self) -> str:
        """Подсказка модели: роль уже определена, как соединять и чем фильтровать."""
        lines = ["ЛЮДИ В ВОПРОСЕ — уже определены по справочникам РУ и ТМ, роль заново не угадывай:"]
        for mention in self.mentions:
            group = self.chosen.get(mention.word, [])
            names = "; ".join(f"{p.role} {p.fio} (объектов: {p.objects})" for p in group)
            extra = ""
            if mention.word in self.namesakes_dropped:
                extra = " — однофамилец другого уровня не учитывается: по правилу речь о " + self.level
            if len(group) > 1 and len({p.role for p in group}) == 1:
                extra += " — несколько человек с такой фамилией: покажи каждого отдельно с полным ФИО"
            lines.append(f"  «{mention.word}» → {names}{extra}")
        if self.level in (ROLE_RM, ROLE_TM):
            table, column = self.sources[self.level]
            names = ", ".join(_quote(p.fio) for p in self.people())
            lines.append(
                f"Считай на уровне {self.level}: соединяй только {table} и фильтруй точным ФИО "
                f"{column} IN ({names}); группируй по {column}. "
                f"{'ТМ' if self.level == ROLE_RM else 'РУ'} не подмешивай — РУ и ТМ между собой не сравнивают."
            )
        else:
            lines.append("Названы руководители разного уровня. РУ и ТМ между собой не сравнивают: посчитай "
                         "каждого отдельно по его справочнику и в ответе не сопоставляй и не ранжируй их.")
            for role in (ROLE_RM, ROLE_TM):
                group = [p for p in self.people() if p.role == role]
                if group:
                    table, column = self.sources[role]
                    lines.append(f"  {role}: {table}, {column} IN ({', '.join(_quote(p.fio) for p in group)})")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "people": [{"fio": p.fio, "role": p.role, "objects": p.objects} for p in self.people()],
            "words": [m.word for m in self.mentions],
        }


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --- фамилии ---------------------------------------------------------------------

def _norm(text: str) -> str:
    return (text or "").strip().lower().replace("ё", "е")


def surname(fio: str) -> str:
    """Фамилия из поля ФИО: первое слово без инициалов, скобок и форм собственности."""
    text = re.sub(r"\([^)]*\)", " ", fio or "")
    for token in text.replace(",", " ").split():
        if "." in token:
            continue                     # инициалы «И.И.»
        if token.isupper() and len(token) <= 3:
            continue                     # ИП, ООО
        if _WORD_RE.fullmatch(token):
            return token
    return ""


def surname_base(name: str) -> str:
    """Основа фамилии без родового и падежного окончания: Черепанова → черепанов."""
    s = _norm(name)
    for ending, replacement in (("ская", "ск"), ("цкая", "цк"), ("ский", "ск"), ("цкий", "цк"), ("ской", "ск")):
        if s.endswith(ending) and len(s) > len(ending) + 2:
            return s[: -len(ending)] + replacement
    if len(s) > 5 and s.endswith(("ой", "ый", "ий")):
        return s[:-2]
    if len(s) > 4 and s.endswith(("а", "я", "ь", "й")):
        return s[:-1]
    return s


def _matches(word: str, base: str) -> bool:
    token = _norm(word)
    return len(base) >= 4 and token.startswith(base) and token[len(base):] in _CASE_ENDINGS


# --- справочник людей ----------------------------------------------------------

def sources(catalog: Catalog) -> dict[str, tuple[str, str, str]]:
    """Роль → (таблица каталога, поле ФИО, ключ объекта) для действующего каталога."""
    if "l_azs_rm_dt_vers" in catalog.tables and "l_azs_tm_dt_vers" in catalog.tables:
        return {ROLE_RM: ("l_azs_rm_dt_vers", "rm_fio", "ksss_code"),
                ROLE_TM: ("l_azs_tm_dt_vers", "tm_fio", "ksss_code")}
    stations = catalog.tables.get("stations", set())
    if {"regional_manager", "territory_manager"} <= stations:
        return {ROLE_RM: ("stations", "regional_manager", "ksss"),
                ROLE_TM: ("stations", "territory_manager", "ksss")}
    return {}


_cache: dict = {}


def directory(scope: Scope, catalog: Catalog | None = None,
              run_query: Callable[[str, int], executor.Result] | None = None) -> list[Person]:
    """Все РУ и ТМ в области данных пользователя (кэш по области на TTL_SECONDS)."""
    catalog = catalog or CATALOG
    run_query = run_query or executor.run
    key = (catalog.source, id(catalog), scope.unrestricted, scope.ksss)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        return cached[1]
    people: list[Person] = []
    for role, (table, column, entity) in sources(catalog).items():
        sql = (f"SELECT {column} AS fio, COUNT(DISTINCT {entity}) AS objects "
               f"FROM {catalog.qualified(table)} WHERE {column} IS NOT NULL GROUP BY {column}")
        checked = validate(sql, scope, row_limit=DIRECTORY_LIMIT)
        result = run_query(checked.sql, checked.row_limit)
        for fio, objects in result.rows:
            fio = str(fio or "").strip()
            if fio and _norm(fio) not in {"отсутствует", "нет данных", "-"}:
                people.append(Person(fio=fio, role=role, objects=int(objects or 0)))
    _cache[key] = (time.monotonic(), people)
    return people


def clear_cache() -> None:
    _cache.clear()


# --- разбор вопроса ------------------------------------------------------------

def find_mentions(question: str, people: list[Person]) -> list[Mention]:
    """Слова вопроса, похожие на фамилии людей из справочника."""
    by_base: dict[str, list[Person]] = {}
    for person in people:
        base = surname_base(surname(person.fio))
        if len(base) >= 4:
            by_base.setdefault(base, []).append(person)
    mentions: list[Mention] = []
    seen_words = set()
    for word in _WORD_RE.findall(question or ""):
        # Строчное слово считается фамилией только при длинной основе: так
        # «трассовых» или «планом» не превратятся в людей.
        found = [p for base, group in by_base.items() if _matches(word, base)
                 and (word[:1].isupper() or len(base) >= 6) for p in group]
        if found and _norm(word) not in seen_words:
            seen_words.add(_norm(word))
            mentions.append(Mention(word=word, people=sorted(set(found), key=lambda p: (p.role, p.fio))))
    return mentions


def resolve_mentions(mentions: list[Mention], catalog: Catalog) -> Resolution | None:
    """Правила владельца: один уровень для всех; если подходят оба — РУ; разные — без сравнения."""
    if not mentions:
        return None
    roles = [{p.role for p in m.people} for m in mentions]
    common = set.intersection(*roles)
    if ROLE_RM in common:
        level = ROLE_RM
    elif ROLE_TM in common:
        level = ROLE_TM
    else:
        level = "mixed"
    chosen: dict[str, list[Person]] = {}
    dropped: list[str] = []
    for mention in mentions:
        if level == "mixed":
            # Каждый — в своей роли; если у слова обе роли, по правилу берётся РУ.
            own = {p.role for p in mention.people}
            role = ROLE_RM if ROLE_RM in own else ROLE_TM
        else:
            role = level
        group = [p for p in mention.people if p.role == role]
        if len(group) < len(mention.people):
            dropped.append(mention.word)
        chosen[mention.word] = group
    src = {role: (catalog.qualified(table), column) for role, (table, column, _) in sources(catalog).items()}
    return Resolution(level=level, mentions=mentions, chosen=chosen, sources=src, namesakes_dropped=dropped)


def resolve(question: str, scope: Scope, catalog: Catalog | None = None,
            run_query: Callable[[str, int], executor.Result] | None = None) -> Resolution | None:
    """Люди в вопросе и уровень сравнения. None — людей не названо или справочник недоступен."""
    catalog = catalog or CATALOG
    if not sources(catalog):
        return None
    try:
        people = directory(scope, catalog, run_query)
    except Exception:  # noqa: BLE001 - справочник недоступен: вопрос идёт без подсказки
        return None
    return resolve_mentions(find_mentions(question, people), catalog)
