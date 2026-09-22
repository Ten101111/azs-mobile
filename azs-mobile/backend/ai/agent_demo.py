"""Прогон аналитического агента из командной строки.

Живая модель (Ollama):
    scripts/python.sh -m backend.ai.agent_demo                    # шесть вопросов владельца одним диалогом
    scripts/python.sh -m backend.ai.agent_demo --question "Почему снизился трафик?" --depth deep

Без модели — сценарный прогон тех же шести вопросов на стенде:
    scripts/python.sh -m backend.ai.agent_demo --scripted
Сценарий подменяет модель заранее заданными вызовами инструментов и проверяет
контур целиком: разбор, валидатор, исполнитель, проверки качества, песочницу,
графики, сверку чисел, память диалога. Качество рассуждений он не измеряет —
для этого нужен живой прогон.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

from . import generator, pipeline
from .agent import llm, prompts

QUESTIONS = [
    "Сколько чеков было вчера?",
    "Как изменились чеки относительно прошлого месяца?",
    "Какие 10 АЗС сильнее всего снизили трафик?",
    "Почему снизился трафик?",
    "Какие АЗС имеют наибольший потенциал роста?",
    "Покажи динамику и построй график",
]


# --- сценарная модель ------------------------------------------------------

class ScenarioModel:
    """Отвечает по сценарию, различая разбор, ход агента и финал по системной подсказке."""

    model = "scripted"

    def __init__(self, scenarios: dict[str, dict]):
        self.scenarios = scenarios
        self.active: dict | None = None
        self.cursor = 0

    def use(self, question: str) -> None:
        self.active = self.scenarios[question]
        self.cursor = 0

    def chat(self, messages, tools=None, json_mode=False, max_tokens=0) -> llm.Reply:
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        scenario = self.active or {}
        if json_mode and system.startswith(prompts.TRIAGE_SYSTEM[:40]):
            return llm.Reply(content=json.dumps(scenario.get("triage", {}), ensure_ascii=False), model=self.model)
        if json_mode:
            evidence = messages[1]["content"] if len(messages) > 1 else ""
            final = scenario.get("final")
            payload = final(evidence) if callable(final) else (final or {})
            return llm.Reply(content=json.dumps(payload, ensure_ascii=False), model=self.model)
        calls = scenario.get("calls", [])
        if self.cursor < len(calls):
            call = calls[self.cursor]
            self.cursor += 1
            return llm.Reply(tool_calls=[llm.ToolCall(name=call["tool"], arguments=call.get("arguments", {}), id=f"s{self.cursor}")],
                             model=self.model)
        # Инструменты закончились — пусть контур сам вызовет финал по собранным данным.
        return llm.Reply(content="Данных достаточно, формулирую ответ.", model=self.model)


def _summary(evidence: str) -> dict:
    """Числа, которые сценарный шаг Python напечатал строкой SUMMARY {...}."""
    match = re.search(r"SUMMARY (\{.*\})", evidence)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def _fmt(value, digits=0) -> str:
    if value is None:
        return "—"
    if digits:
        return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")
    return f"{round(value):,}".replace(",", " ")


def build_scenarios() -> dict[str, dict]:
    """Сценарии для стенда SQLite (station_kpi_daily + stations)."""
    # Стенд обновляется по дням: последняя дата — в get_data_range; сравниваем
    # одинаковое число дней текущего и прошлого месяца.
    same_days = ("WITH last AS (SELECT MAX(metric_date) AS d FROM station_kpi_daily), "
                 "bounds AS (SELECT d, strftime('%Y-%m', d) AS cur, strftime('%Y-%m', d, '-1 month') AS prev, "
                 "CAST(strftime('%d', d) AS INTEGER) AS days FROM last) ")

    compare_sql = same_days + (
        "SELECT k.period AS \"Месяц\", ROUND(SUM(k.checks)) AS \"Чеки, шт\", "
        "COUNT(DISTINCT k.metric_date) AS \"Дней\", "
        "ROUND(SUM(k.checks) * 1.0 / COUNT(DISTINCT k.metric_date)) AS \"Чеков в день\", "
        "COUNT(DISTINCT CASE WHEN k.checks > 0 THEN k.ksss END) AS \"Работающие АЗС\" "
        "FROM station_kpi_daily k, bounds b "
        "WHERE k.period IN (b.prev, b.cur) AND CAST(strftime('%d', k.metric_date) AS INTEGER) <= b.days "
        "GROUP BY k.period ORDER BY k.period"
    )
    compare_py = (
        "prev, cur = r1.iloc[0], r1.iloc[1]\n"
        "ch = pct_change(cur['Чеки, шт'], prev['Чеки, шт'])\n"
        "per_day = pct_change(cur['Чеков в день'], prev['Чеков в день'])\n"
        "print('SUMMARY', json.dumps({'cur': cur['Месяц'], 'prev': prev['Месяц'], 'days': int(cur['Дней']), "
        "'cur_checks': float(cur['Чеки, шт']), 'prev_checks': float(prev['Чеки, шт']), 'abs': ch['abs'], 'pct': ch['pct'], "
        "'per_day_pct': per_day['pct'], 'stations_cur': int(cur['Работающие АЗС']), 'stations_prev': int(prev['Работающие АЗС'])}, ensure_ascii=False))\n"
        "result = r1"
    )

    def compare_final(evidence: str) -> dict:
        s = _summary(evidence)
        if not s:
            return {"headline": "Сравнение чеков за одинаковое число дней двух месяцев — в таблице.", "happened": []}
        direction = "снизилось" if (s["abs"] or 0) < 0 else "выросло"
        return {
            "headline": f"За первые {s['days']} дней {s['cur']} количество чеков {direction} на {_fmt(abs(s['pct']), 1)} % "
                        f"к тем же дням {s['prev']}: {_fmt(s['cur_checks'])} против {_fmt(s['prev_checks'])}.",
            "happened": [f"Абсолютное изменение: {_fmt(s['abs'])} чеков.",
                         f"В расчёте на день изменение составило {_fmt(s['per_day_pct'], 1)} %.",
                         f"Работающих АЗС: {s['stations_cur']} против {s['stations_prev']}."],
            "why": [], "where": [], "actions": [],
            "limitations": [f"Текущий месяц неполный: сравнение по первым {s['days']} дням обоих месяцев."],
            "main_result": "r1",
        }

    top_decline_sql = same_days + (
        "SELECT s.station_number AS \"Номер АЗС\", s.name AS \"АЗС\", s.region AS \"Регион\", "
        "ROUND(SUM(CASE WHEN k.period = b.prev THEN k.checks END)) AS \"Чеки прошлый месяц\", "
        "ROUND(SUM(CASE WHEN k.period = b.cur THEN k.checks END)) AS \"Чеки текущий месяц\", "
        "ROUND(SUM(CASE WHEN k.period = b.cur THEN k.checks END) - SUM(CASE WHEN k.period = b.prev THEN k.checks END)) AS \"Изменение, шт\", "
        "ROUND(100.0 * SUM(CASE WHEN k.period = b.cur THEN k.checks END) / NULLIF(SUM(CASE WHEN k.period = b.prev THEN k.checks END), 0) - 100, 1) AS \"Изменение, %\" "
        "FROM station_kpi_daily k JOIN stations s ON s.ksss = k.ksss, bounds b "
        "WHERE k.period IN (b.prev, b.cur) AND CAST(strftime('%d', k.metric_date) AS INTEGER) <= b.days "
        "GROUP BY s.station_number, s.name, s.region "
        "HAVING SUM(CASE WHEN k.period = b.prev THEN k.checks END) >= 500 "
        "AND SUM(CASE WHEN k.period = b.cur THEN k.checks END) IS NOT NULL "
        "ORDER BY 6 ASC LIMIT 10"
    )

    def top_final(evidence: str) -> dict:
        return {
            "headline": "Десять АЗС с наибольшим снижением числа чеков за одинаковое число дней текущего и прошлого месяца — в таблице и на графике.",
            "happened": ["Снижение показано в штуках и в процентах; объекты с малым трафиком (меньше 500 чеков за период) и объекты без данных за текущий месяц исключены."],
            "why": [], "where": [], "actions": ["Проверить на этих объектах режим работы и наличие топлива за текущий месяц."],
            "limitations": ["Причины снижения по отдельным объектам в стенде не видны: нет цен, ассортимента и данных о простоях.",
                            "Объекты, у которых в текущем месяце данных нет вовсе (возможно, закрытые), в список не попали — их стоит проверить отдельно."],
            "main_result": "r1",
        }

    why_sql_1 = same_days + (
        "SELECT k.period AS \"Месяц\", ROUND(SUM(k.checks)) AS \"Чеки, шт\", "
        "COUNT(DISTINCT CASE WHEN k.checks > 0 THEN k.ksss END) AS \"Работающие АЗС\", "
        "ROUND(SUM(k.checks_ntu)) AS \"Чеки НТУ, шт\", ROUND(SUM(k.fuel_volume)) AS \"Топливо, л\" "
        "FROM station_kpi_daily k, bounds b "
        "WHERE k.period IN (b.prev, b.cur) AND CAST(strftime('%d', k.metric_date) AS INTEGER) <= b.days "
        "GROUP BY k.period ORDER BY k.period"
    )
    why_sql_2 = same_days + (
        "SELECT s.region AS \"Регион\", "
        "ROUND(SUM(CASE WHEN k.period = b.prev THEN k.checks END)) AS \"Прошлый месяц\", "
        "ROUND(SUM(CASE WHEN k.period = b.cur THEN k.checks END)) AS \"Текущий месяц\", "
        "ROUND(SUM(CASE WHEN k.period = b.cur THEN k.checks END) - SUM(CASE WHEN k.period = b.prev THEN k.checks END)) AS \"Изменение, шт\" "
        "FROM station_kpi_daily k JOIN stations s ON s.ksss = k.ksss, bounds b "
        "WHERE k.period IN (b.prev, b.cur) AND CAST(strftime('%d', k.metric_date) AS INTEGER) <= b.days "
        "GROUP BY s.region ORDER BY 4 ASC"
    )
    why_sql_3 = same_days + (
        "SELECT s.station_number AS \"Номер АЗС\", "
        "ROUND(SUM(CASE WHEN k.period = b.cur THEN k.checks END) - SUM(CASE WHEN k.period = b.prev THEN k.checks END)) AS \"Изменение, шт\" "
        "FROM station_kpi_daily k JOIN stations s ON s.ksss = k.ksss, bounds b "
        "WHERE k.period IN (b.prev, b.cur) AND CAST(strftime('%d', k.metric_date) AS INTEGER) <= b.days "
        "GROUP BY s.station_number ORDER BY 2 ASC"
    )
    why_sql_4 = same_days + (
        "SELECT k.period AS \"Месяц\", ROUND(SUM(k.checks)) AS \"Чеки, шт\" "
        "FROM station_kpi_daily k, bounds b "
        "WHERE k.period IN (strftime('%Y-%m', b.d, '-1 year'), strftime('%Y-%m', b.d, '-1 year', '-1 month')) "
        "AND CAST(strftime('%d', k.metric_date) AS INTEGER) <= b.days GROUP BY k.period ORDER BY k.period"
    )
    why_py = (
        "prev, cur = r1.iloc[0], r1.iloc[1]\n"
        "base = {'работающие АЗС': prev['Работающие АЗС'], 'чеков на АЗС': prev['Чеки, шт'] / prev['Работающие АЗС']}\n"
        "now = {'работающие АЗС': cur['Работающие АЗС'], 'чеков на АЗС': cur['Чеки, шт'] / cur['Работающие АЗС']}\n"
        "d = decompose(base, now, ['работающие АЗС', 'чеков на АЗС'])\n"
        "p = pareto(r3, 'Номер АЗС', 'Изменение, шт', share=0.5)\n"
        "ntu = pct_change(cur['Чеки НТУ, шт'], prev['Чеки НТУ, шт'])\n"
        "fuel = pct_change(cur['Топливо, л'], prev['Топливо, л'])\n"
        "ly = pct_change(r4.iloc[1]['Чеки, шт'], r4.iloc[0]['Чеки, шт']) if len(r4) == 2 else {'pct': None}\n"
        "neg_regions = int((r2['Изменение, шт'] < 0).sum()); all_regions = int(len(r2))\n"
        "print('SUMMARY', json.dumps({'cur': cur['Месяц'], 'prev': prev['Месяц'], 'change': d['change'], 'change_pct': d['change_pct'], "
        "'stations_share': d['factors'][0]['share_of_change_pct'], 'per_station_share': d['factors'][1]['share_of_change_pct'], "
        "'stations_prev': int(prev['Работающие АЗС']), 'stations_cur': int(cur['Работающие АЗС']), "
        "'pareto_n': p['count_for_share'], 'pareto_all': p['objects_all'], 'declined': p['objects'], "
        "'ntu_pct': ntu['pct'], 'fuel_pct': fuel['pct'], 'ly_pct': ly['pct'], 'neg_regions': neg_regions, 'all_regions': all_regions}, ensure_ascii=False))\n"
        "result = pd.DataFrame([{'Драйвер': f['factor'], 'Вклад, чеков': f['contribution'], 'Доля изменения, %': f['share_of_change_pct']} for f in d['factors']])"
    )

    def why_final(evidence: str) -> dict:
        s = _summary(evidence)
        if not s:
            return {"headline": "Разложение изменения трафика — в таблицах.", "happened": []}
        why = [f"Число работающих АЗС: {s['stations_prev']} → {s['stations_cur']}; вклад этого фактора — {_fmt(s['stations_share'], 1)} % изменения, "
               f"остальное ({_fmt(s['per_station_share'], 1)} %) — чеки на одну АЗС.",
               f"Чеки с НТУ изменились на {_fmt(s['ntu_pct'], 1)} %, объём топлива — на {_fmt(s['fuel_pct'], 1)} %: движение общее, а не только по одной составляющей."]
        if s.get("ly_pct") is not None:
            why.append(f"Год назад за те же дни изменение между этими месяцами составляло {_fmt(s['ly_pct'], 1)} % — сезонная часть движения оценивается по этой величине.")
        return {
            "headline": f"За одинаковое число дней {s['cur']} к {s['prev']} количество чеков изменилось на {_fmt(s['change'])} ({_fmt(s['change_pct'], 1)} %).",
            "happened": [f"Снижение затронуло {s['neg_regions']} регионов из {s['all_regions']}.",
                         f"Половину суммарного снижения дали {s['pareto_n']} АЗС из {s['declined']} снизившихся (всего объектов {s['pareto_all']})."],
            "why": why,
            "where": ["Регионы и объекты с наибольшим снижением — в таблицах ниже; чужие объекты не называются."],
            "actions": ["Начать с объектов, давших половину снижения: проверить режим работы, наличие топлива и отзывы за период."],
            "limitations": ["Разложение показывает, какие драйверы двигались вместе с трафиком, — это не доказательство причины.",
                            "В стенде нет цен, ассортимента и наличия товара: их влияние не оценивалось."],
            "main_result": "p1",
        }

    potential_sql = (
        "WITH last AS (SELECT strftime('%Y-%m', MAX(metric_date), '-1 month') AS m FROM station_kpi_daily) "
        "SELECT s.station_number AS \"Номер АЗС\", s.name AS \"АЗС\", s.region AS \"Регион\", s.format AS \"Формат\", "
        "ROUND(SUM(k.checks)) AS \"Чеки, шт\", "
        "ROUND(100.0 * SUM(k.checks_ntu) / NULLIF(SUM(k.checks), 0), 1) AS \"Конверсия НТУ, %\", "
        "ROUND(SUM(k.revenue_ntu) / NULLIF(SUM(k.checks_ntu), 0)) AS \"Средний чек НТУ, руб\" "
        "FROM station_kpi_daily k JOIN stations s ON s.ksss = k.ksss, last "
        "WHERE k.period = last.m AND s.is_active = 1 "
        "GROUP BY s.station_number, s.name, s.region, s.format HAVING SUM(k.checks) >= 3000 ORDER BY 5 DESC"
    )
    potential_py = (
        "w = whatif_lift(r1, value='Конверсия НТУ, %', target='median', base='Чеки, шт', label='Номер АЗС', group=['Регион', 'Формат'])\n"
        "top = pd.DataFrame(w['top'])\n"
        "top = top.merge(r1[['Номер АЗС', 'Регион', 'Средний чек НТУ, руб']], on='Номер АЗС', how='left')\n"
        "top['Доп. выручка НТУ, руб'] = top['effect'] * top['Средний чек НТУ, руб']\n"
        "top = top.rename(columns={'target': 'Медиана группы, %', 'gap': 'Разрыв, п.п.', 'effect': 'Доп. чеков с НТУ'})\n"
        "print('SUMMARY', json.dumps({'below': w['objects_below'], 'all': w['objects_all'], 'effect': w['effect'], "
        "'effect_pct': w['effect_pct_of_baseline'], 'top_revenue': float(top['Доп. выручка НТУ, руб'].head(15).sum())}, ensure_ascii=False))\n"
        "result = top.round(1)"
    )

    def potential_final(evidence: str) -> dict:
        s = _summary(evidence)
        if not s:
            return {"headline": "Объекты с потенциалом роста конверсии НТУ — в таблице.", "happened": []}
        return {
            "headline": f"У {s['below']} из {s['all']} крупных АЗС конверсия НТУ ниже медианы своей группы (регион × формат); "
                        f"подтягивание до медианы дало бы около {_fmt(s['effect'])} дополнительных чеков с НТУ в месяц ({_fmt(s['effect_pct'], 1)} % к их трафику).",
            "happened": [f"По пятнадцати объектам с наибольшим эффектом дополнительная выручка НТУ оценивается в {_fmt(s['top_revenue'])} руб. в месяц при текущем среднем чеке."],
            "why": ["Отбор: высокий трафик (не меньше 3000 чеков за месяц) и конверсия ниже медианы сопоставимых АЗС."],
            "where": ["Список объектов с разрывом и эффектом — в таблице; сравнение внутри группы регион × формат."],
            "actions": ["Начать с верхних строк таблицы: разобрать выкладку и предложение НТУ на кассе; целевой показатель — конверсия НТУ до медианы группы."],
            "limitations": ["Оценка линейная: прочие условия неизменны, ёмкость спроса не учитывается.",
                            "Категории товаров и цены в стенде отсутствуют — потенциал по конкретному товару не оценивается."],
            "main_result": "p1",
        }

    trend_sql = (
        "SELECT period AS \"Месяц\", ROUND(SUM(checks)) AS \"Чеки, шт\", "
        "ROUND(SUM(checks) * 1.0 / COUNT(DISTINCT metric_date)) AS \"Чеков в день\" "
        "FROM station_kpi_daily WHERE metric_date >= date((SELECT MAX(metric_date) FROM station_kpi_daily), 'start of month', '-12 months') "
        "GROUP BY period ORDER BY period"
    )
    trend_py = (
        "full = r1.iloc[:-1] if len(r1) > 2 else r1\n"
        "t = trend(full, 'Месяц', 'Чеков в день')\n"
        "print('SUMMARY', json.dumps({'first': t['first'], 'last': t['last'], 'min': t['min'], 'max': t['max'], "
        "'direction': t['direction'], 'total_pct': t['total_change']['pct'], 'points': t['points']}, ensure_ascii=False))\n"
        "result = full.assign(**{'Скользящее 3 мес.': [p['y'] for p in t['rolling']]})"
    )

    def trend_final(evidence: str) -> dict:
        s = _summary(evidence)
        if not s:
            return {"headline": "Динамика чеков по месяцам — на графике.", "happened": []}
        return {
            "headline": f"За {s['points']} полных месяцев среднесуточное число чеков изменилось с {_fmt(s['first']['y'])} ({s['first']['x']}) "
                        f"до {_fmt(s['last']['y'])} ({s['last']['x']}), то есть на {_fmt(s['total_pct'], 1)} %; общий тренд — {s['direction']}.",
            "happened": [f"Максимум — {_fmt(s['max']['y'])} чеков в день в {s['max']['x']}, минимум — {_fmt(s['min']['y'])} в {s['min']['x']}."],
            "why": [], "where": [], "actions": [],
            "limitations": ["Последний неполный месяц из тренда исключён; ряд показан в чеках в день, чтобы месяцы разной длины были сопоставимы."],
            "main_result": "p1",
        }

    return {
        QUESTIONS[0]: {},  # быстрый путь: разбор не вызывается
        QUESTIONS[1]: {
            "triage": {"standalone_question": "Как изменилось количество чеков в текущем месяце относительно прошлого месяца",
                       "task_type": "compare", "depth": "analyze", "metrics": ["traffic"], "period": "текущий и прошлый месяц",
                       "steps": ["чеки за одинаковое число дней обоих месяцев", "абсолютное и относительное изменение, в день"]},
            "calls": [
                {"tool": "get_metric_definition", "arguments": {"metric": "трафик"}},
                {"tool": "run_sql", "arguments": {"sql": compare_sql, "purpose": "чеки за одинаковое число дней текущего и прошлого месяца"}},
                {"tool": "run_python", "arguments": {"code": compare_py, "inputs": ["r1"], "purpose": "изменение в штуках, процентах и в день"}},
            ],
            "final": compare_final,
        },
        QUESTIONS[2]: {
            "triage": {"standalone_question": "Какие 10 АЗС сильнее всего снизили количество чеков в текущем месяце относительно прошлого",
                       "task_type": "compare", "depth": "analyze", "metrics": ["traffic"], "period": "текущий и прошлый месяц",
                       "steps": ["изменение чеков по АЗС за одинаковое число дней", "топ-10 по снижению", "график"]},
            "calls": [
                {"tool": "run_sql", "arguments": {"sql": top_decline_sql, "purpose": "топ-10 АЗС по снижению чеков"}},
                {"tool": "create_chart", "arguments": {"source": "r1", "type": "bar", "title": "Снижение чеков по АЗС, %", "x": "Номер АЗС", "series": ["Изменение, %"], "unit": "%"}},
            ],
            "final": top_final,
        },
        QUESTIONS[3]: {
            "triage": {"standalone_question": "Почему снизилось количество чеков в текущем месяце относительно прошлого",
                       "task_type": "diagnose", "depth": "deep", "metrics": ["traffic", "active_stations", "checks_per_station"],
                       "period": "текущий и прошлый месяц",
                       "steps": ["чеки, работающие АЗС, чеки НТУ и топливо по месяцам", "по регионам", "по объектам", "тот же период год назад", "разложение на драйверы", "график вклада"]},
            "calls": [
                {"tool": "get_metric_definition", "arguments": {"metric": "трафик"}},
                {"tool": "run_sql", "arguments": {"sql": why_sql_1, "purpose": "чеки, работающие АЗС, чеки НТУ и топливо за одинаковые дни двух месяцев"}},
                {"tool": "run_sql", "arguments": {"sql": why_sql_2, "purpose": "изменение чеков по регионам"}},
                {"tool": "run_sql", "arguments": {"sql": why_sql_3, "purpose": "изменение чеков по каждой АЗС"}},
                {"tool": "run_sql", "arguments": {"sql": why_sql_4, "purpose": "те же месяцы год назад — сезонность"}},
                {"tool": "run_python", "arguments": {"code": why_py, "inputs": ["r1", "r2", "r3", "r4"], "purpose": "разложение изменения на число АЗС и чеки на АЗС, концентрация снижения, сезонность"}},
                {"tool": "create_chart", "arguments": {"source": "p1", "type": "waterfall", "title": "Вклад драйверов в изменение чеков", "x": "Драйвер", "series": ["Вклад, чеков"], "unit": "шт"}},
                {"tool": "create_chart", "arguments": {"source": "r2", "type": "bar", "title": "Изменение чеков по регионам", "x": "Регион", "series": ["Изменение, шт"], "unit": "шт"}},
            ],
            "final": why_final,
        },
        QUESTIONS[4]: {
            "triage": {"standalone_question": "Какие АЗС имеют наибольший потенциал роста конверсии НТУ относительно сопоставимых объектов",
                       "task_type": "opportunity", "depth": "deep", "metrics": ["conversion_ntu", "traffic", "avg_check_ntu"],
                       "period": "последний полный месяц",
                       "steps": ["конверсия и трафик по АЗС", "медиана по группе регион × формат", "разрыв и эффект", "график"]},
            "calls": [
                {"tool": "get_metric_definition", "arguments": {"metric": "конверсия НТУ"}},
                {"tool": "run_sql", "arguments": {"sql": potential_sql, "purpose": "конверсия НТУ, трафик и средний чек по крупным АЗС за последний полный месяц"}},
                {"tool": "run_python", "arguments": {"code": potential_py, "inputs": ["r1"], "purpose": "разрыв до медианы группы и эффект подтягивания"}},
                {"tool": "create_chart", "arguments": {"source": "p1", "type": "bar", "title": "Потенциал: дополнительные чеки с НТУ в месяц", "x": "Номер АЗС", "series": ["Доп. чеков с НТУ"], "unit": "шт"}},
            ],
            "final": potential_final,
        },
        QUESTIONS[5]: {
            "triage": {"standalone_question": "Покажи динамику количества чеков по месяцам за последние 12 месяцев и построй график",
                       "task_type": "trend", "depth": "analyze", "metrics": ["traffic", "checks_per_day"], "period": "последние 12 месяцев",
                       "steps": ["чеки по месяцам и в день", "тренд, максимум и минимум", "график"]},
            "calls": [
                {"tool": "run_sql", "arguments": {"sql": trend_sql, "purpose": "чеки по месяцам за последние 12 месяцев"}},
                {"tool": "run_python", "arguments": {"code": trend_py, "inputs": ["r1"], "purpose": "тренд по полным месяцам"}},
                {"tool": "create_chart", "arguments": {"source": "p1", "type": "line", "title": "Чеков в день по месяцам", "x": "Месяц", "series": ["Чеков в день", "Скользящее 3 мес."], "unit": "шт/день"}},
            ],
            "final": trend_final,
        },
    }


FAST_SQL = {
    QUESTIONS[0]: "SELECT ROUND(SUM(checks)) AS \"Чеки за вчера, шт\" FROM station_kpi_daily WHERE metric_date = date('now', '-1 day')",
}


# --- вывод -----------------------------------------------------------------

def show(answer: pipeline.Answer, verbose: bool = False) -> None:
    head = "ОТВЕТ " if answer.ok else "ОТКАЗ "
    print(f"\n{head}| {answer.scope_label} | глубина {answer.depth} | задача {answer.task_type} "
          f"| модель {answer.model_ms} мс, витрина {answer.sql_ms} мс, ходов {answer.attempts}")
    if not answer.ok:
        print(f"  причина: {answer.rule} — {answer.error}")
    if answer.analysis:
        a = answer.analysis
        print(f"  ГЛАВНОЕ: {a.get('headline')}")
        for title, key in (("Что произошло", "happened"), ("Почему", "why"), ("Где именно", "where"),
                           ("Что можно сделать", "actions"), ("Ограничения", "limitations")):
            if a.get(key):
                print(f"  {title}:")
                for item in a[key]:
                    print(f"    • {item}")
    elif answer.summary:
        print(f"  {answer.summary}")
    if answer.columns:
        print(f"  Таблица: {' | '.join(answer.columns)}")
        for row in answer.rows[:6]:
            print("    " + " | ".join("—" if c is None else (f"{c:,.1f}".replace(",", " ") if isinstance(c, float) else str(c)) for c in row))
        if len(answer.rows) > 6:
            print(f"    … всего строк: {len(answer.rows)}")
    for table in answer.tables:
        print(f"  Ещё таблица {table['id']}: {table['title']} — {len(table['rows'])} строк")
    for chart in answer.charts:
        points = len(chart.get("x") or chart.get("points") or chart.get("cards") or [])
        print(f"  График {chart['id']}: {chart['type']} «{chart['title']}», точек {points}")
    if answer.notes:
        for note in answer.notes:
            print(f"  ⓘ {note}")
    if answer.steps:
        print("  Шаги:")
        for step in answer.steps:
            mark = "✓" if step.get("ok") else "✗"
            print(f"    {mark} {step['label']}" + (f" — {step['ms']} мс" if step.get("ms") else ""))
            if verbose and step.get("sql"):
                print("       " + " ".join(step["sql"].split())[:200])
            if verbose and step.get("error"):
                print("       ошибка: " + str(step["error"])[:200])
            for warning in step.get("warnings") or []:
                print(f"       ⚠ {warning}")
    if answer.grounding:
        g = answer.grounding
        print(f"  Сверка чисел: проверено {g.get('checked')}, не подтверждено {len(g.get('unverified') or [])}, убрано фраз {len(g.get('removed') or [])}")
        if g.get("unverified"):
            print(f"    не подтверждено: {', '.join(g['unverified'])}")
        for phrase in g.get("removed") or []:
            print(f"    убрано: {phrase[:160]}")


def run_dialog(questions: list[str], role: str, binding: str | None, depth: str, model: str | None,
               verbose: bool) -> int:
    history: list[dict] = []
    failures = 0
    for question in questions:
        print(f"\n=== {question}   [{role}, глубина {depth}]")
        started = time.monotonic()
        answer = pipeline.ask(question, role, binding, "agent-demo", model=model, depth=depth, history=history)
        show(answer, verbose)
        print(f"  всего {time.monotonic() - started:.1f} с")
        failures += 0 if answer.ok else 1
        history.append({"question": question, "answer": {
            "ok": answer.ok, "sql": answer.sql, "summary": answer.summary, "columns": answer.columns,
            "error": answer.error, "rule": answer.rule, "frame": answer.frame,
        }, "frame": answer.frame})
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон аналитического агента")
    parser.add_argument("--question", help="один вопрос вместо шести")
    parser.add_argument("--depth", default="auto", choices=list(pipeline.DEPTHS))
    parser.add_argument("--role", default="admin")
    parser.add_argument("--binding", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--scripted", action="store_true", help="сценарная модель вместо Ollama")
    parser.add_argument("--verbose", action="store_true", help="показывать SQL шагов")
    args = parser.parse_args()

    if args.scripted:
        scenarios = build_scenarios()
        model = ScenarioModel(scenarios)
        import backend.ai.pipeline as pipe
        pipe.MODEL_FACTORY = lambda _name: model

        def fake_generate(question, feedback=None, _model=None):
            sql = FAST_SQL.get(question) or "SELECT ROUND(SUM(checks)) AS \"Чеки, шт\" FROM station_kpi_daily WHERE period = strftime('%Y-%m', 'now')"
            return generator.Generated(sql=sql, raw=sql, model="scripted", elapsed_ms=1)

        def fake_narrate(question, scope, columns, rows, _model=None):
            if rows and rows[0] and rows[0][0] is not None:
                return f"{columns[0]}: {rows[0][0]:,.0f}".replace(",", " "), 1
            return "", 0

        pipe.generator.generate = fake_generate
        pipe.generator.narrate = fake_narrate
        questions = [args.question] if args.question else QUESTIONS
        for question in questions:
            if question not in scenarios:
                print(f"Для вопроса «{question}» нет сценария; доступны: {QUESTIONS}")
                return 1
        history: list[dict] = []
        failures = 0
        for question in questions:
            model.use(question)
            print(f"\n=== {question}   [{args.role}, глубина {args.depth}] — сценарий")
            started = time.monotonic()
            answer = pipeline.ask(question, args.role, args.binding, "agent-demo", depth=args.depth, history=history)
            show(answer, args.verbose)
            print(f"  всего {time.monotonic() - started:.1f} с")
            failures += 0 if answer.ok else 1
            history.append({"question": question, "answer": {"ok": answer.ok, "sql": answer.sql, "summary": answer.summary,
                                                             "columns": answer.columns, "frame": answer.frame}, "frame": answer.frame})
        pipe.MODEL_FACTORY = None
        print(f"\nСценарный прогон: {len(questions) - failures} из {len(questions)} вопросов дали ответ.")
        return 1 if failures else 0

    if not generator.available(args.model):
        print(f"Модель недоступна на {generator.OLLAMA_HOST}. Запустите Ollama и загрузите модель: ollama pull {args.model or generator.MODEL}")
        print("Без модели можно проверить контур сценарием: --scripted")
        return 1
    print(f"Модель: {args.model or generator.MODEL} на {generator.OLLAMA_HOST}\n" + "-" * 72)
    questions = [args.question] if args.question else QUESTIONS
    failures = run_dialog(questions, args.role, args.binding, args.depth, args.model, args.verbose)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
