"""Свод аналитики: каталог плиток и сборка запроса.

Плитки объявляются один раз, декларативно: какие столбцы нужны и как
считается величина. Доступной считается только та плитка, все источники
которой есть в действующем каталоге витрины — правило «нет данных, нет
плитки» соблюдается само, а не пометками вручную.

Запрос собирается один на все выбранные плитки и проходит тот же
валидатор, что и вопросы к ИИ: область данных подставляется системно.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ai.catalog import CATALOG

FACTS = "f"
PLANS = "p"


@dataclass(frozen=True)
class Tile:
    code: str
    title: str
    unit: str
    group: str
    expression: str                       # выражение над агрегатами
    needs: tuple[str, ...]                # столбцы, без которых плитка невозможна
    plans: bool = False                   # требуется ли витрина планов
    decimals: int = 0
    hint: str = ""
    lower_is_better: bool = False          # рост такого показателя — плохая новость


# Порядок здесь — порядок в каталоге настройки.
TILES: tuple[Tile, ...] = (
    Tile("fuel_volume", "Реализация топлива", "т", "Топливо",
         "SUM(f.sum_weight)", ("sum_weight",), decimals=1),
    Tile("fuel_plan", "Выполнение плана по топливу", "%", "Топливо",
         "100.0 * SUM(f.sum_weight) / NULLIF(SUM(p.plan_weights_b2c) + SUM(p.plan_weights_b2b), 0)",
         ("sum_weight", "plan_weights_b2c", "plan_weights_b2b"), plans=True, decimals=1),
    Tile("fuel_checks", "Топливные чеки", "шт", "Топливо",
         "SUM(f.cnt_cheq_tu)", ("cnt_cheq_tu",)),
    Tile("avg_refuel", "Средняя заправка", "л", "Топливо",
         "SUM(f.sum_volume) / NULLIF(SUM(f.cnt_cheq_tu), 0)",
         ("sum_volume", "cnt_cheq_tu"), decimals=1),

    Tile("ntu_revenue", "Выручка НТУ", "руб", "НТУ",
         "SUM(f.sum_receipt_netto_ntu)", ("sum_receipt_netto_ntu",)),
    Tile("ntu_plan", "Выполнение плана по НТУ", "%", "НТУ",
         "100.0 * SUM(f.sum_receipt_netto_ntu) / NULLIF(SUM(p.plan_ntu_revenue), 0)",
         ("sum_receipt_netto_ntu", "plan_ntu_revenue"), plans=True, decimals=1),
    Tile("ntu_vd", "Валовой доход НТУ", "руб", "НТУ",
         "SUM(f.vd_ntu)", ("vd_ntu",)),
    Tile("ntu_vd_plan", "Выполнение плана по ВД НТУ", "%", "НТУ",
         "100.0 * SUM(f.vd_ntu) / NULLIF(SUM(p.plan_ntu_vd), 0)",
         ("vd_ntu", "plan_ntu_vd"), plans=True, decimals=1),
    Tile("ntu_margin", "Маржа НТУ", "%", "НТУ",
         "100.0 * SUM(f.vd_ntu) / NULLIF(SUM(f.sum_receipt_netto_ntu), 0)",
         ("vd_ntu", "sum_receipt_netto_ntu"), decimals=1),
    Tile("conversion", "Конверсия НТУ", "%", "НТУ",
         "100.0 * SUM(f.cnt_cheq_ntu) / NULLIF(SUM(f.cnt_cheq_tu), 0)",
         ("cnt_cheq_ntu", "cnt_cheq_tu"), decimals=1,
         hint="Доля топливных чеков, в которых купили что-то ещё"),
    Tile("ntu_avg_check", "Средний чек НТУ", "руб", "НТУ",
         "SUM(f.sum_receipt_netto_ntu) / NULLIF(SUM(f.cnt_cheq_ntu), 0)",
         ("sum_receipt_netto_ntu", "cnt_cheq_ntu")),
    Tile("ntu_checks", "Нетопливные чеки", "шт", "НТУ",
         "SUM(f.cnt_cheq_ntu)", ("cnt_cheq_ntu",)),
    Tile("ntu_items", "Количество товаров НТУ", "шт", "НТУ",
         "SUM(f.sum_quant_ntu_retail)", ("sum_quant_ntu_retail",),
         hint="Счёт по ретейлу — решение владельца от 20.09.2026"),
    Tile("ntu_complexity", "Комплексность НТУ", "шт", "НТУ",
         # 1.0 обязательно: оба столбца целочисленные, иначе PostgreSQL
         # отбросит дробную часть и комплексность всегда будет ровным числом.
         "SUM(f.sum_quant_ntu_retail) * 1.0 / NULLIF(SUM(f.cnt_cheq_ntu), 0)",
         ("sum_quant_ntu_retail", "cnt_cheq_ntu"), decimals=2,
         hint="Товаров в одном нетопливном чеке"),
    Tile("vd_cafe", "Валовой доход кафе", "руб", "НТУ",
         "SUM(f.vd_cafe)", ("vd_cafe",)),

    Tile("checks_total", "Чеки всего", "шт", "Поток",
         "SUM(f.cnt_cheq)", ("cnt_cheq",)),
    # Выручка всего — топливо и НТУ без НДС (плитка для еженедельной справки, 23.09.2026).
    Tile("revenue_total", "Выручка всего", "руб", "Поток",
         "SUM(f.sum_receipt_netto_tu) + SUM(f.sum_receipt_netto_ntu)",
         ("sum_receipt_netto_tu", "sum_receipt_netto_ntu"), hint="Топливо и НТУ, без НДС"),
    Tile("loyalty_share", "Доля чеков с картой лояльности", "%", "Лояльность",
         "100.0 * SUM(f.cnt_cheq_kl) / NULLIF(SUM(f.cnt_cheq), 0)",
         ("cnt_cheq_kl", "cnt_cheq"), decimals=1),

    Tile("service_avg", "Средняя оценка в приложении", "балл", "Сервис",
         # 5.0, а не 5: столбцы оценок целочисленные, и в PostgreSQL деление
         # целого на целое отбрасывает дробную часть — средняя оценка молча
         # превращалась в ровную «4,000» за любой период.
         "(SUM(p.rate_cnt_5) * 5.0 + SUM(p.rate_cnt_4) * 4 + SUM(p.rate_cnt_3) * 3 "
         "+ SUM(p.rate_cnt_2) * 2 + SUM(p.rate_cnt_1)) / NULLIF(SUM(p.all_rate), 0)",
         ("rate_cnt_5", "rate_cnt_4", "rate_cnt_3", "rate_cnt_2", "rate_cnt_1", "all_rate"),
         plans=True, decimals=3),
    Tile("service_ratings", "Оценок в приложении", "шт", "Сервис",
         "SUM(p.all_rate)", ("all_rate",), plans=True),
    Tile("service_negative", "Негативные оценки", "шт", "Сервис",
         "SUM(p.rate_cnt_1) + SUM(p.rate_cnt_2)", ("rate_cnt_1", "rate_cnt_2"), plans=True,
         hint="Оценки «1» и «2» — то, что напрямую снижает уровень сервиса",
         lower_is_better=True),
    Tile("service_quality", "Качество сервиса", "на 100 тыс. чеков", "Сервис",
         "100000.0 * (SUM(p.rate_cnt_1) + SUM(p.rate_cnt_2) + SUM(p.cnt_num_compl)) "
         "/ NULLIF(SUM(f.cnt_cheq), 0)",
         ("rate_cnt_1", "rate_cnt_2", "cnt_num_compl", "cnt_cheq"), plans=True, decimals=2,
         lower_is_better=True,
         hint="Негатив и жалобы, приведённые к сопоставимому потоку чеков"),
    Tile("complaints", "Жалобы ЕГЛ", "шт", "Сервис",
         "SUM(p.cnt_num_compl)", ("cnt_num_compl",), plans=True, lower_is_better=True),
)

# Запасные определения тех же плиток для демонстрационного стенда: там у
# витрины другие имена столбцов. Код плитки общий, поэтому шаблоны и
# пользовательские наборы переносятся между контурами без правок.
STAND_TILES: tuple[Tile, ...] = (
    Tile("fuel_volume", "Реализация топлива", "л", "Топливо",
         "SUM(f.fuel_volume)", ("fuel_volume",)),
    Tile("fuel_checks", "Топливные чеки", "шт", "Топливо",
         "SUM(f.checks) - SUM(f.checks_ntu)", ("checks", "checks_ntu")),
    Tile("ntu_revenue", "Выручка НТУ", "руб", "НТУ",
         "SUM(f.revenue_ntu)", ("revenue_ntu",)),
    Tile("ntu_checks", "Нетопливные чеки", "шт", "НТУ",
         "SUM(f.checks_ntu)", ("checks_ntu",)),
    Tile("ntu_avg_check", "Средний чек НТУ", "руб", "НТУ",
         "SUM(f.revenue_ntu) / NULLIF(SUM(f.checks_ntu), 0)", ("revenue_ntu", "checks_ntu")),
    Tile("conversion", "Конверсия НТУ", "%", "НТУ",
         "100.0 * SUM(f.checks_ntu) / NULLIF(SUM(f.checks), 0)",
         ("checks_ntu", "checks"), decimals=1),
    Tile("checks_total", "Чеки всего", "шт", "Поток",
         "SUM(f.checks)", ("checks",)),
    Tile("revenue_total", "Выручка всего", "руб", "Поток",
         "SUM(f.revenue)", ("revenue",)),
)

TILES_BY_CODE = {tile.code: tile for tile in TILES}

# Состав по умолчанию. Для агентов — свой, с упором на качество обслуживания.
DEFAULT_TEMPLATE = ("fuel_plan", "ntu_plan", "ntu_vd", "conversion", "service_avg", "checks_total")
# Если витрина беднее, шаблон дополняется тем, что она умеет.
FALLBACK_TEMPLATE = ("fuel_volume", "ntu_revenue", "conversion", "ntu_avg_check",
                     "fuel_checks", "checks_total")
# Агенту план по НТУ доводится (решение владельца от 20.09.2026), поэтому
# выполнение плана стоит в шаблоне первым после реализации топлива.
AGENT_TEMPLATE = ("fuel_volume", "ntu_plan", "ntu_revenue", "conversion",
                  "service_avg", "service_quality")
TEMPLATES = {"agent": AGENT_TEMPLATE}
MAX_TILES = 8
MIN_TILES = 4


def _satisfied(tile: Tile, columns: set[str], has_plans: bool) -> bool:
    if tile.plans and not has_plans:
        return False
    return all(column in columns for column in tile.needs)


def available_tiles() -> list[Tile]:
    """Плитки, которые действующий каталог способен посчитать.

    У плитки может быть несколько определений — под разные витрины.
    Берётся первое, чьи столбцы есть в каталоге; порядок задан списком.
    """
    columns = CATALOG.column_universe()
    has_plans = bool(CATALOG.plans_table)
    chosen: dict[str, Tile] = {}
    for tile in (*TILES, *STAND_TILES):
        if tile.code in chosen:
            continue
        if _satisfied(tile, columns, has_plans):
            chosen[tile.code] = tile
    order = [tile.code for tile in (*TILES, *STAND_TILES)]
    seen = []
    for code in order:
        if code in chosen and code not in seen:
            seen.append(code)
    return [chosen[code] for code in seen]


def template_for(role: str) -> list[str]:
    available = {tile.code for tile in available_tiles()}
    codes = [code for code in TEMPLATES.get(role, DEFAULT_TEMPLATE) if code in available]
    for code in FALLBACK_TEMPLATE:
        if len(codes) >= MIN_TILES:
            break
        if code in available and code not in codes:
            codes.append(code)
    return codes


def resolve_selection(codes: list[str] | None, role: str) -> list[Tile]:
    available = {tile.code: tile for tile in available_tiles()}
    chosen = [code for code in (codes or []) if code in available]
    if not chosen:
        chosen = template_for(role)
    return [available[code] for code in chosen[:MAX_TILES]]


def _date_literal(value: str) -> str:
    """Литерал даты под диалект витрины.

    В PostgreSQL дата пишется как DATE '2026-08-01'. В SQLite типа DATE нет:
    CAST('2026-08-01' AS DATE) даёт число 2026, и сравнение молча перестаёт
    находить строки. Поэтому там дата остаётся текстом — формат YYYY-MM-DD
    сравнивается лексикографически и работает правильно.
    """
    if CATALOG.dialect == "sqlite":
        return f"'{value}'"
    return f"DATE '{value}'"


def build_query(tiles: list[Tile], date_from: str, date_to: str) -> str:
    """Один запрос на все плитки: витрина читается один раз, а не по разу на плитку."""
    if not tiles:
        raise ValueError("Не выбрано ни одной плитки")
    schema = f"{CATALOG.schema}." if CATALOG.schema else ""
    date_column = CATALOG.date_column
    needs_plans = any(tile.plans for tile in tiles)
    if needs_plans and not CATALOG.plans_table:
        raise ValueError("В каталоге нет витрины планов")

    selects = ",\n       ".join(
        f'ROUND({tile.expression}, {tile.decimals}) AS "{tile.code}"' if tile.decimals
        else f'ROUND({tile.expression}) AS "{tile.code}"'
        for tile in tiles
    )
    lines = [f"SELECT {selects}", f"FROM {schema}{CATALOG.facts_table} AS {FACTS}"]
    if needs_plans:
        lines.append(f"JOIN {schema}{CATALOG.plans_table} AS {PLANS}")
        lines.append(f"  ON {PLANS}.{CATALOG.scope_column} = {FACTS}.{CATALOG.scope_column}"
                     f" AND {PLANS}.{date_column} = {FACTS}.{date_column}")
    lines.append(f"WHERE {FACTS}.{date_column} >= {_date_literal(date_from)}")
    lines.append(f"  AND {FACTS}.{date_column} <= {_date_literal(date_to)}")
    return "\n".join(lines)


def latest_date_query() -> str:
    """Последний день, за который в витрине есть данные."""
    schema = f"{CATALOG.schema}." if CATALOG.schema else ""
    return (f"SELECT MAX({FACTS}.{CATALOG.date_column}) AS \"latest\"\n"
            f"FROM {schema}{CATALOG.facts_table} AS {FACTS}")


def months_query() -> str:
    """Месяцы, за которые в витрине вообще есть данные.

    Витрина пилота залита не сплошным периодом, поэтому список месяцев
    нельзя строить по календарю: пользователь выбирал бы месяцы, которых
    в витрине нет, и видел прочерки вместо значений.
    """
    schema = f"{CATALOG.schema}." if CATALOG.schema else ""
    column = f"{FACTS}.{CATALOG.date_column}"
    expr = f"SUBSTR({column}, 1, 7)" if CATALOG.dialect == "sqlite" else f"TO_CHAR({column}, 'YYYY-MM')"
    return (f'SELECT DISTINCT {expr} AS "month"\n'
            f"FROM {schema}{CATALOG.facts_table} AS {FACTS}\n"
            f'ORDER BY "month" DESC')


def describe(tile: Tile) -> dict:
    return {
        "code": tile.code,
        "title": tile.title,
        "unit": tile.unit,
        "group": tile.group,
        "decimals": tile.decimals,
        "hint": tile.hint,
        "lowerIsBetter": tile.lower_is_better,
    }
