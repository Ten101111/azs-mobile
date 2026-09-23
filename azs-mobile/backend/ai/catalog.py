"""Каталог данных для ИИ: что модель видит и что разрешено валидатору.

По умолчанию описывает демонстрационный стенд на SQLite. Чтобы перевести
контур на витрину в ОХД, каталог задаётся файлом data/ai_catalog.json —
код при этом не меняется. Путь переопределяется переменной AI_CATALOG.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
CATALOG_FILE = Path(os.environ.get("AI_CATALOG") or DATA / "ai_catalog.json")

# Демонстрационный стенд: две таблицы локальной базы агрегатов.
BUILTIN_TABLES: dict[str, set[str]] = {
    "station_kpi_daily": {
        "metric_date", "period", "ksss", "revenue", "revenue_ntu",
        "fuel_volume", "checks", "checks_ntu", "avg_check",
    },
    "stations": {
        "ksss", "station_number", "name", "station_type", "status", "npo",
        "region", "city", "address", "format", "location", "service_cluster",
        "shop", "shop_area", "trk_count", "posts_count", "has_cafe",
        "has_shop", "is_active", "is_agency", "regional_manager",
        "territory_manager", "manager",
    },
}
BUILTIN_SCOPED = {"station_kpi_daily", "stations"}


@dataclass
class Catalog:
    tables: dict[str, set[str]] = field(default_factory=dict)
    scoped_tables: set[str] = field(default_factory=set)
    scope_column: str = "ksss"
    # Какая витрина содержит факты, какая планы, и по какому столбцу период.
    facts_table: str = ""
    plans_table: str = ""
    date_column: str = ""
    # Тип ключа объекта: text — подставляем в кавычках, number — без них.
    scope_value_type: str = "text"
    dialect: str = "sqlite"
    schema: str = ""          # схема витрины, если таблицы адресуются с префиксом
    description: str = ""     # текст описания таблиц для модели
    examples: list[tuple[str, str]] = field(default_factory=list)
    source: str = "встроенный"
    # Секция semantic файла каталога: правки формул и синонимов поверх
    # встроенного профиля (см. backend/ai/semantic.py). None — нет секции.
    semantic: dict | None = None
    # Таблицы из другой схемы, чем `schema` (например справочники bds рядом
    # с витринами dm): имя таблицы → её схема.
    table_schemas: dict[str, str] = field(default_factory=dict)
    # Свой ключ объекта у таблицы, если он называется иначе, чем scope_column
    # (в справочниках РУ/ТМ это ksss_code): по нему подставляется область данных.
    scope_columns: dict[str, str] = field(default_factory=dict)
    # Таблицы, из которых наружу выходят только столбцы каталога: валидатор
    # подменяет их подзапросом с явным списком столбцов. Остальные поля таблицы
    # не видны ни модели, ни результату — даже через SELECT * или CTE.
    projected_tables: set[str] = field(default_factory=set)
    # Порядок столбцов таблицы, как в каталоге (для проекции и описаний).
    column_order: dict[str, list[str]] = field(default_factory=dict)

    def column_universe(self) -> set[str]:
        return set().union(*self.tables.values()) if self.tables else set()

    def schema_of(self, table: str) -> str:
        """Схема, в которой лежит таблица каталога."""
        return self.table_schemas.get(table, self.schema)

    def scope_column_of(self, table: str) -> str:
        """Столбец ключа объекта, по которому таблица ограничивается областью данных."""
        return self.scope_columns.get(table, self.scope_column)

    def qualified(self, table: str) -> str:
        schema = self.schema_of(table)
        return f"{schema}.{table}" if schema and "." not in table else table

    def columns_of(self, table: str) -> list[str]:
        order = self.column_order.get(table)
        return list(order) if order else sorted(self.tables.get(table, ()))


def _from_file(path: Path) -> Catalog:
    payload = json.loads(path.read_text(encoding="utf-8"))
    specs = payload.get("tables", {})
    tables = {
        name: {str(column).lower() for column in spec.get("columns", [])}
        for name, spec in specs.items()
    }
    scoped = {name for name, spec in specs.items() if spec.get("scoped", True)}
    return Catalog(
        table_schemas={name: spec["schema"].lower() for name, spec in specs.items() if spec.get("schema")},
        scope_columns={name: spec["scope_column"].lower() for name, spec in specs.items()
                       if spec.get("scope_column")},
        projected_tables={name for name, spec in specs.items() if spec.get("project")},
        column_order={name: [str(c).lower() for c in spec.get("columns", [])] for name, spec in specs.items()},
        tables=tables,
        scoped_tables=scoped,
        scope_column=payload.get("scope_column", "ksss"),
        facts_table=payload.get("facts_table", ""),
        plans_table=payload.get("plans_table", ""),
        date_column=payload.get("date_column", ""),
        scope_value_type=payload.get("scope_value_type", "text"),
        dialect=payload.get("dialect", "sqlite"),
        schema=(payload.get("schema") or "").lower(),
        description=payload.get("description", ""),
        examples=[(item["question"], item["sql"]) for item in payload.get("examples", [])],
        source=str(path),
        semantic=payload.get("semantic") or None,
    )


def load() -> Catalog:
    if CATALOG_FILE.exists():
        try:
            return _from_file(CATALOG_FILE)
        except Exception as err:  # noqa: BLE001
            raise SystemExit(f"Каталог {CATALOG_FILE} не прочитан: {err}") from err
    return Catalog(
        tables={name: set(columns) for name, columns in BUILTIN_TABLES.items()},
        scoped_tables=set(BUILTIN_SCOPED),
        facts_table="station_kpi_daily",
        plans_table="",
        date_column="metric_date",
    )


CATALOG = load()
