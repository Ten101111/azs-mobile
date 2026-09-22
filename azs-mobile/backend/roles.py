"""Ролевая модель и область данных.

Единственный источник правды о том, что за роли существуют, чем они
отличаются и какие объекты видит конкретный пользователь. Им пользуются
и экраны аналитики, и ИИ-контур: расходиться они не должны.

Область данных всегда вычисляется на сервере по роли и привязке из учётной
записи. Ни параметр запроса, ни текст вопроса к ИИ на неё не влияют.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
REFERENCE_DB = Path(os.environ.get("AI_REFERENCE_DB") or DATA / "ai_reference.sqlite3")

# Как задаётся привязка роли к объектам.
BINDING_NONE = "none"        # привязка не нужна — роль видит всю сеть
BINDING_NPO = "npo"          # общество
BINDING_MANAGER = "manager"  # ФИО руководителя из справочника
BINDING_STATION = "station"  # один объект по КССС
BINDING_LIST = "list"        # явный перечень КССС через запятую


@dataclass(frozen=True)
class RoleSpec:
    code: str
    title: str
    binding_kind: str
    binding_label: str = ""
    reference_column: str = ""   # столбец справочника для подбора привязки
    unrestricted: bool = False
    ai_dialog: bool = False      # доступен ли свободный диалог с ИИ
    order: int = 0


ROLES: dict[str, RoleSpec] = {
    spec.code: spec
    for spec in [
        RoleSpec("admin", "Администратор", BINDING_NONE,
                 unrestricted=True, ai_dialog=True, order=1),
        RoleSpec("subadmin", "Субадминистратор", BINDING_NONE,
                 unrestricted=True, ai_dialog=True, order=2),
        RoleSpec("aup_network", "АУП сети", BINDING_NONE,
                 unrestricted=True, ai_dialog=True, order=3),
        RoleSpec("aup_npo", "АУП общества", BINDING_NPO,
                 binding_label="Общество (ОНПО)", reference_column="npo",
                 ai_dialog=True, order=4),
        RoleSpec("regional_manager", "Руководитель управления", BINDING_MANAGER,
                 binding_label="ФИО руководителя", reference_column="regional_manager",
                 ai_dialog=True, order=5),
        RoleSpec("territory_manager", "Территориальный менеджер", BINDING_MANAGER,
                 binding_label="ФИО менеджера", reference_column="territory_manager",
                 order=6),
        RoleSpec("agent", "Агент", BINDING_LIST,
                 binding_label="Коды КССС через запятую", order=7),
        RoleSpec("station", "Управляющий АЗС", BINDING_STATION,
                 binding_label="КССС объекта", reference_column="ksss", order=8),
    ]
}

ROLE_ORDER = [code for code, _ in sorted(ROLES.items(), key=lambda item: item[1].order)]
# Роль, которую получает пользователь, пока администратор не назначил другую.
DEFAULT_ROLE = ""


@dataclass
class DataScope:
    """Область данных пользователя."""

    unrestricted: bool = False
    ksss: tuple[str, ...] = ()
    label: str = ""
    role: str = ""
    binding: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def stations(self) -> int:
        return -1 if self.unrestricted else len(self.ksss)

    @property
    def is_empty(self) -> bool:
        return not self.unrestricted and not self.ksss


def _reference_query(sql: str, params: tuple) -> list:
    if not REFERENCE_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{REFERENCE_DB}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def plural_stations(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        word = "объектов"
    else:
        word = {1: "объект", 2: "объекта", 3: "объекта", 4: "объекта"}.get(count % 10, "объектов")
    return f"{count} {word}"


def _by_column(column: str, value: str) -> list[str]:
    rows = _reference_query(
        f"SELECT ksss FROM stations WHERE {column} = ? AND is_active = 1", (value,)
    )
    return [str(row[0]) for row in rows]


def binding_options(role: str, limit: int = 500) -> list[dict]:
    """Значения привязки, которые администратор может выбрать для роли."""
    spec = ROLES.get(role)
    if not spec or not spec.reference_column:
        return []
    column = spec.reference_column
    if column == "ksss":
        rows = _reference_query(
            "SELECT ksss, name || ' · ' || COALESCE(npo, '') AS note FROM stations "
            "WHERE is_active = 1 ORDER BY station_number LIMIT ?", (limit,)
        )
        return [{"value": str(r[0]), "note": str(r[1]), "stations": 1} for r in rows]
    rows = _reference_query(
        f"SELECT {column}, COUNT(*) FROM stations "
        f"WHERE {column} IS NOT NULL AND is_active = 1 "
        f"GROUP BY {column} ORDER BY 2 DESC LIMIT ?", (limit,)
    )
    return [{"value": str(r[0]), "note": "", "stations": int(r[1])} for r in rows]


def resolve(role: str, binding: str = "") -> DataScope:
    """Область данных по роли и привязке. Неизвестная роль — пустая область."""
    role = (role or "").strip()
    binding = (binding or "").strip()
    spec = ROLES.get(role)

    if not spec:
        return DataScope(label="роль не назначена", role=role, binding=binding,
                         problems=["Администратор ещё не назначил роль"])

    if spec.unrestricted:
        return DataScope(unrestricted=True, label="вся сеть", role=role, binding=binding)

    if not binding:
        return DataScope(label=f"{spec.title}: привязка не задана", role=role,
                         problems=[f"Для роли «{spec.title}» не указана привязка"])

    if spec.binding_kind == BINDING_LIST:
        codes = [part.strip() for part in binding.replace(";", ",").split(",")]
        ksss = [code for code in codes if code]
        label = spec.title
    elif spec.binding_kind == BINDING_STATION:
        ksss = _by_column("ksss", binding)
        label = f"АЗС {binding}"
    else:
        ksss = _by_column(spec.reference_column, binding)
        label = f"{spec.title} {binding}"

    problems = []
    if not ksss:
        problems.append("По этой привязке в справочнике нет действующих объектов")
    else:
        label = f"{label} — {plural_stations(len(ksss))}"

    return DataScope(ksss=tuple(sorted(set(ksss))), label=label,
                     role=role, binding=binding, problems=problems)


def describe(role: str) -> dict:
    spec = ROLES.get(role)
    if not spec:
        return {"code": role, "title": "Роль не назначена", "bindingKind": BINDING_NONE,
                "bindingLabel": "", "unrestricted": False, "aiDialog": False}
    return {
        "code": spec.code,
        "title": spec.title,
        "bindingKind": spec.binding_kind,
        "bindingLabel": spec.binding_label,
        "unrestricted": spec.unrestricted,
        "aiDialog": spec.ai_dialog,
    }


def catalog() -> list[dict]:
    return [describe(code) for code in ROLE_ORDER]
