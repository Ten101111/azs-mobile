"""Детерминированный валидатор SQL для ИИ-контура.

Модель — недоверенный генератор. Решение о допустимости запроса принимает
этот модуль, разбирая SQL в синтаксическое дерево, а не регулярными
выражениями. Область данных подставляется системно и не может быть снята
ни текстом вопроса, ни текстом сгенерированного запроса.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import sqlglot
from sqlglot import exp

from .catalog import CATALOG

# Диалект разбора совпадает с исполнителем: sqlite — стенд, postgres — витрина ОХД.
DIALECT = (os.environ.get("AI_SQL_DIALECT") or CATALOG.dialect or "sqlite").strip().lower()
# Схема витрины, если таблицы адресуются с префиксом (например dm.station_kpi).
ALLOWED_SCHEMA = (os.environ.get("AI_SQL_SCHEMA") or CATALOG.schema or "").strip().lower()
DEFAULT_ROW_LIMIT = 200

# --- каталог: что вообще существует для ИИ ---------------------------------

# Каталог задаётся файлом data/ai_catalog.json; по умолчанию — стенд на SQLite.
SCOPED_TABLES = CATALOG.scoped_tables
SCOPE_COLUMN = CATALOG.scope_column
# Таблицы, из которых выходят только столбцы каталога (см. Catalog.projected_tables).
PROJECTED_TABLES = CATALOG.projected_tables

FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Command, exp.Transaction, exp.Commit, exp.Rollback, exp.Merge,
    exp.Pragma, exp.Attach, exp.Detach, exp.Set,
)

FORBIDDEN_FUNCTIONS = {
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
    "sqlite_compileoption_used", "sqlite_source_id", "randomblob", "zeroblob",
}

KSSS_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")


class Rejected(Exception):
    """Запрос отклонён валидатором."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message


@dataclass
class Scope:
    """Область данных пользователя.

    unrestricted=True — доступ ко всей сети (АУП сети, администратор).
    Иначе запрос ограничивается перечнем КССС.
    """

    unrestricted: bool = False
    ksss: tuple[str, ...] = ()
    label: str = "вся сеть"

    @classmethod
    def all_network(cls) -> "Scope":
        return cls(unrestricted=True, label="вся сеть")

    @classmethod
    def for_stations(cls, ksss: Iterable[str], label: str) -> "Scope":
        clean = tuple(sorted({str(k).strip() for k in ksss if str(k).strip()}))
        for value in clean:
            if not KSSS_RE.match(value):
                raise ValueError(f"недопустимый КССС в области данных: {value!r}")
        return cls(unrestricted=False, ksss=clean, label=label)


@dataclass
class Validated:
    sql: str
    notes: list[str] = field(default_factory=list)
    scope_applied: bool = False
    row_limit: int = DEFAULT_ROW_LIMIT


# --- разбор ----------------------------------------------------------------

def _parse(sql: str) -> exp.Expression:
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise Rejected("empty", "Пустой запрос")
    try:
        statements = sqlglot.parse(text, read=DIALECT)
    except Exception as err:  # noqa: BLE001 — любая ошибка разбора = отказ
        raise Rejected("parse", f"Запрос не разобран: {err}") from err
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise Rejected("multi", "Допускается ровно один запрос")
    return statements[0]


def _assert_select_only(tree: exp.Expression) -> None:
    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        raise Rejected("not_select", "Допускается только SELECT")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise Rejected(
                "forbidden_statement",
                f"Недопустимая конструкция: {type(node).__name__.upper()}",
            )
        if isinstance(node, exp.Anonymous):
            name = (node.name or "").lower()
            if name in FORBIDDEN_FUNCTIONS:
                raise Rejected("forbidden_function", f"Недопустимая функция: {name}")


def _cte_names(tree: exp.Expression) -> set[str]:
    names = set()
    for cte in tree.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            names.add(alias.lower())
    return names


def _expected_schema(name: str) -> str:
    """Схема таблицы каталога: своя у справочников (bds), общая у витрин (dm)."""
    return CATALOG.table_schemas.get(name) or ALLOWED_SCHEMA


def _check_tables(tree: exp.Expression, known_ctes: set[str]) -> None:
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            continue
        if name in known_ctes:
            continue
        if name not in CATALOG.tables:
            raise Rejected("unknown_table", f"Таблица вне каталога: {table.name}")
        schema = (table.db or "").lower()
        if table.catalog or (schema and schema != _expected_schema(name)):
            raise Rejected("qualified_table", "Обращение к внешней схеме запрещено")


def _qualify(tree: exp.Expression, known_ctes: set[str]) -> None:
    """Таблицам из своей схемы дописывает схему, если модель её опустила.

    Витрины dm адресуются так, как написано в примерах; справочники из другой
    схемы (bds) без префикса в Postgres не найдутся — префикс ставит система.
    """
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in known_ctes or table.db:
            continue
        schema = CATALOG.table_schemas.get(name)
        if schema:
            table.set("db", exp.to_identifier(schema))


def _query_aliases(tree: exp.Expression) -> set[str]:
    """Имена, которые запрос вводит сам: псевдонимы выражений и колонок CTE."""
    names: set[str] = set()
    for alias in tree.find_all(exp.Alias):
        name = (alias.alias or "").lower()
        if name:
            names.add(name)
    for cte in tree.find_all(exp.CTE):
        table_alias = cte.args.get("alias")
        if isinstance(table_alias, exp.TableAlias):
            for column in table_alias.columns:
                name = (column.name or "").lower()
                if name:
                    names.add(name)
    return names


def _check_columns(tree: exp.Expression, known_ctes: set[str]) -> None:
    if known_ctes:
        # При CTE состав колонок промежуточных наборов заранее неизвестен;
        # ограничение по таблицам и по области данных при этом сохраняется.
        return
    allowed = CATALOG.column_universe() | _query_aliases(tree)
    for column in tree.find_all(exp.Column):
        name = (column.name or "").lower()
        if name and name != "*" and name not in allowed:
            raise Rejected("unknown_column", f"Колонка вне каталога: {column.name}")


# --- подстановка области данных --------------------------------------------

def _scope_predicate(scope: Scope, column: str = SCOPE_COLUMN) -> str:
    if not scope.ksss:
        # Пустая область данных: запрос корректен, но результата не даёт.
        return "0 = 1"
    if CATALOG.scope_value_type == "number":
        # Ключ объекта числовой (в витрине ОХД это bigint): кавычки недопустимы.
        if not all(value.isdigit() for value in scope.ksss):
            raise Rejected(
                "scope_type",
                "В области данных есть нечисловые коды объектов, а ключ витрины числовой",
            )
        values = ", ".join(scope.ksss)
    else:
        values = ", ".join(f"'{value}'" for value in scope.ksss)
    return f"{column} IN ({values})"


def _referenced_columns(tree: exp.Expression) -> set[str]:
    """Имена столбцов, к которым запрос обращается хоть где-нибудь (включая JOIN … USING)."""
    names = {(column.name or "").lower() for column in tree.find_all(exp.Column)}
    for join in tree.find_all(exp.Join):
        for ident in join.args.get("using") or []:
            names.add((getattr(ident, "name", "") or "").lower())
    names.discard("")
    return names


def _star_projection(tree: exp.Expression) -> bool:
    """В запросе есть проекция `*` или `t.*` (COUNT(*) проекцией не считается)."""
    for select in tree.find_all(exp.Select):
        for item in select.expressions:
            if isinstance(item, exp.Star):
                return True
            if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                return True
    return False


def _projection(name: str, referenced: set[str], star: bool) -> list[str]:
    """Столбцы, которые выпускаются из таблицы: только из каталога и только нужные запросу.

    При `SELECT *` — все столбцы каталога, но не больше. Если запросу не нужен
    ни один столбец (COUNT(*)), выпускается ключ объекта: подзапросу нужен хотя бы один.
    """
    allowed = CATALOG.columns_of(name)
    if star:
        return allowed
    picked = [column for column in allowed if column in referenced]
    if picked:
        return picked
    key = CATALOG.scope_column_of(name)
    return [key] if key in allowed else allowed[:1]


def _apply_scope(tree: exp.Expression, scope: Scope, known_ctes: set[str]) -> bool:
    """Заменяет каждую ссылку на защищаемую таблицу подзапросом с фильтром.

    Приём выбран намеренно: дописывать условие в WHERE ненадёжно при
    соединениях и вложенных запросах, а подмена самой таблицы ограничивает
    её всюду, где бы она ни встретилась.

    Тот же подзапрос отсекает лишние столбцы у таблиц из projected_tables:
    вместо `SELECT *` в нём перечислены только столбцы каталога, нужные
    запросу. Поэтому даже `SELECT *` или обращение из CTE не вытащит из
    таблицы полей вне каталога (служебные, закрытые решением владельца,
    лишние поля справочников). Проекция действует для любой роли, фильтр
    области — только для ограниченной. Возвращает, подставлен ли фильтр.
    """
    applied = False
    # Считается до подмены: подставленные подзапросы добавят свои столбцы.
    referenced = _referenced_columns(tree)
    star = _star_projection(tree)

    def transform(node: exp.Expression) -> exp.Expression:
        nonlocal applied
        if not isinstance(node, exp.Table):
            return node
        name = (node.name or "").lower()
        if name in known_ctes or name not in CATALOG.tables:
            return node
        scoped = name in SCOPED_TABLES and not scope.unrestricted
        projected = name in PROJECTED_TABLES
        if not scoped and not projected:
            return node
        alias = node.alias or node.name
        # Копия ссылки на таблицу сохраняет схему: dm.foo останется dm.foo.
        source = node.copy()
        source.set("alias", None)
        columns = _projection(name, referenced, star) if projected else ["*"]
        inner = exp.select(*columns).from_(source)
        if scoped:
            column = CATALOG.scope_column_of(name)
            inner = inner.where(_scope_predicate(scope, column), dialect=DIALECT)
            applied = True
        return exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias)))

    tree.transform(transform, copy=False)
    return applied


def _enforce_limit(tree: exp.Expression, row_limit: int) -> bool:
    if not isinstance(tree, exp.Select):
        return False
    existing = tree.args.get("limit")
    if existing is not None:
        try:
            current = int(existing.expression.name)
        except Exception:  # noqa: BLE001
            current = row_limit + 1
        if current <= row_limit:
            return False
    tree.set("limit", exp.Limit(expression=exp.Literal.number(row_limit)))
    return True


# --- точка входа -----------------------------------------------------------

def validate(sql: str, scope: Scope, row_limit: int = DEFAULT_ROW_LIMIT) -> Validated:
    tree = _parse(sql)
    _assert_select_only(tree)
    ctes = _cte_names(tree)
    _check_tables(tree, ctes)
    _check_columns(tree, ctes)

    notes: list[str] = []
    _qualify(tree, ctes)
    scope_applied = _apply_scope(tree, scope, ctes)
    if scope_applied:
        notes.append(f"Подставлен фильтр области данных: {scope.label}")
    elif scope.unrestricted:
        notes.append("Область данных: вся сеть, фильтр не требуется")

    if _enforce_limit(tree, row_limit):
        notes.append(f"Ограничение вывода: {row_limit} строк")

    return Validated(
        sql=tree.sql(dialect=DIALECT, pretty=True),
        notes=notes,
        scope_applied=scope_applied,
        row_limit=row_limit,
    )
