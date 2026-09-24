"""СП-05. PDF справки: A4, корпоративный стиль, из JSON модели без пересчёта.

ReportLab — чистый Python без браузера (сервер 1 vCPU, 957 МБ). Шрифт — PT Sans
(ParaType Free Font License, лицензия рядом в `fonts/`): решение Р-14 — Tahoma
только при лицензии на встраивание, иначе PT Sans. Когда лицензия на Tahoma
будет, код не меняется: пути к её файлам — в `REPORT_FONT_REGULAR` и
`REPORT_FONT_BOLD`. Шрифт встраивается подмножеством, файл получается небольшим.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.shapes import Drawing, Line, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from . import fmt, narrative

FONTS = Path(__file__).resolve().parent / "fonts"
FONT, FONT_BOLD = "Report", "Report-Bold"
RED = colors.HexColor("#E31E24")
TEXT = colors.HexColor("#1A1F29")
MUTED = colors.HexColor("#646C7D")
LINE = colors.HexColor("#D9DDE3")
SOFT = colors.HexColor("#F4F5F7")
GREY_SERIES = colors.HexColor("#9AA1AE")
FOOTER = "LUKOIL ОНПО · Инструмент АУП · Конфиденциально"

_registered = False


def font_files() -> tuple[Path, Path]:
    """PT Sans из пакета; Tahoma (или другой шрифт) — только если заданы оба пути."""
    regular = os.environ.get("REPORT_FONT_REGULAR", "").strip()
    bold = os.environ.get("REPORT_FONT_BOLD", "").strip()
    if regular and bold:
        return Path(regular), Path(bold)
    return FONTS / "PTSans-Regular.ttf", FONTS / "PTSans-Bold.ttf"


def _fonts() -> None:
    global _registered
    if _registered:
        return
    regular, bold = font_files()
    pdfmetrics.registerFont(TTFont(FONT, str(regular)))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(bold)))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)
    _registered = True


def _styles() -> dict:
    base = dict(fontName=FONT, alignment=TA_LEFT)
    return {
        "title": ParagraphStyle("title", fontName=FONT_BOLD, fontSize=17, leading=21, textColor=TEXT),
        "meta": ParagraphStyle("meta", **base, fontSize=8.5, leading=11.5, textColor=MUTED),
        "h2": ParagraphStyle("h2", fontName=FONT_BOLD, fontSize=9, leading=12, textColor=RED,
                             spaceBefore=9, spaceAfter=4),
        "body": ParagraphStyle("body", **base, fontSize=9.5, leading=13.5, textColor=TEXT),
        "small": ParagraphStyle("small", **base, fontSize=7.5, leading=10, textColor=MUTED),
        "cell": ParagraphStyle("cell", **base, fontSize=8, leading=10, textColor=TEXT),
        "cellb": ParagraphStyle("cellb", fontName=FONT_BOLD, fontSize=8, leading=10, textColor=TEXT),
        "cellr": ParagraphStyle("cellr", fontName=FONT, fontSize=8, leading=10, textColor=TEXT, alignment=TA_RIGHT),
        "cellbr": ParagraphStyle("cellbr", fontName=FONT_BOLD, fontSize=8, leading=10, textColor=TEXT, alignment=TA_RIGHT),
    }


def _h(text: str, st) -> Paragraph:
    return Paragraph(text.upper(), st["h2"])


def _table(rows: list[list], widths: list[float], st, numeric_from: int = 1, total_last: bool = False) -> Table:
    def style_of(i: int, j: int):
        right = j >= numeric_from
        if i == 0:
            return st["cellbr"] if right else st["cellb"]
        return st["cellr"] if right else st["cell"]

    data = [[Paragraph(str(c), style_of(i, j)) if not isinstance(c, Paragraph) else c
             for j, c in enumerate(row)] for i, row in enumerate(rows)]
    table = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, TEXT),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"),
    ]
    if total_last:
        style.append(("BACKGROUND", (0, -1), (-1, -1), SOFT))
    table.setStyle(TableStyle(style))
    return table


def _num_cell(text: str, st) -> Paragraph:
    return Paragraph(f'<para alignment="right">{text}</para>', st["cell"])


def _chart(chart: dict, width: float, height: float) -> Drawing:
    drawing = Drawing(width, height)
    lc = HorizontalLineChart()
    lc.x, lc.y = 38, 22
    lc.width, lc.height = width - 48, height - 56
    series = [[v if v is not None else 0 for v in s["values"]] for s in chart["series"]]
    lc.data = series
    # Подпись точки — понедельник недели («07.09»): восемь интервалов «07.09–13.09» не помещаются.
    lc.categoryAxis.categoryNames = [label.split("–")[0] for label in chart["x"]]
    lc.categoryAxis.labels.fontName = FONT
    lc.categoryAxis.labels.fontSize = 6
    lc.categoryAxis.labels.fillColor = MUTED
    lc.categoryAxis.strokeColor = LINE
    lc.valueAxis.labels.fontName = FONT
    lc.valueAxis.labels.fontSize = 6
    lc.valueAxis.labels.fillColor = MUTED
    lc.valueAxis.strokeColor = LINE
    lc.valueAxis.gridStrokeColor = LINE
    lc.valueAxis.visibleGrid = True
    peak = max((v for s in series for v in s), default=0)
    scale, suffix = (1e6, "млн") if peak >= 1e6 else ((1e3, "тыс.") if peak >= 1e4 else (1, ""))
    lc.valueAxis.labelTextFormat = lambda v: fmt.number(v / scale, 0 if scale == 1 else 1)
    lc.valueAxis.valueMin = 0
    lc.lines[0].strokeColor = RED
    lc.lines[0].strokeWidth = 1.6
    if len(series) > 1:
        lc.lines[1].strokeColor = GREY_SERIES
        lc.lines[1].strokeWidth = 1.2
        lc.lines[1].strokeDashArray = (3, 2)
    drawing.add(lc)
    base = chart["title"].rsplit(",", 1)[0]
    unit = fmt.unit(chart.get("unit", ""))
    title = f"{base}, {suffix + ' ' if suffix else ''}{unit}".strip()
    drawing.add(String(0, height - 10, title, fontName=FONT_BOLD, fontSize=7.5, fillColor=TEXT))
    x = 0
    for i, s in enumerate(chart["series"]):
        color = RED if i == 0 else GREY_SERIES
        drawing.add(Line(x, height - 23, x + 12, height - 23, strokeColor=color, strokeWidth=1.6))
        drawing.add(String(x + 15, height - 25, s["name"], fontName=FONT, fontSize=6.5, fillColor=MUTED))
        x += 30 + 4.2 * len(s["name"])
    return drawing


def version_label(model: dict) -> str:
    """«версия 2» — выпуск с сайта. Копия, собранная на компьютере владельца до публикации, помечена отдельно."""
    return "копия с компьютера, до публикации" if model.get("localCopy") else f"версия {model.get('version', 1)}"


def _header_footer(model: dict):
    generated = model["passport"]["generatedAt"]
    stamp = datetime.fromisoformat(generated).strftime("%d.%m.%Y %H:%M МСК")
    issue = f"Выпуск {model['week']['iso']} · {version_label(model)}"

    def draw(canvas, doc):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(RED)
        canvas.rect(0, h - 6 * mm, w, 6 * mm, stroke=0, fill=1)
        canvas.setFont(FONT, 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 9 * mm, FOOTER)
        canvas.drawRightString(w - 15 * mm, 9 * mm, f"{issue} · сформирована {stamp} · стр. {doc.page}")
        canvas.setStrokeColor(LINE)
        canvas.line(15 * mm, 12 * mm, w - 15 * mm, 12 * mm)
        canvas.restoreState()
    return draw


def render(model: dict, path: Path) -> Path:
    _fonts()
    st = _styles()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=14 * mm, bottomMargin=17 * mm,
                            title=f"{model['title']} · неделя {model['week']['label']}", author="Инструмент АУП")
    width = A4[0] - 30 * mm
    passport = model["passport"]
    story = []

    # 1. Шапка
    story.append(Paragraph(f"{model['scopeLabel']} · неделя {model['week']['label']}", st["title"]))
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        f"{model['title']} · выпуск {model['week']['iso']}, {version_label(model)} · "
        f"данные по {_date(passport['latestDate'])} · объектов с продажами за неделю: {fmt.number(passport['stationsWeek'])} · "
        f"сравнение: прошлая неделя {model['compare']['prev']['label']}, прошлый год {model['compare']['lastYear']['label']}",
        st["meta"]))
    if model.get("reason"):
        story.append(Paragraph(f"Перевыпуск: {model['reason']}", st["meta"]))

    # 2. Главное
    story.append(_h("Главное", st))
    for sentence in narrative.headline(model):
        story.append(Paragraph(escape(sentence), st["body"]))

    # 3. Цифры недели
    rows = [["Показатель", "Неделя", "Прошлая неделя", "Δ н/н", "Прошлый год", "Δ г/г"]]
    for m in model["metrics"]:
        rows.append([f"{m['title']}, {fmt.unit(m['unit'])}",
                     _num_cell(fmt.number(m["value"], m["decimals"]), st),
                     _num_cell(fmt.number(m["prev"], m["decimals"]), st),
                     _num_cell(fmt.delta(m["deltaPrev"], m["isShare"]), st),
                     _num_cell(fmt.number(m["lastYear"], m["decimals"]), st),
                     _num_cell(fmt.delta(m["deltaYear"], m["isShare"]), st)])
    story.append(KeepTogether([_h("Цифры недели", st),
                               _table(rows, [width * 0.30] + [width * 0.14] * 5, st)]))
    story.append(Paragraph("Значения — по всей сети; изменения — по сопоставимой базе (объекты с 7/7 днями в обоих периодах); "
                           "для долей — в процентных пунктах.", st["small"]))

    # 3а. План месяца (СП-07): выполнение, % дней, отставание, темп
    for month in plan_months(model):
        story.append(KeepTogether(_plan_block(month, model.get("plan") or {}, width, st)))

    # 4. Динамика 8 недель
    charts = model["dynamics"]["charts"]
    if charts:
        story.append(_h("Динамика 8 недель", st))
        drawings = [_chart(c, width / len(charts) - 4, 62 * mm) for c in charts]
        table = Table([drawings], colWidths=[width / len(charts)] * len(charts))
        table.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(table)

    # 5. ОНПО
    unit = fmt.unit(model["fuelUnit"])
    rows = [["ОНПО", "АЗС", f"Топливо, {unit}", "Δ н/н", "Δ г/г", "Выручка НТУ, ₽", "Δ н/н", "Δ г/г", "Конверсия НТУ"]]
    for r in model["onpo"] + [dict(model["onpoTotal"], name="Итого по сети")]:
        rows.append([r["name"], _num_cell(fmt.number(r.get("stations")), st),
                     _num_cell(fmt.number(r.get("fuel")), st), _num_cell(fmt.delta(r.get("fuelDeltaPrev")), st),
                     _num_cell(fmt.delta(r.get("fuelDeltaYear")), st), _num_cell(fmt.number(r.get("ntu")), st),
                     _num_cell(fmt.delta(r.get("ntuDeltaPrev")), st), _num_cell(fmt.delta(r.get("ntuDeltaYear")), st),
                     _num_cell(fmt.value(r.get("conversion"), "%", 1), st)])
    story.append(KeepTogether([_h("ОНПО сети", st), _table(
        rows, [width * 0.16, width * 0.08, width * 0.13, width * 0.09, width * 0.09, width * 0.14, width * 0.09,
               width * 0.09, width * 0.13], st, total_last=True)]))

    # 6. Зоны внимания: сначала текст ИИ «На что обратить внимание» (СП-06), потом таблицы
    notes = narrative.attention(model)
    if notes:
        story.append(KeepTogether([_h("На что обратить внимание", st)] +
                                  [Paragraph("• " + escape(line), st["body"]) for line in notes]))
    att = model["attention"]
    th = passport["thresholds"]
    heading = _h("Зоны внимания", st)
    if att["drops"]:
        rows = [["Объект", "Регион", "ОНПО", f"Топливо, {unit}", "Прошлая неделя", "Δ н/н"]]
        for r in att["drops"]:
            rows.append([r["label"], r["region"], r["onpo"], _num_cell(fmt.number(r["fuel"]), st),
                         _num_cell(fmt.number(r["fuelPrev"]), st), _num_cell(fmt.delta(r["delta"]), st)])
        story.append(KeepTogether([heading, Paragraph(
            f"Падение реализации топлива к прошлой неделе на {fmt.number(th['fuelDropPct'])} % и больше "
            f"— всего {att['dropsTotal']}, показаны {len(att['drops'])} с наибольшим падением", st["small"]),
            _table(rows, [width * 0.16, width * 0.26, width * 0.14, width * 0.15, width * 0.15, width * 0.14],
                   st, numeric_from=3)]))
    else:
        story.append(heading)
        story.append(Paragraph(f"Объектов с падением топлива на {fmt.number(th['fuelDropPct'])} % и больше нет.", st["body"]))
    story.append(Spacer(1, 4))
    if att["noSales"]:
        rows = [["Объект", "Регион", "ОНПО", "Дней без продаж"]]
        for r in att["noSales"][:30]:
            rows.append([r["label"], r["region"], r["onpo"], _num_cell(str(r["idleDays"]), st)])
        story.append(KeepTogether([Paragraph(
            f"Действующие объекты без продаж {fmt.days(th['noSalesDays'])} и больше за неделю — {len(att['noSales'])}",
            st["small"]), _table(rows, [width * 0.16, width * 0.26, width * 0.14, width * 0.44], st, numeric_from=3)]))
    story.append(Spacer(1, 4))
    if att["leaders"]:
        rows = [["Объект", "Регион", "ОНПО", f"Топливо, {unit}", "Прошлая неделя", "Δ н/н"]]
        for r in att["leaders"]:
            rows.append([r["label"], r["region"], r["onpo"], _num_cell(fmt.number(r["fuel"]), st),
                         _num_cell(fmt.number(r["fuelPrev"]), st), _num_cell(fmt.delta(r["delta"]), st)])
        story.append(KeepTogether([Paragraph("Лидеры роста реализации топлива к прошлой неделе", st["small"]),
                                   _table(rows, [width * 0.16, width * 0.26, width * 0.14, width * 0.15, width * 0.15,
                                                 width * 0.14], st, numeric_from=3)]))

    # 6а. Рекомендации ИИ — справочно (ИИ-16, СП-06)
    recs = narrative.recommendations(model)
    if recs:
        block = [_h("Рекомендации (справочно)", st)]
        for rec in recs:
            block.append(Paragraph("• " + escape(rec["action"]), st["body"]))
            block.append(Paragraph(f"Основание: {escape(rec['basis'])} Ожидаемый эффект: {escape(rec['effect'])} "
                                   f"Ограничения: {escape(rec['limits'])}", st["small"]))
        block.append(Paragraph(narrative.RECOMMENDATION_NOTE, st["small"]))
        story.append(KeepTogether(block))

    # 7. Что пока не входит
    if model["pending"]:
        story.append(_h("Что пока не входит", st))
        story.append(Paragraph(", ".join(model["pending"]) + " — появятся в справке после дополнения витрины. "
                               "Простои топлива в справку не входят, пока их данные не актуальны.", st["body"]))

    # 8. Паспорт
    story.append(_h("Паспорт данных", st))
    for line in passport_lines(model):
        story.append(Paragraph(escape(line), st["small"]))

    doc.build(story, onFirstPage=_header_footer(model), onLaterPages=_header_footer(model))
    return path


def plan_months(model: dict) -> list[dict]:
    return list((model.get("plan") or {}).get("months") or [])


def plan_caption(month: dict) -> str:
    """Подпись месяца блока плана — общая для экрана, PDF и Excel."""
    if month["closed"]:
        return f"{month['label'].capitalize()} — итоги месяца"
    return (f"{month['label'].capitalize()}: прошло {month['daysPassed']} из {month['daysTotal']} дней "
            f"({fmt.number(month['daysPct'], 1)} %)")


def _plan_value(value, row: dict) -> str:
    return "—" if value is None else fmt.number(value, row["decimals"])


def _plan_block(month: dict, plan: dict, width: float, st) -> list:
    block = [_h("План месяца" if not month["closed"] else "Итоги месяца по плану", st),
             Paragraph(escape(plan_caption(month)), st["small"])]
    to_date = not month["planComplete"] and not month["closed"]
    if month["closed"]:
        rows = [["Показатель", "Факт за месяц", "План месяца", "Выполнение, %", "Отклонение"]]
        for r in month["rows"]:
            rows.append([f"{r['title']}, {fmt.unit(r['unit'])}", _num_cell(_plan_value(r["fact"], r), st),
                         _num_cell(_plan_value(r["planMonth"], r), st), _num_cell(fmt.number(r["pctMonth"], 1), st),
                         _num_cell(fmt.delta(r["gapPp"], share=True), st)])
        widths = [width * 0.28] + [width * 0.18] * 4
    elif to_date:
        rows = [["Показатель", "Факт с начала месяца", "План на дату", "Выполнение на дату, %"]]
        for r in month["rows"]:
            rows.append([f"{r['title']}, {fmt.unit(r['unit'])}", _num_cell(_plan_value(r["fact"], r), st),
                         _num_cell(_plan_value(r["planToDate"], r), st), _num_cell(fmt.number(r["pctToDate"], 1), st)])
        widths = [width * 0.34] + [width * 0.22] * 3
    else:
        rows = [["Показатель", "Факт с начала месяца", "План месяца", "Выполнение, %", "Отставание",
                 "Нужно в день", "Сейчас в день"]]
        for r in month["rows"]:
            rows.append([f"{r['title']}, {fmt.unit(r['unit'])}", _num_cell(_plan_value(r["fact"], r), st),
                         _num_cell(_plan_value(r["planMonth"], r), st), _num_cell(fmt.number(r["pctMonth"], 1), st),
                         _num_cell(fmt.delta(r["gapPp"], share=True), st), _num_cell(_plan_value(r["needPerDay"], r), st),
                         _num_cell(_plan_value(r["pacePerDay"], r), st)])
        widths = [width * 0.22, width * 0.14, width * 0.14, width * 0.11, width * 0.12, width * 0.14, width * 0.13]
    block.append(_table(rows, widths, st))
    if month["onpo"]:
        head = ["ОНПО", "Топливо, % плана", "Выручка НТУ, % плана", "Отставание по НТУ", "ВД НТУ, % плана", "Статус"]
        rows = [head] + [[o["name"], _num_cell(fmt.number(o.get("fuelPct"), 1), st),
                          _num_cell(fmt.number(o.get("ntuPct"), 1), st),
                          _num_cell(fmt.delta(o.get("ntuGap"), share=True), st),
                          _num_cell(fmt.number(o.get("vdPct"), 1), st), o.get("status") or "—"]
                         for o in month["onpo"]]
        block.append(Spacer(1, 4))
        block.append(_table(rows, [width * 0.2, width * 0.16, width * 0.18, width * 0.18, width * 0.16, width * 0.12],
                            st, numeric_from=1))
    notes = []
    if plan.get("note") and to_date:
        notes.append(plan["note"].capitalize() + ".")
    if not month["closed"] and not to_date:
        notes.append("Отставание — % плана минус % прошедших дней; статус по выручке НТУ: до 3 п. п. — норма, "
                     "3–8 — внимание, больше 8 — критично.")
    for note in notes:
        block.append(Paragraph(escape(note), st["small"]))
    return block


def _date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y") if iso else "—"


def passport_lines(model: dict) -> list[str]:
    """Строки паспорта — общие для PDF и XLSX."""
    p = model["passport"]
    c = p["completeness"]
    lines = [
        f"Отчётная неделя: {p['week']['label']} ({p['week']['iso']}); прошлая неделя: {p['prev']['label']}; "
        f"та же неделя прошлого года: {p['lastYear']['label']}.",
        f"Охват: объектов с продажами за неделю — {fmt.number(p['stationsWeek'])}; сопоставимая база к прошлой неделе — "
        f"{fmt.number(p['comparablePrev'])}, исключено {fmt.number(p['excludedPrev'])}; к прошлому году — "
        f"{fmt.number(p['comparableYear'])}, исключено {fmt.number(p['excludedYear'])}.",
        f"Полнота: все 7 дней есть у {fmt.number(c['sharePct'], 1)} % объектов, работавших всю прошлую неделю "
        f"(порог публикации — {fmt.number(c['thresholdPct'])} %).",
        f"Свежесть: данные по {_date(p['latestDate'])}. Источник: {p['source']}. {p['fuelUnitNote']}.",
        f"Пороги зон внимания: падение топлива на {fmt.number(p['thresholds']['fuelDropPct'])} % и больше; "
        f"без продаж {fmt.days(p['thresholds']['noSalesDays'])} и больше; объекты с базой меньше "
        f"{fmt.number(p['thresholds']['minBaseShareOfMedian'] * 100)} % медианы сети не ранжируются.",
    ]
    if p["holidays"]:
        days = "; ".join(f"{_date(h['date'])} — {h['name']}" for h in p["holidays"])
        lines.append(f"Праздничные дни в сравниваемых неделях: {days}.")
    lines += p["rules"]
    if p.get("planNote"):
        lines.append(p["planNote"])
    text_line = narrative.passport_line(model)
    if text_line:
        lines.append(text_line)
    lines.append(f"Версия формул {p['formulasVersion']} · прогон {p['runId']} · сформирована "
                 f"{datetime.fromisoformat(p['generatedAt']).strftime('%d.%m.%Y %H:%M')} МСК · Конфиденциально.")
    return lines
