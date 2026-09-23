"""Семантический слой ИИ-контура: показатели, измерения, драйверы.

Бизнес-определения не генерируются моделью заново на каждый вопрос — они
лежат здесь и в каталоге. Модель получает готовые формулы через инструменты
(`get_metric_definition`, `search_schema`), а не придумывает их сама.

Два встроенных профиля: стенд на SQLite (`station_kpi_daily` + `stations`) и
витрина ОХД (`dm.data_for_ai_analytic_part_1/2`). Профиль выбирается по
каталогу; секция `semantic` в файле каталога дополняет или переопределяет
встроенные определения — так формулы можно править без правки кода.

Правило, которое нельзя ослаблять: каждое выражение в `expr` — агрегат над
колонками каталога. Валидатор всё равно проверит колонки, но модель должна
видеть только то, что действительно есть в витрине.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .catalog import CATALOG, Catalog


@dataclass(frozen=True)
class Metric:
    key: str
    title: str
    expr: str                       # агрегатное SQL-выражение для диалекта каталога
    unit: str = ""
    aliases: tuple[str, ...] = ()
    direction: str = "higher_better"  # higher_better | lower_better | neutral
    kind: str = "additive"            # additive | ratio | count | average
    table: str = ""                   # факты / планы; пусто — таблица фактов
    drivers: tuple[str, ...] = ()     # ключи метрик, на которые раскладывается
    notes: str = ""

    def describe(self) -> dict:
        return {
            "key": self.key, "title": self.title, "expr": self.expr, "unit": self.unit,
            "direction": self.direction, "kind": self.kind, "table": self.table,
            "drivers": list(self.drivers), "notes": self.notes,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class Dimension:
    key: str
    title: str
    column: str
    table: str
    aliases: tuple[str, ...] = ()
    kind: str = "category"   # category | entity | time
    notes: str = ""

    def describe(self) -> dict:
        return {
            "key": self.key, "title": self.title, "column": self.column, "table": self.table,
            "kind": self.kind, "notes": self.notes, "aliases": list(self.aliases),
        }


@dataclass
class Semantic:
    profile: str
    metrics: dict[str, Metric] = field(default_factory=dict)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    entity: dict = field(default_factory=dict)
    time: dict = field(default_factory=dict)
    peer_groups: list[list[str]] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)

    # --- поиск ------------------------------------------------------------

    def metric(self, name: str) -> Metric | None:
        """Метрика по ключу или синониму; None — такой метрики нет."""
        key = _norm(name)
        if key in self.metrics:
            return self.metrics[key]
        best, best_score = None, 0.0
        for metric in self.metrics.values():
            score = _match(key, (metric.title, *metric.aliases))
            if score > best_score:
                best, best_score = metric, score
        return best if best_score >= 0.7 else None

    def dimension(self, name: str) -> Dimension | None:
        key = _norm(name)
        if key in self.dimensions:
            return self.dimensions[key]
        for dim in self.dimensions.values():
            if key == _norm(dim.column) or _match(key, (dim.title, *dim.aliases)) >= 0.7:
                return dim
        return None

    def find(self, query: str, limit: int = 8) -> dict:
        """Что в семантическом слое похоже на текст вопроса.

        Возвращает метрики и измерения с оценкой похожести и перечень того,
        чего в витрине заведомо нет и что встретилось в вопросе, — чтобы агент
        сразу сказал об этом, а не искал.
        """
        text = _norm(query)
        tokens = _tokens(text)
        scored_metrics = []
        for metric in self.metrics.values():
            score = _score(tokens, text, (metric.title, *metric.aliases))
            if score > 0:
                scored_metrics.append((score, metric))
        scored_dims = []
        for dim in self.dimensions.values():
            score = _score(tokens, text, (dim.title, *dim.aliases, dim.column))
            if score > 0:
                scored_dims.append((score, dim))
        scored_metrics.sort(key=lambda item: -item[0])
        scored_dims.sort(key=lambda item: -item[0])
        absent_hits = [item for item in self.absent if _absent_mentioned(text, tokens, item)]
        return {
            "metrics": [
                {**metric.describe(), "score": round(score, 2)}
                for score, metric in scored_metrics[:limit]
            ],
            "dimensions": [
                {**dim.describe(), "score": round(score, 2)}
                for score, dim in scored_dims[:limit]
            ],
            "absent": absent_hits,
        }

    # --- для подсказки модели --------------------------------------------

    def prompt_block(self, keys: Iterable[str] | None = None) -> str:
        chosen = [self.metrics[k] for k in keys if k in self.metrics] if keys else list(self.metrics.values())
        lines = ["Показатели (ключ — название — формула — единица):"]
        for metric in chosen:
            tail = f" [{metric.table}]" if metric.table else ""
            lines.append(f"  {metric.key} — {metric.title} — {metric.expr} — {metric.unit}{tail}")
        if self.dimensions:
            lines.append("Измерения (ключ — колонка — что это):")
            for dim in self.dimensions.values():
                lines.append(f"  {dim.key} — {dim.table}.{dim.column} — {dim.title}")
        if self.absent:
            lines.append("Чего в витрине НЕТ (не досчитывать, а сказать прямо): " + "; ".join(self.absent))
        return "\n".join(lines)

    def describe(self) -> dict:
        return {
            "profile": self.profile,
            "metrics": [m.describe() for m in self.metrics.values()],
            "dimensions": [d.describe() for d in self.dimensions.values()],
            "entity": self.entity,
            "time": self.time,
            "peer_groups": self.peer_groups,
            "absent": self.absent,
            "rules": self.rules,
        }


# --- сопоставление текста -------------------------------------------------

_WORD_RE = re.compile(r"[a-zа-яё0-9_]+", re.I)


def _norm(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower().replace("ё", "е")))


def _tokens(text: str) -> list[str]:
    return [t for t in text.split() if len(t) > 2]


_ENDINGS = (
    "иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ешь", "ишь",
    "ов", "ев", "ах", "ях", "ам", "ям", "ой", "ей", "ый", "ий", "ая", "яя", "ое", "ее",
    "ые", "ие", "ом", "ем", "ую", "юю", "ть",
    "и", "ы", "а", "я", "у", "ю", "е", "о", "ь",
)


def _stem(token: str) -> str:
    # Грубое усечение окончаний: «чеков»/«чеки»/«чек» → «чек». Для поиска
    # синонимов этого достаточно, полноценная морфология здесь не нужна.
    if len(token) <= 3 or token.isdigit():
        return token
    for ending in _ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 3:
            return token[: -len(ending)]
    return token


def _match(text: str, names: Iterable[str]) -> float:
    for name in names:
        if _norm(name) == text:
            return 1.0
    return _score(_tokens(text), text, names)


def _score(tokens: list[str], text: str, names: Iterable[str]) -> float:
    best = 0.0
    stems = {_stem(t) for t in tokens}
    for raw in names:
        name = _norm(raw)
        if not name:
            continue
        if name in text:
            best = max(best, 0.9 + min(0.1, len(name) / 100))
            continue
        name_tokens = _tokens(name)
        if not name_tokens:
            continue
        hit = sum(1 for t in name_tokens if _stem(t) in stems)
        if hit:
            best = max(best, 0.5 * hit / len(name_tokens) + (0.2 if hit == len(name_tokens) else 0))
    return best


def _absent_mentioned(text: str, tokens: list[str], item: str) -> bool:
    # Пункт «нет в витрине» записан как «цена / средняя цена: …»; в вопросе
    # ищем любой из вариантов до двоеточия.
    head = item.split(":")[0]
    variants = [v.strip() for v in head.split("/") if v.strip()]
    stems = {_stem(t) for t in tokens}
    words = set(text.split())
    for variant in variants:
        v = _norm(variant)
        if not v:
            continue
        v_tokens = _tokens(v)
        if not v_tokens:
            # Короткое сокращение («ВД», «КС») — только целым словом.
            if v in words:
                return True
            continue
        if v in text or all(_stem(t) in stems for t in v_tokens):
            return True
    return False


# --- встроенные профили ---------------------------------------------------

def _m(key, title, expr, unit="", aliases=(), direction="higher_better", kind="additive",
       table="", drivers=(), notes="") -> Metric:
    return Metric(key, title, expr, unit, tuple(aliases), direction, kind, table, tuple(drivers), notes)


def _d(key, title, column, table, aliases=(), kind="category", notes="") -> Dimension:
    return Dimension(key, title, column, table, tuple(aliases), kind, notes)


STAND = Semantic(
    profile="stand",
    metrics={m.key: m for m in [
        _m("traffic", "Трафик — количество чеков", "SUM(checks)", "шт",
           ("трафик", "чеки", "количество чеков", "число чеков", "клиентский поток", "клиенты",
            "поток клиентов", "посещаемость"), kind="count",
           drivers=("active_stations", "checks_per_station", "checks_ntu"),
           notes="все чеки за день с учётом возвратов; отдельно топливные чеки в стенде не выделены"),
        _m("checks_ntu", "Чеки с НТУ", "SUM(checks_ntu)", "шт",
           ("чеки нту", "нетопливные чеки", "чеков с товарами"), kind="count",
           drivers=("traffic", "conversion_ntu")),
        _m("revenue", "Выручка общая", "SUM(revenue)", "руб",
           ("выручка", "общая выручка", "продажи", "оборот", "товарооборот"),
           drivers=("revenue_ntu", "fuel_volume", "traffic"),
           notes="топливо и НТУ вместе, с налогами"),
        _m("revenue_ntu", "Выручка НТУ", "SUM(revenue_ntu)", "руб",
           ("выручка нту", "продажи нту", "нетопливная выручка", "выручка магазина",
            "продажи нетопливных товаров", "нту"),
           drivers=("traffic", "conversion_ntu", "avg_check_ntu"),
           notes="выручка НТУ = чеки × конверсия НТУ × средний чек НТУ"),
        _m("fuel_volume", "Объём топлива", "SUM(fuel_volume)", "л",
           ("топливо", "объем топлива", "литры", "реализация топлива", "продажи топлива", "заправки"),
           drivers=("traffic",)),
        _m("conversion_ntu", "Конверсия НТУ", "100.0 * SUM(checks_ntu) / NULLIF(SUM(checks), 0)", "%",
           ("конверсия", "конверсия нту", "проникновение", "проникновение нту", "доля чеков с нту"),
           kind="ratio", drivers=("checks_ntu", "traffic"),
           notes="доля чеков, в которых были нетопливные товары"),
        _m("avg_check_ntu", "Средний чек НТУ", "SUM(revenue_ntu) / NULLIF(SUM(checks_ntu), 0)", "руб",
           ("средний чек нту", "средний чек магазина", "чек нту"), kind="ratio",
           drivers=("revenue_ntu", "checks_ntu")),
        _m("avg_check", "Средний чек общий", "SUM(revenue) / NULLIF(SUM(checks), 0)", "руб",
           ("средний чек", "средний чек общий", "выручка на чек", "выручка на клиента"), kind="ratio",
           drivers=("revenue", "traffic")),
        _m("ntu_per_100", "Выручка НТУ на 100 клиентов", "100.0 * SUM(revenue_ntu) / NULLIF(SUM(checks), 0)", "руб/100 чеков",
           ("на 100 клиентов", "на сто клиентов", "на 100 чеков", "нту на 100"), kind="ratio",
           drivers=("revenue_ntu", "traffic"),
           notes="в стенде нет количества товаров, поэтому «на 100 клиентов» считается по выручке НТУ"),
        _m("ntu_share", "Доля НТУ в выручке", "100.0 * SUM(revenue_ntu) / NULLIF(SUM(revenue), 0)", "%",
           ("доля нту", "доля нетопливной выручки", "структура выручки"), kind="ratio",
           drivers=("revenue_ntu", "revenue")),
        _m("active_stations", "Работающие АЗС", "COUNT(DISTINCT CASE WHEN checks > 0 THEN ksss END)", "шт",
           ("количество азс", "число азс", "работающие азс", "действующие азс", "сколько азс", "объектов"),
           kind="count", direction="neutral",
           notes="объекты, у которых в периоде были чеки; изменение их числа — отдельный драйвер трафика"),
        _m("checks_per_station", "Чеков на одну АЗС", "SUM(checks) * 1.0 / NULLIF(COUNT(DISTINCT CASE WHEN checks > 0 THEN ksss END), 0)", "шт/АЗС",
           ("на одну азс", "на 1 азс", "чеков на азс", "в расчете на азс", "на объект"), kind="ratio",
           drivers=("traffic", "active_stations")),
        _m("checks_per_day", "Чеков в день", "SUM(checks) * 1.0 / NULLIF(COUNT(DISTINCT metric_date), 0)", "шт/день",
           ("в день", "среднесуточно", "в сутки", "среднедневной"), kind="ratio", drivers=("traffic",),
           notes="делитель — число дней с данными в периоде; удобно для сравнения неполного месяца с полным"),
    ]},
    dimensions={d.key: d for d in [
        _d("station", "АЗС (объект)", "ksss", "stations", ("азс", "объект", "станция", "кссс"), kind="entity",
           notes="КССС и номер АЗС (station_number) — разные числа одной станции; названа числом — фильтруй оба"),
        _d("region", "Регион (субъект РФ)", "region", "stations", ("регион", "область", "край", "субъект", "москва", "по регионам")),
        _d("npo", "Общество (ОНПО)", "npo", "stations", ("онпо", "общество", "нпо", "по обществам")),
        _d("city", "Город", "city", "stations", ("город", "населенный пункт")),
        _d("format", "Формат АЗС", "format", "stations", ("формат",)),
        _d("location", "Расположение", "location", "stations", ("расположение", "трасса", "город или трасса")),
        _d("service_cluster", "Состав сервиса", "service_cluster", "stations", ("состав сервиса", "магазин кафе", "сервис")),
        _d("station_type", "Тип объекта", "station_type", "stations", ("тип объекта", "тип азс")),
        _d("status", "Статус объекта", "status", "stations", ("статус",)),
        _d("has_cafe", "Наличие кафе (1/0)", "has_cafe", "stations", ("кафе", "с кафе")),
        _d("is_active", "Действующая (1/0)", "is_active", "stations", ("действующая", "активная")),
        _d("regional_manager", "Руководитель управления (РУ)", "regional_manager", "stations", ("ру", "руководитель управления")),
        _d("territory_manager", "Территориальный менеджер (ТМ)", "territory_manager", "stations", ("тм", "территориальный менеджер")),
        _d("manager", "Управляющий АЗС", "manager", "stations", ("управляющий",)),
        _d("month", "Месяц", "period", "station_kpi_daily", ("месяц", "по месяцам"), kind="time",
           notes="формат 'YYYY-MM'"),
        _d("day", "День", "metric_date", "station_kpi_daily", ("день", "дата", "по дням"), kind="time",
           notes="формат 'YYYY-MM-DD'; неделя — strftime('%Y-%W', metric_date)"),
    ]},
    entity={
        "table": "stations", "facts_table": "station_kpi_daily", "key": "ksss",
        "number": "station_number", "name": "name",
        "join": "JOIN stations s ON s.ksss = d.ksss",
        "notes": "справочник присоединяй только за атрибутами; для сумм по выборке JOIN не нужен",
    },
    time={
        "dialect": "sqlite", "date_column": "metric_date", "month_column": "period",
        "month_filter": "period = 'YYYY-MM'",
        "range_filter": "metric_date >= 'YYYY-MM-DD' AND metric_date < 'YYYY-MM-DD'",
        "yesterday": "metric_date = date('now', '-1 day')",
        "today": "date('now')",
        "week": "strftime('%Y-%W', metric_date)",
        "notes": "данные по дням; текущий месяц может быть неполным — сравнивай одинаковое число дней или используй checks_per_day",
    },
    peer_groups=[["region", "format"], ["region"], ["format", "service_cluster"], ["npo"]],
    absent=[
        "кофе / SKU / категории товаров: в стенде нет разбивки НТУ по товарам и категориям",
        "цена / средняя цена / стоимость: цен и количества товаров в стенде нет",
        "ассортимент / наличие товара / остатки: таких данных в стенде нет",
        "валовой доход / ВД / маржа: в стенде только выручка",
        "план / выполнение плана: планов в стенде нет",
        "оценки / жалобы / уровень сервиса / качество сервиса: в стенде нет",
        "OPEX / расходы / затраты / покрытие: в стенде нет",
        "персонал / штат / операторы / кассиры: в стенде нет, обсуждать линейный персонал нельзя",
    ],
    rules=[
        "Гранулярность фактов — одна строка на АЗС за день; для ratio-показателей агрегируй числитель и знаменатель отдельно, а не усредняй дневные значения.",
        "Неполный текущий месяц сравнивай с тем же числом дней прошлого периода или через checks_per_day.",
        "Изменение числа работающих АЗС — отдельный драйвер: сначала проверь его, потом показатель на одну АЗС.",
        "РУ (regional_manager) и ТМ (territory_manager) — разные уровни, их между собой не сравнивают. Фамилия в вопросе — "
        "РУ или ТМ: если есть блок «ЛЮДИ В ВОПРОСЕ», роль уже определена; однофамильцы среди РУ и ТМ — речь о РУ.",
    ],
)


DWH = Semantic(
    profile="dwh",
    metrics={m.key: m for m in [
        _m("traffic", "Трафик — уникальные чеки", "SUM(cnt_cheq)", "шт",
           ("трафик", "чеки", "количество чеков", "уникальные чеки", "клиентский поток", "клиенты",
            "поток", "посещаемость"), kind="count",
           drivers=("active_stations", "checks_per_station", "checks_fuel", "checks_ntu")),
        _m("traffic_b2c", "Чеки B2C", "SUM(cnt_cheq_b2c)", "шт", ("чеки b2c", "физлица", "b2c"), kind="count"),
        _m("traffic_b2b", "Чеки B2B", "SUM(cnt_cheq_b2b)", "шт", ("чеки b2b", "юрлица", "b2b"), kind="count"),
        _m("checks_fuel", "Топливные чеки", "SUM(cnt_cheq_tu)", "шт", ("топливные чеки", "чеки ту", "заправки"), kind="count"),
        _m("checks_ntu", "Чеки НТУ", "SUM(cnt_cheq_ntu)", "шт", ("чеки нту", "нетопливные чеки"), kind="count",
           drivers=("checks_fuel", "conversion_ntu")),
        _m("complex_checks", "Комплексные чеки (топливо+НТУ)", "SUM(cnt_complex_cheq_tu_ntu)", "шт",
           ("комплексные чеки", "топливо и нту вместе"), kind="count"),
        _m("loyalty_checks", "Чеки с картой лояльности", "SUM(cnt_cheq_kl)", "шт", ("карта лояльности", "кл", "лояльность"), kind="count"),
        _m("loyalty_share", "Доля чеков с картой лояльности", "100.0 * SUM(cnt_cheq_kl) / NULLIF(SUM(cnt_cheq), 0)", "%",
           ("доля лояльности", "проникновение кл"), kind="ratio", drivers=("loyalty_checks", "traffic")),
        _m("points_out", "Списано баллов", "SUM(sum_ball_out)", "балл", ("баллы", "списание баллов")),
        _m("fuel_volume", "Реализация топлива", "SUM(sum_volume)", "л",
           ("топливо", "объем топлива", "литры", "реализация топлива", "продажи топлива"), drivers=("checks_fuel", "avg_fill")),
        _m("fuel_weight", "Реализация топлива", "SUM(sum_weight)", "т", ("тонны", "топливо в тоннах")),
        _m("fuel_volume_b2c", "Топливо B2C", "SUM(sum_volume_b2c)", "л", ("топливо b2c",)),
        _m("fuel_volume_b2b", "Топливо B2B", "SUM(sum_volume_b2b)", "л", ("топливо b2b",)),
        _m("fuel_volume_ab", "Бензины (АБ)", "SUM(sum_volume_ab)", "л", ("бензин", "бензины", "аб")),
        _m("fuel_volume_dt", "Дизельное топливо (ДТ)", "SUM(sum_volume_dt)", "л", ("дизель", "дт", "дизельное топливо")),
        _m("avg_fill", "Средняя заправка", "SUM(sum_volume) * 1.0 / NULLIF(SUM(cnt_cheq_tu), 0)", "л",
           ("средняя заправка", "литров на чек"), kind="ratio", drivers=("fuel_volume", "checks_fuel")),
        _m("revenue_fuel", "Выручка от топлива (без НДС)", "SUM(sum_receipt_netto_tu)", "руб",
           ("выручка топлива", "топливная выручка"), drivers=("fuel_volume",)),
        _m("revenue_ntu", "Выручка НТУ (без НДС)", "SUM(sum_receipt_netto_ntu)", "руб",
           ("выручка нту", "продажи нту", "нетопливная выручка", "выручка магазина", "нту"),
           drivers=("traffic", "conversion_ntu", "avg_check_ntu"),
           notes="выручка НТУ = топливные чеки × конверсия НТУ × средний чек НТУ (плюс чеки без топлива)"),
        _m("revenue", "Выручка общая (без НДС)", "SUM(sum_receipt_netto_tu) + SUM(sum_receipt_netto_ntu)", "руб",
           ("выручка", "общая выручка", "оборот"), drivers=("revenue_fuel", "revenue_ntu")),
        _m("vd_ntu", "Валовой доход НТУ", "SUM(vd_ntu)", "руб",
           ("вд", "валовой доход", "вд нту", "валовый доход", "маржинальный доход"),
           drivers=("revenue_ntu", "margin_ntu"),
           notes="ВД = выручка НТУ за вычетом себестоимости"),
        _m("vd_cafe", "ВД кафе", "SUM(vd_cafe)", "руб", ("кафе", "вд кафе", "кофейня"),
           notes="категория «кафе» целиком; отдельной позиции «кофе» в витрине нет"),
        _m("vd_prod", "ВД продовольственных товаров", "SUM(vd_prod)", "руб", ("продовольственные", "продукты", "вд прод")),
        _m("vd_neprod", "ВД непродовольственных товаров", "SUM(vd_neprod)", "руб", ("непродовольственные", "вд непрод")),
        _m("vd_category_4", "ВД категории 4 (хот-дог, кофе, кулинария и др.)", "SUM(vd_category_4)", "руб",
           ("категория 4", "хот-дог", "хот дог", "кулинария", "кофе", "горячие напитки"),
           notes="кофе входит в категорию 4 вместе с хот-догами и кулинарией — отдельно по кофе данных нет"),
        _m("items_ntu", "Количество товаров НТУ (ретейл)", "SUM(sum_quant_ntu_retail)", "шт",
           ("количество товаров", "товаров нту", "штук нту", "продажи в штуках"), kind="count",
           notes="только sum_quant_ntu_retail; sum_quant_ntu_balance не использовать"),
        _m("items_cafe", "SKU кафе, шт", "SUM(sum_quant_cafe)", "шт", ("штук кафе",), kind="count"),
        _m("items_prod", "SKU продовольственные, шт", "SUM(sum_quant_prod)", "шт", ("штук прод",), kind="count"),
        _m("items_neprod", "SKU непродовольственные, шт", "SUM(sum_quant_neprod)", "шт", ("штук непрод",), kind="count"),
        _m("items_category_4", "SKU категории 4, шт", "SUM(sum_quant_category_4)", "шт", ("штук категории 4",), kind="count"),
        _m("items_per_100", "Продажи НТУ на 100 клиентов", "100.0 * SUM(sum_quant_ntu_retail) / NULLIF(SUM(cnt_cheq), 0)", "шт/100 чеков",
           ("на 100 клиентов", "на сто клиентов", "на 100 чеков", "проникновение", "нту на 100"), kind="ratio",
           drivers=("items_ntu", "traffic")),
        _m("vd_per_client", "ВД на клиента", "SUM(vd_ntu) * 1.0 / NULLIF(SUM(cnt_cheq), 0)", "руб/чек",
           ("вд на клиента", "вд на чек", "доход на клиента"), kind="ratio", drivers=("vd_ntu", "traffic")),
        _m("conversion_ntu", "Конверсия НТУ", "100.0 * SUM(cnt_cheq_ntu) / NULLIF(SUM(cnt_cheq_tu), 0)", "%",
           ("конверсия", "конверсия нту", "доля чеков с нту"), kind="ratio", drivers=("checks_ntu", "checks_fuel")),
        _m("avg_check_ntu", "Средний чек НТУ", "SUM(sum_receipt_netto_ntu) / NULLIF(SUM(cnt_cheq_ntu), 0)", "руб",
           ("средний чек нту", "средний чек магазина"), kind="ratio", drivers=("revenue_ntu", "checks_ntu")),
        _m("avg_price_ntu", "Средняя выручка на товар НТУ", "SUM(sum_receipt_netto_ntu) / NULLIF(SUM(sum_quant_ntu_retail), 0)", "руб/шт",
           ("средняя цена", "цена товара", "цена нту"), kind="ratio", drivers=("revenue_ntu", "items_ntu"),
           notes="прокси цены: выручка на единицу товара; прайс-листа и цен по SKU в витрине нет"),
        _m("margin_ntu", "Маржа НТУ", "100.0 * SUM(vd_ntu) / NULLIF(SUM(sum_receipt_netto_ntu), 0)", "%",
           ("маржа", "маржинальность", "рентабельность нту"), kind="ratio", drivers=("vd_ntu", "revenue_ntu")),
        _m("complexity_ntu", "Комплексность НТУ", "SUM(sum_quant_ntu_retail) * 1.0 / NULLIF(SUM(cnt_cheq_ntu), 0)", "шт/чек",
           ("комплексность", "товаров в чеке"), kind="ratio", drivers=("items_ntu", "checks_ntu")),
        _m("active_stations", "Работающие АЗС", "COUNT(DISTINCT CASE WHEN cnt_cheq > 0 THEN ksss_azs_code END)", "шт",
           ("количество азс", "число азс", "работающие азс", "сколько азс", "объектов"), kind="count", direction="neutral"),
        _m("checks_per_station", "Чеков на одну АЗС", "SUM(cnt_cheq) * 1.0 / NULLIF(COUNT(DISTINCT CASE WHEN cnt_cheq > 0 THEN ksss_azs_code END), 0)", "шт/АЗС",
           ("на одну азс", "на 1 азс", "чеков на азс", "на объект"), kind="ratio", drivers=("traffic", "active_stations")),
        _m("checks_per_day", "Чеков в день", "SUM(cnt_cheq) * 1.0 / NULLIF(COUNT(DISTINCT account_date), 0)", "шт/день",
           ("в день", "среднесуточно", "в сутки"), kind="ratio", drivers=("traffic",)),
        # планы и сервис — вторая витрина
        _m("plan_ntu_revenue", "План выручки НТУ", "SUM(plan_ntu_revenue)", "руб", ("план нту", "план выручки нту"),
           table="data_for_ai_analytic_part_2", direction="neutral"),
        _m("plan_ntu_vd", "План ВД НТУ", "SUM(plan_ntu_vd)", "руб", ("план вд",), table="data_for_ai_analytic_part_2", direction="neutral"),
        _m("plan_fuel_b2c", "План топлива B2C", "SUM(plan_weights_b2c)", "т", ("план топлива", "план b2c"),
           table="data_for_ai_analytic_part_2", direction="neutral"),
        _m("plan_fuel_b2b", "План топлива B2B", "SUM(plan_weights_b2b)", "т", ("план b2b",), table="data_for_ai_analytic_part_2", direction="neutral"),
        _m("plan_completion_ntu", "Выполнение плана НТУ по выручке", "100.0 * SUM(f.sum_receipt_netto_ntu) / NULLIF(SUM(p.plan_ntu_revenue), 0)", "%",
           ("выполнение плана", "план факт", "темп к плану", "процент плана"), kind="ratio",
           table="data_for_ai_analytic_part_1 f JOIN data_for_ai_analytic_part_2 p ON p.ksss_azs_code = f.ksss_azs_code AND p.account_date = f.account_date",
           drivers=("revenue_ntu", "plan_ntu_revenue"),
           notes="соединять витрины по обоим полям ключа; иначе план задвоится"),
        _m("ratings", "Оценки в МП", "SUM(all_rate)", "шт", ("оценки", "количество оценок", "оценок в приложении"),
           table="data_for_ai_analytic_part_2", kind="count", direction="neutral"),
        _m("avg_rating", "Средняя оценка в МП (УС)", "(SUM(rate_cnt_5) * 5.0 + SUM(rate_cnt_4) * 4 + SUM(rate_cnt_3) * 3 + SUM(rate_cnt_2) * 2 + SUM(rate_cnt_1)) / NULLIF(SUM(all_rate), 0)", "балл",
           ("средняя оценка", "уровень сервиса", "ус", "оценка в мп", "рейтинг"), table="data_for_ai_analytic_part_2", kind="ratio",
           drivers=("ratings", "negative_ratings")),
        _m("negative_ratings", "Негативные оценки (1 и 2)", "SUM(rate_cnt_1) + SUM(rate_cnt_2)", "шт",
           ("негатив", "негативные оценки", "плохие оценки"), table="data_for_ai_analytic_part_2", kind="count", direction="lower_better"),
        _m("complaints", "Жалобы на ЕГЛ", "SUM(cnt_num_compl)", "шт", ("жалобы", "егл", "обращения"),
           table="data_for_ai_analytic_part_2", kind="count", direction="lower_better"),
        _m("service_quality", "Качество сервиса (КС)", "100000.0 * (SUM(p.rate_cnt_1) + SUM(p.rate_cnt_2) + SUM(p.cnt_num_compl)) / NULLIF(SUM(f.cnt_cheq), 0)", "на 100 тыс. чеков",
           ("качество сервиса", "кс"), kind="ratio", direction="lower_better",
           table="data_for_ai_analytic_part_1 f JOIN data_for_ai_analytic_part_2 p ON p.ksss_azs_code = f.ksss_azs_code AND p.account_date = f.account_date",
           drivers=("negative_ratings", "complaints", "traffic"),
           notes="негатив и жалобы на 100 тыс. чеков, меньше — лучше; не путать с УС"),
        _m("neg_cashier", "Негатив: обслуживание на кассе", "SUM(rate_pers_act_azs_cnt)", "шт", ("касса", "обслуживание на кассе"), table="data_for_ai_analytic_part_2", direction="lower_better"),
        _m("neg_clean", "Негатив: чистота АЗС", "SUM(rate_clear_azs_cnt)", "шт", ("чистота",), table="data_for_ai_analytic_part_2", direction="lower_better"),
        _m("neg_assort", "Негатив: ассортимент и качество НТУ", "SUM(rate_asort_azs_cnt)", "шт", ("ассортимент", "качество нту"), table="data_for_ai_analytic_part_2", direction="lower_better",
           notes="это оценки клиентов об ассортименте, а не данные о наличии товара"),
        _m("neg_tech", "Негатив: техническое состояние", "SUM(rate_tech_azs_cnt)", "шт", ("техническое состояние", "поломки"), table="data_for_ai_analytic_part_2", direction="lower_better"),
        _m("neg_app", "Негатив: мобильное приложение", "SUM(rate_app_mob_azs_cnt)", "шт", ("приложение", "мп"), table="data_for_ai_analytic_part_2", direction="lower_better"),
        _m("neg_loyalty", "Негатив: программа лояльности", "SUM(rate_loy_card_azs_cnt)", "шт", ("программа лояльности",), table="data_for_ai_analytic_part_2", direction="lower_better"),
        _m("neg_refueller", "Негатив: работа заправщика", "SUM(rate_refueller_azs_cnt)", "шт", ("заправщик",), table="data_for_ai_analytic_part_2", direction="lower_better"),
        _m("neg_other", "Негатив: другое", "SUM(rate_other_azs_cnt)", "шт", (), table="data_for_ai_analytic_part_2", direction="lower_better"),
        _m("opex", "OPEX", "SUM(sum_opex)", "млн руб", ("opex", "опекс", "расходы", "затраты", "операционные расходы"),
           table="data_for_ai_analytic_part_2", direction="lower_better"),
        _m("coverage", "Покрытие", "AVG(sum_costs_to_cover)", "%", ("покрытие", "покрытие расходов"),
           table="data_for_ai_analytic_part_2", kind="average",
           notes="процент за день по АЗС; агрегируется средним, суммировать нельзя"),
    ]},
    dimensions={d.key: d for d in [
        _d("station", "АЗС (объект)", "ksss_azs_code", "data_for_ai_analytic_part_1", ("азс", "объект", "станция", "кссс"), kind="entity",
           notes="КССС (ksss_azs_code) и номер на вывеске (num_azs) — разные числа; объект назван числом — фильтруй оба и выводи оба"),
        _d("station_number", "Номер АЗС", "num_azs", "data_for_ai_analytic_part_1", ("номер азс", "номер на вывеске"), kind="entity"),
        _d("npo", "Общество (НПО/ОНПО)", "npo", "data_for_ai_analytic_part_1", ("нпо", "онпо", "общество", "по обществам")),
        _d("region", "Регион", "region_name", "data_for_ai_analytic_part_1", ("регион", "область", "край", "субъект", "москва", "по регионам")),
        _d("day", "День", "account_date", "data_for_ai_analytic_part_1", ("день", "дата", "по дням"), kind="time",
           notes="date; месяц — DATE_TRUNC('month', account_date); неделя — DATE_TRUNC('week', account_date)"),
        # Справочники bds: кто руководил объектом на дату, текущие статус и тип АЗС.
        _d("regional_manager", "Руководитель управления (РУ) на дату", "rm_fio", "l_azs_rm_dt_vers",
           ("ру", "руководитель управления", "руководитель", "по ру"),
           notes="периоды закрепления: JOIN bds.l_azs_rm_dt_vers rm ON rm.ksss_code = f.ksss_azs_code "
                 "AND f.account_date >= rm.dt_vers_start AND f.account_date <= rm.dt_vers_end"),
        _d("territory_manager", "Территориальный менеджер (ТМ) на дату", "tm_fio", "l_azs_tm_dt_vers",
           ("тм", "территориальный менеджер", "территориал", "менеджер", "по тм"),
           notes="периоды закрепления: JOIN bds.l_azs_tm_dt_vers tm ON tm.ksss_code = f.ksss_azs_code "
                 "AND f.account_date >= tm.dt_vers_start AND f.account_date <= tm.dt_vers_end"),
        _d("station_status", "Статус работы АЗС (текущий)", "status_azs_name", "s_azs_ksss",
           ("статус", "статус азс", "статус работы", "действующие", "работающие по статусу"),
           notes="текущий статус, не на дату; JOIN bds.s_azs_ksss k ON k.ksss_azs_code = f.ksss_azs_code"),
        _d("station_type", "Тип АЗС: наличие кафе и магазина (текущий)", "type_azs_name", "s_azs_ksss",
           ("тип азс", "формат", "по формату", "кафе", "с кафе", "магазин", "с магазином", "состав сервиса"),
           notes="по нему считают и фильтруют АЗС «по формату»; точные значения — get_dimension_values; "
                 "JOIN bds.s_azs_ksss k ON k.ksss_azs_code = f.ksss_azs_code"),
    ]},
    entity={
        "table": "data_for_ai_analytic_part_1", "facts_table": "data_for_ai_analytic_part_1",
        "plans_table": "data_for_ai_analytic_part_2",
        "key": "ksss_azs_code", "number": "num_azs", "name": "",
        "join": "JOIN dm.data_for_ai_analytic_part_2 p ON p.ksss_azs_code = f.ksss_azs_code AND p.account_date = f.account_date",
        "rm_join": "JOIN bds.l_azs_rm_dt_vers rm ON rm.ksss_code = f.ksss_azs_code "
                   "AND f.account_date >= rm.dt_vers_start AND f.account_date <= rm.dt_vers_end",
        "tm_join": "JOIN bds.l_azs_tm_dt_vers tm ON tm.ksss_code = f.ksss_azs_code "
                   "AND f.account_date >= tm.dt_vers_start AND f.account_date <= tm.dt_vers_end",
        "status_type_join": "JOIN bds.s_azs_ksss k ON k.ksss_azs_code = f.ksss_azs_code",
        "notes": "измерения (НПО, регион, номер) только в part_1; витрины соединяй по обоим полям ключа; "
                 "РУ и ТМ — на дату через периоды закрепления, статус и тип АЗС — текущие",
    },
    time={
        "dialect": "postgres", "date_column": "account_date", "month_column": "",
        "month_filter": "account_date >= DATE 'YYYY-MM-01' AND account_date < DATE 'YYYY-MM+1-01'",
        "range_filter": "account_date >= DATE 'YYYY-MM-DD' AND account_date < DATE 'YYYY-MM-DD'",
        "yesterday": "account_date = CURRENT_DATE - INTERVAL '1 day'",
        "today": "CURRENT_DATE",
        "week": "DATE_TRUNC('week', account_date)",
        "notes": "столбца месяца нет — период задаётся диапазоном; CURRENT_DATE вместо даты числом; целые столбцы делятся нацело — умножай числитель на 1.0",
    },
    peer_groups=[["region", "station_type"], ["npo", "station_type"], ["region"], ["npo"]],
    absent=[
        "кофе / SKU / товарные позиции: разбивки по товарам нет — только категории (кафе, прод, непрод, категория 4 с кофе внутри)",
        "цена / прайс / стоимость товара: цен по SKU нет; есть только средняя выручка на товар (avg_price_ntu)",
        "наличие товара / остатки / out-of-stock: таких данных нет",
        "трасса или город / локация / тип местности: в витрине ОХД этих атрибутов нет (есть только тип АЗС по наличию кафе и магазина)",
        "управляющий АЗС / директор АЗС: ФИО управляющих в ОХД для ИИ нет — есть только РУ и ТМ",
        "персонал / штат / операторы / кассиры: нет, линейный персонал не обсуждается",
        "тексты отзывов / комментарии клиентов: в витрине нет, только количества по категориям",
    ],
    rules=[
        "Гранулярность — одна строка на АЗС за день; ratio-показатели считай как отношение агрегатов, а не среднее дневных значений.",
        "Витрины part_1 и part_2 соединяй по ksss_azs_code и account_date одновременно, иначе планы и оценки задвоятся.",
        "Неполный текущий месяц сравнивай с тем же числом дней прошлого периода или через checks_per_day.",
        "Изменение числа работающих АЗС — отдельный драйвер: сначала проверь его, потом показатель на одну АЗС.",
        "Прогноз только по методике: метод А (темп) и метод Б (профиль прошлого года), не раньше 8-го дня месяца, диапазон вместо одного числа.",
        "РУ и ТМ — на дату: соединяй факты с bds.l_azs_rm_dt_vers / bds.l_azs_tm_dt_vers по ksss_code = ksss_azs_code и попаданию "
        "account_date в период dt_vers_start…dt_vers_end (обе границы включительно); объект, сменивший руководителя, делится по дням.",
        "Фамилия в вопросе — РУ или ТМ. Блок «ЛЮДИ В ВОПРОСЕ» в задаче означает, что роль и точные ФИО уже определены — "
        "фильтруй по ним. Без блока ищи в обоих справочниках (UNION ALL из rm_fio и tm_fio) по основе фамилии без окончания, "
        "ILIKE '%Иванов%'; выводи ФИО, роль и число АЗС; не нашёл — скажи, что такой фамилии в справочниках нет.",
        "РУ и ТМ — разные уровни, их между собой не сравнивают: РУ только с РУ, ТМ только с ТМ. Однофамильцы среди РУ и ТМ — "
        "речь о РУ. Названы РУ и ТМ вместе — считай каждого отдельно, без сопоставления и ранжирования.",
        "Статус и тип АЗС (bds.s_azs_ksss) — текущие, не на дату; тип — наличие кафе и магазина, по нему считают «по формату»; "
        "точные значения статуса и типа не угадывай — посмотри get_dimension_values.",
    ],
)


PROFILES = {"stand": STAND, "dwh": DWH}


def _profile_for(catalog: Catalog) -> Semantic:
    if catalog.dialect == "postgres" or catalog.facts_table.startswith("data_for_ai"):
        return DWH
    return STAND


def _merge_from_catalog(base: Semantic, payload: dict | None) -> Semantic:
    """Секция semantic каталога дополняет встроенный профиль.

    Метрики и измерения с тем же ключом переопределяются, новые добавляются,
    ключи из `remove` убираются. Так владелец данных правит формулы файлом.
    """
    if not payload:
        return base
    metrics = dict(base.metrics)
    for item in payload.get("metrics", []):
        try:
            metric = _m(
                item["key"], item.get("title", item["key"]), item["expr"], item.get("unit", ""),
                tuple(item.get("aliases", [])), item.get("direction", "higher_better"),
                item.get("kind", "additive"), item.get("table", ""), tuple(item.get("drivers", [])),
                item.get("notes", ""),
            )
        except KeyError as err:
            raise SystemExit(f"Семантика каталога: у метрики нет поля {err}") from err
        metrics[metric.key] = metric
    for key in payload.get("remove", []):
        metrics.pop(key, None)
    dimensions = dict(base.dimensions)
    for item in payload.get("dimensions", []):
        dim = _d(item["key"], item.get("title", item["key"]), item["column"], item["table"],
                 tuple(item.get("aliases", [])), item.get("kind", "category"), item.get("notes", ""))
        dimensions[dim.key] = dim
    return Semantic(
        profile=base.profile,
        metrics=metrics,
        dimensions=dimensions,
        entity={**base.entity, **payload.get("entity", {})},
        time={**base.time, **payload.get("time", {})},
        peer_groups=payload.get("peer_groups") or base.peer_groups,
        absent=list(payload.get("absent") or base.absent),
        rules=list(base.rules) + list(payload.get("rules", [])),
    )


def load(catalog: Catalog | None = None) -> Semantic:
    catalog = catalog or CATALOG
    payload = getattr(catalog, "semantic", None)
    return _merge_from_catalog(_profile_for(catalog), payload)


SEMANTIC = load()
