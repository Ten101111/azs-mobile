"""Область данных ИИ-контура на витрине ОХД — по справочникам самой витрины.

Решение владельца 23.09.2026: ИИ живёт в ДВХ и всё считает в нём, в том числе
то, какие объекты видит пользователь. Экраны приложения по-прежнему берут
привязку из stations.json (backend.roles); возможные расхождения между двумя
источниками — задача З-29 реестра и бэклога (База_знаний_проекта/ROADMAP.md), до её решения
допустимы.

Что откуда берётся при AI_DB_BACKEND=postgres:

  * роль и привязка — из учётной записи, как и раньше (backend.roles.ROLES);
  * РУ   — bds.l_azs_rm_dt_vers: объекты, закреплённые за rm_fio на сегодня;
  * ТМ   — bds.l_azs_tm_dt_vers: объекты, закреплённые за tm_fio на сегодня;
  * ОНПО — dm.data_for_ai_analytic_part_1: объекты с этим npo за последние
           SCOPE_NPO_DAYS дней;
  * управляющий и агент — КССС из привязки как есть (фильтр по ним ставит
    валидатор, лишних объектов он не добавит).

Запросы системные: их пишет код, а не модель, значение привязки экранируется.
Результат кэшируется на AI_SCOPE_TTL секунд (по умолчанию 30 минут). Если
витрина не ответила, область пустая с понятной причиной — открывать доступ
«на всякий случай» нельзя.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .. import roles
from . import executor
from .catalog import CATALOG, Catalog
from .validator import KSSS_RE, Scope

TTL_SECONDS = float(os.environ.get("AI_SCOPE_TTL", "1800"))
NPO_DAYS = int(os.environ.get("AI_SCOPE_NPO_DAYS", "60"))
ROW_LIMIT = 20_000

# Справочники людей: роль → (таблица, столбец ФИО). Ключ объекта — ksss_code.
MANAGER_TABLES = {
    "regional_manager": ("l_azs_rm_dt_vers", "rm_fio"),
    "territory_manager": ("l_azs_tm_dt_vers", "tm_fio"),
}
DEFAULT_SCHEMAS = {"l_azs_rm_dt_vers": "bds", "l_azs_tm_dt_vers": "bds"}

# Витрина не ответила (например, выключен VPN) — не повторять попытку при каждом
# открытии раздела: статус ИИ не должен ждать таймаутов подключения к ОХД.
FAIL_TTL_SECONDS = float(os.environ.get("AI_SCOPE_FAIL_TTL", "120"))

_cache: dict[tuple[str, str], tuple[float, Scope]] = {}
_identities_cache: dict[int, tuple[float, list]] = {}
_identities_failed: dict[int, float] = {}
_warming: set = set()
_warm_lock = threading.Lock()


def _in_background(key: tuple, fn, *args) -> None:
    """Прогреть кэш в фоне — один поток на ключ, ответ на запрос не ждёт витрину."""
    with _warm_lock:
        if key in _warming:
            return
        _warming.add(key)

    def run() -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - прогрев не должен ронять процесс
            pass
        finally:
            with _warm_lock:
                _warming.discard(key)

    threading.Thread(target=run, name="dwh-warm", daemon=True).start()


def enabled() -> bool:
    """Область по ДВХ действует только на витрине ОХД.

    AI_SCOPE_SOURCE=reference возвращает прежний способ (справочник
    приложения) — аварийный выключатель, а не рабочий режим.
    """
    source = (os.environ.get("AI_SCOPE_SOURCE") or "dwh").strip().lower()
    return executor.BACKEND == "postgres" and source != "reference"


def clear_cache() -> None:
    _cache.clear()
    _identities_cache.clear()
    _identities_failed.clear()


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _qualified(catalog: Catalog, table: str) -> str:
    schema = catalog.table_schemas.get(table) or DEFAULT_SCHEMAS.get(table) or catalog.schema
    return f"{schema}.{table}" if schema else table


def manager_sql(role: str, fio: str, catalog: Catalog | None = None) -> str:
    catalog = catalog or CATALOG
    table, column = MANAGER_TABLES[role]
    return (
        f"SELECT DISTINCT ksss_code FROM {_qualified(catalog, table)} "
        f"WHERE {column} = {_literal(fio)} "
        f"AND CURRENT_DATE >= dt_vers_start "
        f"AND CURRENT_DATE <= COALESCE(dt_vers_end, DATE '9999-12-31')"
    )


def npo_sql(npo: str, catalog: Catalog | None = None) -> str:
    catalog = catalog or CATALOG
    facts = catalog.facts_table or "data_for_ai_analytic_part_1"
    schema = catalog.table_schemas.get(facts) or catalog.schema
    table = f"{schema}.{facts}" if schema else facts
    key = catalog.scope_column or "ksss_azs_code"
    date = catalog.date_column or "account_date"
    return (
        f"SELECT DISTINCT {key} FROM {table} "
        f"WHERE npo = {_literal(npo)} AND {date} > CURRENT_DATE - {NPO_DAYS}"
    )


def _codes(sql: str) -> list[str]:
    result = executor.run(sql, ROW_LIMIT)
    return [str(row[0]) for row in result.rows if row and row[0] is not None]


def _empty(label: str) -> Scope:
    return Scope(unrestricted=False, ksss=(), label=label)


def build(role: str, binding: str = "", catalog: Catalog | None = None) -> Scope:
    role = (role or "").strip()
    binding = (binding or "").strip()
    spec = roles.ROLES.get(role)
    if not spec:
        raise ValueError(f"Неизвестная роль: {role!r}")
    if spec.unrestricted:
        return Scope.all_network()
    if not binding:
        return _empty(f"{spec.title}: привязка не задана")

    key = (role, binding)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        return cached[1]

    try:
        if spec.binding_kind == roles.BINDING_LIST:
            codes = [c.strip() for c in binding.replace(";", ",").split(",") if c.strip()]
            label = spec.title
        elif spec.binding_kind == roles.BINDING_STATION:
            codes = [binding]
            label = f"АЗС {binding}"
        elif role in MANAGER_TABLES:
            codes = _codes(manager_sql(role, binding, catalog))
            label = f"{spec.title} {binding}"
        elif spec.binding_kind == roles.BINDING_NPO:
            codes = _codes(npo_sql(binding, catalog))
            label = f"{spec.title} {binding}"
        else:
            return _empty(f"{spec.title}: способ привязки не поддержан на витрине ОХД")
    except executor.ExecutionError as err:
        # Не кэшируем: витрина может ответить со следующей попытки.
        return _empty(f"{spec.title} {binding}: витрина ОХД не ответила ({err})")

    codes = [c for c in codes if KSSS_RE.match(c)]
    if codes:
        label = f"{label} — {roles.plural_stations(len(set(codes)))} (по ОХД)"
    else:
        label = f"{label}: в ОХД нет объектов по этой привязке"
    scope = Scope.for_stations(codes, label)
    _cache[key] = (time.monotonic(), scope)
    return scope


def identities(limit_per_role: int = 12, catalog: Catalog | None = None,
               wait: bool = True) -> list[tuple[str, str, int]] | None:
    """Самые крупные привязки ОНПО, РУ и ТМ по ОХД — для режима «от имени».

    Возвращает (роль, привязка, число объектов). Ошибка витрины — пустой список:
    выбор «от имени» не должен ронять статус ИИ-раздела. `wait=False` — только
    из кэша: если его нет, запрос уходит в фон, а функция сразу возвращает None.
    """
    catalog = catalog or CATALOG
    cached = _identities_cache.get(limit_per_role)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        return cached[1]
    failed = _identities_failed.get(limit_per_role)
    if failed and time.monotonic() - failed < FAIL_TTL_SECONDS:
        return []
    if not wait:
        _in_background(("identities", limit_per_role), identities, limit_per_role, catalog)
        return None
    items: list[tuple[str, str, int]] = []
    queries = []
    facts = catalog.facts_table or "data_for_ai_analytic_part_1"
    schema = catalog.table_schemas.get(facts) or catalog.schema
    table = f"{schema}.{facts}" if schema else facts
    key = catalog.scope_column or "ksss_azs_code"
    date = catalog.date_column or "account_date"
    queries.append(("aup_npo",
                    f"SELECT npo, COUNT(DISTINCT {key}) AS n FROM {table} "
                    f"WHERE npo IS NOT NULL AND {date} > CURRENT_DATE - {NPO_DAYS} "
                    f"GROUP BY npo ORDER BY n DESC LIMIT {int(limit_per_role)}"))
    for role, (ref, column) in MANAGER_TABLES.items():
        queries.append((role,
                        f"SELECT {column}, COUNT(DISTINCT ksss_code) AS n FROM {_qualified(catalog, ref)} "
                        f"WHERE {column} IS NOT NULL AND CURRENT_DATE >= dt_vers_start "
                        f"AND CURRENT_DATE <= COALESCE(dt_vers_end, DATE '9999-12-31') "
                        f"GROUP BY {column} ORDER BY n DESC LIMIT {int(limit_per_role)}"))
    def fetch(item: tuple[str, str]) -> list[tuple[str, str, int]]:
        role, sql = item
        try:
            rows = executor.run(sql, limit_per_role).rows
        except executor.ExecutionError:
            return []
        return [(role, str(binding), int(count)) for binding, count in rows if binding]

    # Три запроса параллельно: при недоступной витрине ждём один таймаут, а не три.
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        for found in pool.map(fetch, queries):
            items.extend(found)
    if items:
        _identities_cache[limit_per_role] = (time.monotonic(), items)
        _identities_failed.pop(limit_per_role, None)
    else:
        _identities_failed[limit_per_role] = time.monotonic()
    return items


def cached_build(role: str, binding: str = "", catalog: Catalog | None = None) -> Scope | None:
    """Область без похода в ОХД: из кэша; нет в кэше — посчитать в фоне и вернуть None.

    Для статуса раздела (приветствие ИИ-08): экран не должен ждать витрину.
    """
    role = (role or "").strip()
    binding = (binding or "").strip()
    spec = roles.ROLES.get(role)
    if not spec:
        raise ValueError(f"Неизвестная роль: {role!r}")
    if spec.unrestricted or not binding:
        return build(role, binding, catalog)
    cached = _cache.get((role, binding))
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        return cached[1]
    _in_background(("scope", role, binding), build, role, binding, catalog)
    return None
