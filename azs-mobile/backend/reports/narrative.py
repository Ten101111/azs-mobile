"""СП-06. Текст справки от ИИ поверх готовых цифр.

Модель не ходит в базу: она получает сводку выпуска (числа уже посчитаны
кодом и записаны по-русски) и пишет три части — «Главное», «На что обратить
внимание» и до двух рекомендаций. Решает код, а не модель:

- каждое число текста есть в модели выпуска; проценты и п. п. сверяются только
  с изменениями и долями, остальные числа — только со значениями, счётчиками,
  датами и номерами объектов;
- в «Главном» и «Внимании» — только факты (ИИ-25): предположения и причины
  («из-за», «вероятно», «может») текст отклоняют;
- названия в кавычках «…» — только те, что есть в выпуске (ОНПО, регионы,
  объекты, показатели);
- рекомендации проходят правила ИИ-16 (поля Р-9, стоп-лист, закрытые темы: без
  акций и скидок, без оценки и санкций к работникам), их числа — тоже из
  выпуска; рекомендация, не прошедшая проверку, просто не показывается.

Ошибка в «Главном» или «Внимании» — откат ко всему шаблонному тексту: выпуск
выходит всегда, текст ИИ — улучшение, а не условие публикации.

ИИ-контур работает на компьютере владельца, а справку собирает сервер, поэтому
модель вызывает не сервер: `task()` отдаёт готовый запрос к Ollama, скрипт
`scripts/report_text.py` на маке выполняет его и возвращает сырой ответ, а
сервер сам разбирает и сверяет его в `apply()` — клиенту он не доверяет.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from types import SimpleNamespace

from ..ai.agent import grounding, recommend
from ..ai.agent.claims import HYPOTHESIS_RE
from . import fmt

PROMPT_VERSION = "2026-09-24.1"
AI, TEMPLATE = "ai", "template"
MAX_HEADLINE = 6
MAX_ATTENTION = 4
MAX_RECOMMENDATIONS = 2
MAX_LINE = 400
MAX_RAW = 20_000
TOP_ROWS = 5
NUM_PREDICT = 1400
RECOMMENDATION_NOTE = "Рекомендации носят справочный характер; решение принимает руководитель."
SOURCE_TITLE = "Справка по сети: зоны внимания"
# Даты вида 15.09 или 15.09.2025 — не числа справки: иначе «15.09» читается как 15,09.
DATE_RE = re.compile(r"(?<![\w.,])(\d{1,2})\.(\d{2})(?:\.(\d{4}))?(?![\w.,]*\d)")
QUOTED_RE = re.compile(r"«([^»]{1,80})»")
LATIN_ID_RE = re.compile(r"[A-Za-z]+_[A-Za-z_]+")
BULLET_RE = re.compile(r"^\s*(?:[-–—•*]|\d{1,2}[.)])\s+")
VOLATILE = ("runId", "generatedAt", "durationMs")

SYSTEM = """Ты пишешь текст еженедельной справки по сети АЗС для руководителей. Цифры уже посчитаны и лежат в сводке — ты только излагаешь их связным деловым русским языком.

Правила:
1. Бери числа только из сводки и записывай их так же, как в ней; крупные суммы можно округлить («76,5 млн ₽»). Не вычисляй новых чисел: никаких сумм, разностей, долей и средних.
2. Только факты из сводки. Не объясняй причины и не строй предположений: без слов «из-за», «благодаря», «вероятно», «возможно», «может», «связано», «вызвано».
3. Названия ОНПО, регионов и объектов пиши так же, как в сводке; ОНПО — в кавычках-ёлочках: ОНПО «Юг».
4. Без заголовков, markdown, эмодзи и английских слов.
5. headline — от 3 до 6 предложений: реализация топлива и выручка НТУ за неделю с изменением к прошлой неделе и к той же неделе прошлого года; если в сводке есть план месяца — его выполнение по выручке НТУ и топливу рядом с долей прошедших дней и отставание в п. п.; ОНПО с лучшей и худшей динамикой топлива; сколько объектов в зоне внимания.
6. attention — от 1 до 4 коротких пунктов о зоне внимания: какие объекты сильнее всего потеряли в топливе и где не было продаж. Если в зоне внимания нет объектов — пустой список.
7. recommendations — не больше двух и только если в зоне внимания есть объекты. Каждая: action — осторожное действие для руководителя («стоит проверить…», «имеет смысл уточнить…»); basis — факт из сводки с числом; effect — чего ждать, без новых чисел; limits — когда рекомендация не подходит. Не предлагай акции, скидки, промо и бонусы клиентам; не предлагай оценивать, наказывать или премировать работников. Если рекомендовать нечего — пустой список.

Ответ — только JSON: {"headline": ["..."], "attention": ["..."], "recommendations": [{"action": "...", "basis": "...", "effect": "...", "limits": "..."}]}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "array", "items": {"type": "string"}},
        "attention": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in ("action", "basis", "effect", "limits")},
            "required": ["action", "basis", "effect", "limits"],
        }},
    },
    "required": ["headline", "attention", "recommendations"],
}


class Rejected(Exception):
    """Текст ИИ не прошёл сверку — публикуется шаблон."""


# --- сводка для модели -------------------------------------------------------

def _zone(att: dict) -> int:
    return len(set(att.get("dropKeys") or [r["key"] for r in att.get("drops", [])]) | {r["key"] for r in att.get("noSales", [])})


def facts(model: dict) -> dict:
    """Всё, что модели можно знать о неделе: числа уже записаны по-русски."""
    unit = model.get("fuelUnit") or "л"
    th = model["passport"]["thresholds"]

    def metric(m: dict) -> dict:
        v = lambda x: "нет данных" if x is None else fmt.value(x, m["unit"], m["decimals"])  # noqa: E731
        return {
            "показатель": m["title"], "за неделю": v(m["value"]), "прошлая неделя": v(m["prev"]),
            "изменение к прошлой неделе": fmt.delta(m["deltaPrev"], m["isShare"]),
            "та же неделя прошлого года": v(m["lastYear"]),
            "изменение к той же неделе прошлого года": fmt.delta(m["deltaYear"], m["isShare"]),
        }

    def onpo(r: dict) -> dict:
        return {
            "ОНПО": r["name"], "АЗС": r.get("stations"),
            "топливо": fmt.value(r.get("fuel"), unit), "топливо к прошлой неделе": fmt.delta(r.get("fuelDeltaPrev")),
            "топливо к прошлому году": fmt.delta(r.get("fuelDeltaYear")),
            "выручка НТУ": fmt.value(r.get("ntu"), "руб"), "выручка НТУ к прошлой неделе": fmt.delta(r.get("ntuDeltaPrev")),
            "выручка НТУ к прошлому году": fmt.delta(r.get("ntuDeltaYear")),
            "конверсия НТУ": fmt.value(r.get("conversion"), "%", 1),
        }

    def station(r: dict) -> dict:
        out = {"объект": r["label"], "регион": r.get("region") or "", "ОНПО": r.get("onpo") or ""}
        if "idleDays" in r:
            out["дней без продаж"] = r["idleDays"]
        else:
            out.update({"топливо за неделю": fmt.value(r["fuel"], unit), "прошлая неделя": fmt.value(r["fuelPrev"], unit),
                        "изменение": fmt.delta(r["delta"])})
        return out

    att = model["attention"]
    drop_rule = f"падение топлива к прошлой неделе на {fmt.number(th['fuelDropPct'])} % и больше"
    idle_rule = f"без продаж {fmt.days(th['noSalesDays'])} и больше"
    return {
        "справка": f"{model['title']} — {model['scopeLabel'].lower()}",
        "неделя": model["week"]["label"],
        "сравнение": {"прошлая неделя": model["compare"]["prev"]["label"],
                      "та же неделя прошлого года": model["compare"]["lastYear"]["label"]},
        "показатели": [metric(m) for m in model["metrics"]],
        "ОНПО": [onpo(r) for r in model["onpo"]],
        "итого по сети": onpo(dict(model["onpoTotal"], name="вся сеть")),
        "зона внимания": {
            "объектов в зоне внимания": _zone(att),
            drop_rule: att["dropsTotal"],
            idle_rule: len(att["noSales"]),
            "самые сильные падения топлива": [station(r) for r in att["drops"][:TOP_ROWS]],
            "без продаж": [station(r) for r in att["noSales"][:TOP_ROWS]],
            "лидеры роста топлива": [station(r) for r in att["leaders"][:3]],
        },
        "план месяца": _plan_facts(model.get("plan")),
        "праздники": [f"{h['date']} — {h['name']}" for h in model["passport"].get("holidays", [])],
        "примечание": "Изменения — по сопоставимой базе: объекты с данными за все 7 дней в обоих периодах; "
                      "для долей — в процентных пунктах.",
    }


def _plan_facts(plan: dict | None):
    if not plan:
        return "плана в витрине нет"
    out = []
    for month in plan["months"]:
        rows = []
        for r in month["rows"]:
            v = lambda x: "нет данных" if x is None else fmt.value(x, r["unit"], r["decimals"])  # noqa: E731
            pct = lambda x: "нет данных" if x is None else fmt.value(x, "%", 1)  # noqa: E731
            rows.append({
                "показатель": r["title"], "факт с начала месяца": v(r["fact"]), "план месяца": v(r["planMonth"]),
                "выполнение плана месяца": pct(r["pctMonth"]), "выполнение плана на дату": pct(r["pctToDate"]),
                "отставание от равномерного графика": "нет данных" if r["gapPp"] is None else fmt.delta(r["gapPp"], share=True),
                "нужно в день до конца месяца": v(r["needPerDay"]), "текущий темп в день": v(r["pacePerDay"]),
                **({"статус": r["status"]} if r.get("status") else {}),
            })
        out.append({
            "месяц": month["label"], "месяц закрыт": month["closed"],
            "прошло дней": f"{month['daysPassed']} из {month['daysTotal']} ({fmt.value(month['daysPct'], '%', 1)})",
            "показатели": rows,
            "ОНПО": [{"ОНПО": o["name"], "выполнение плана по выручке НТУ": fmt.value(o.get("ntuPct"), "%", 1),
                      "отставание по НТУ": fmt.delta(o.get("ntuGap"), share=True),
                      "выполнение плана по топливу": fmt.value(o.get("fuelPct"), "%", 1),
                      **({"статус": o["status"]} if o.get("status") else {})} for o in month["onpo"]],
        })
    return out


def task(model: dict, attempt: int = 1) -> dict:
    """Готовый запрос к Ollama /api/chat — без имени модели: его ставит мак."""
    user = "Сводка справки:\n" + json.dumps(facts(model), ensure_ascii=False, indent=1) + "\n/no_think"
    return {
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "format": SCHEMA,
        "options": {"temperature": 0 if attempt <= 1 else 0.3, "num_predict": NUM_PREDICT},
        "promptVersion": PROMPT_VERSION,
        "attempt": attempt,
    }


def digest(model: dict) -> str:
    """Отпечаток цифр выпуска без времени сборки: тот же — значит, текст к нему подходит."""
    stable = dict(model, passport={k: v for k, v in model["passport"].items() if k not in VOLATILE})
    stable.pop("narrative", None)
    stable.pop("passportLines", None)
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:32]


# --- сверка ------------------------------------------------------------------

def _mask_dates(text: str) -> str:
    """Убрать из текста настоящие даты (день 1–31, месяц 1–12); «3.20» или «76.53» остаются числами."""
    def repl(m: re.Match) -> str:
        day, month = int(m.group(1)), int(m.group(2))
        return " " if 1 <= day <= 31 and 1 <= month <= 12 else m.group(0)
    text = (text or "").replace("\u202f", "\u00a0").replace("\u2009", "\u00a0")
    return DATE_RE.sub(repl, text)


def _near(value: float, candidate: float) -> bool:
    """Число текста — это число выпуска с точностью до показанных знаков или округлённое до тыс./млн/млрд."""
    if grounding._exact(value, candidate):  # noqa: SLF001 - общий допуск округления
        return True
    a, b = abs(value), abs(candidate)
    if a == 0 or b == 0:
        return a == b
    for scale in (1e3, 1e6, 1e9):
        scaled = b / scale
        if scaled >= 1 and (abs(a - scaled) / max(a, scaled) <= 0.006 or abs(a - round(scaled, 1)) < 1e-9):
            return True
    return False


def _dates(week: dict) -> list[str]:
    return [week.get("from", ""), week.get("to", "")]


class Evidence:
    """Числа и названия выпуска, на которые текст вправе опираться."""

    def __init__(self, model: dict):
        self.percent: set[float] = set()
        self.plain: set[float] = set()
        self.names: set[str] = set()
        p = model["passport"]
        for m in model["metrics"]:
            (self.percent if m["isShare"] else self.plain).update(
                abs(float(v)) for v in (m["value"], m["prev"], m["lastYear"]) if v is not None)
            self.percent.update(abs(float(v)) for v in (m["deltaPrev"], m["deltaYear"]) if v is not None)
            self.names.add(m["title"])
        for r in model["onpo"] + [model["onpoTotal"]]:
            self.plain.update(abs(float(r[k])) for k in ("stations", "fuel", "ntu") if r.get(k) is not None)
            self.percent.update(abs(float(r[k])) for k in ("fuelDeltaPrev", "fuelDeltaYear", "ntuDeltaPrev",
                                                            "ntuDeltaYear", "conversion") if r.get(k) is not None)
            if r.get("name"):
                self.names.add(r["name"])
        att = model["attention"]
        for r in att["drops"] + att["leaders"] + att["noSales"]:
            self.plain.update(abs(float(r[k])) for k in ("fuel", "fuelPrev", "idleDays") if r.get(k) is not None)
            if r.get("delta") is not None:
                self.percent.add(abs(float(r["delta"])))
            self._object(r)
        self.plain.update(float(v) for v in (att["dropsTotal"], len(att["noSales"]), len(att["drops"]),
                                             len(att["leaders"]), _zone(att)))
        for month in (model.get("plan") or {}).get("months", []):
            self.percent.add(float(month["daysPct"]))
            self.plain.update(float(v) for v in (month["daysPassed"], month["daysTotal"]))
            for r in month["rows"]:
                self.percent.update(abs(float(r[k])) for k in ("pctMonth", "pctToDate", "gapPp") if r.get(k) is not None)
                self.plain.update(abs(float(r[k])) for k in ("fact", "planMonth", "planToDate", "needPerDay",
                                                             "pacePerDay", "stations") if r.get(k) is not None)
            for o in month["onpo"]:
                self.percent.update(abs(float(v)) for k, v in o.items() if k.endswith(("Pct", "Gap")) and v is not None)
        th = p["thresholds"]
        self.percent.update(float(v) for v in (th["fuelDropPct"], th["completeSharePct"], th["minBaseShareOfMedian"] * 100,
                                               p["completeness"]["sharePct"], p["completeness"]["thresholdPct"]))
        self.plain.update(float(v) for v in (th["noSalesDays"], th["topDrops"], th["topLeaders"], p["stationsWeek"],
                                             p["comparablePrev"], p["excludedPrev"], p["comparableYear"],
                                             p["excludedYear"], p["completeness"]["base"],
                                             p["completeness"]["complete"], 7, 8))
        weeks = [model["week"], model["compare"]["prev"], model["compare"]["lastYear"]]
        for iso in _dates(weeks[0]) + _dates(weeks[1]) + _dates(weeks[2]) + [h["date"] for h in p.get("holidays", [])]:
            for part in (iso or "").split("-"):
                if part.isdigit():
                    self.plain.add(float(part))
        for w in weeks:
            number = (w.get("iso") or "").rsplit("W", 1)[-1]
            if number.isdigit():
                self.plain.add(float(number))
        self.names.update(r for r in (model.get("scopeLabel"), model.get("title"), "Вся сеть", "вся сеть") if r)
        self.lower_names = {n.lower() for n in self.names}

    def _object(self, row: dict) -> None:
        for key in ("label", "region", "onpo"):
            if row.get(key):
                self.names.add(row[key])
        for digits in re.findall(r"\d+", row.get("label") or ""):
            self.plain.add(float(digits))

    def unmatched(self, text: str) -> list[str]:
        """Числа текста, которых нет в выпуске (с учётом округления и «млн»)."""
        bad = []
        for value, is_percent in grounding.numbers_with_units(_mask_dates(text)):
            pool = self.percent if is_percent else self.plain
            if not any(_near(value, c) for c in pool):
                bad.append(fmt.number(value) if float(value).is_integer() else f"{value:g}".replace(".", ","))
        return bad

    def matched(self, text: str) -> int:
        return len(grounding.numbers_with_units(_mask_dates(text))) - len(self.unmatched(text))

    def unknown_names(self, text: str) -> list[str]:
        return [q for q in QUOTED_RE.findall(text or "") if q.strip().lower() not in self.lower_names]

    def mentions_object(self, text: str) -> bool:
        low = (text or "").lower()
        return any(n.lower() in low for n in self.names if len(n) >= 3)


def _clean(line) -> str:
    text = " ".join(str(line or "").replace("**", "").replace("__", "").split())
    text = BULLET_RE.sub("", text).lstrip("#").strip()
    return text


def _lines(value, limit: int, label: str) -> list[str]:
    if isinstance(value, str):
        value = [s for s in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«])", value) if s.strip()]
    if not isinstance(value, list):
        raise Rejected(f"{label}: не список")
    lines = [_clean(v) for v in value if _clean(v)]
    if len(lines) > limit:
        lines = lines[:limit]
    for line in lines:
        if len(line) > MAX_LINE:
            raise Rejected(f"{label}: слишком длинная фраза")
    return lines


def _check_fact(line: str, ev: Evidence, label: str) -> None:
    bad = ev.unmatched(line)
    if bad:
        raise Rejected(f"{label}: числа не из выпуска — {', '.join(bad[:3])}")
    unknown = ev.unknown_names(line)
    if unknown:
        raise Rejected(f"{label}: названия не из выпуска — {', '.join(unknown[:2])}")
    if HYPOTHESIS_RE.search(line):
        raise Rejected(f"{label}: предположение или причина вместо факта")
    if LATIN_ID_RE.search(line):
        raise Rejected(f"{label}: служебное имя латиницей")


class _RecContext:
    """Контекст для правил рекомендаций ИИ-16: опора — только цифры и объекты выпуска."""

    def __init__(self, ev: Evidence, source: dict):
        self.ev, self.source = ev, source

    def numbers(self, text: str):
        return [SimpleNamespace(kind="fact")] * self.ev.matched(text), self.ev.unmatched(text)

    def sources_of(self, proofs):
        return [self.source] if proofs else []

    def link(self, text: str):
        return [self.source] if self.ev.mentions_object(text) else []

    def formula(self, proof) -> str:  # noqa: ARG002 - у фактов выпуска формулы нет
        return ""


def _recommendations(raw, ev: Evidence, zone: int, week_label: str) -> tuple[list[dict], list[dict]]:
    items = raw if isinstance(raw, list) else []
    if not items:
        return [], []
    if zone == 0:
        return [], [{"action": _clean((recommend.normalize(i))["action"]), "reason": "в зоне внимания нет объектов"}
                    for i in items]
    source = {"id": "issue", "title": f"{SOURCE_TITLE}, неделя {week_label}"}
    review = recommend.review(items, _RecContext(ev, source), asked=True)
    kept, withheld = [], list(review.withheld)
    for rec in review.kept:
        fields = {k: _clean(rec[k]) for k in ("action", "basis", "effect", "limits")}
        bad = [n for k in ("action", "effect", "limits") for n in ev.unmatched(fields[k])]
        unknown = [q for k in fields for q in ev.unknown_names(fields[k])]
        if bad or unknown:
            withheld.append({"action": fields["action"], "reason": "числа или названия не из выпуска"})
            continue
        confidence = min(rec["confidence"], "средняя", key=recommend.CONFIDENCE.index)
        kept.append({**fields, "source": source["title"], "confidence": confidence,
                     **({"softened": True} if rec.get("softened") else {})})
    for extra in kept[MAX_RECOMMENDATIONS:]:
        withheld.append({"action": extra["action"], "reason": "больше двух рекомендаций"})
    return kept[:MAX_RECOMMENDATIONS], withheld


def parse(raw: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    if len(text) > MAX_RAW:
        raise Rejected("ответ модели слишком длинный")
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start:end + 1]) if 0 <= start < end else None
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise Rejected("ответ модели — не JSON")
    return data


def apply(model: dict, raw: str, *, model_name: str = "", duration_ms: int = 0) -> dict:
    """Разобрать и сверить ответ модели; вернуть текст выпуска (ИИ или шаблон с причиной)."""
    try:
        data = parse(raw)
        ev = Evidence(model)
        headline = _lines(data.get("headline"), MAX_HEADLINE, "Главное")
        if len(headline) < 2:
            raise Rejected("Главное: меньше двух предложений")
        attention = _lines(data.get("attention") or [], MAX_ATTENTION, "Внимание")
        for line in headline:
            _check_fact(line, ev, "Главное")
        for line in attention:
            _check_fact(line, ev, "Внимание")
    except Rejected as err:
        return template(model, reason=f"текст ИИ отклонён: {err}", model_name=model_name)
    recs, withheld = _recommendations(data.get("recommendations"), ev, _zone(model["attention"]), model["week"]["label"])
    return {
        "source": AI, "headline": headline, "attention": attention, "recommendations": recs,
        "withheld": withheld[:6], "model": model_name[:100], "promptVersion": PROMPT_VERSION,
        "durationMs": int(duration_ms or 0), "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "claims": {"headline": ["fact"] * len(headline), "attention": ["fact"] * len(attention),
                   "recommendations": ["recommendation"] * len(recs)},
        "reason": "",
    }


def recheck(model: dict) -> dict:
    """Сервер не верит присланному тексту: сверяет его с цифрами выпуска ещё раз теми же правилами."""
    text = of(model)
    if text.get("source") != AI:
        return template(model, reason=str(text.get("reason") or ""), model_name=str(text.get("model") or ""))
    raw = json.dumps({
        "headline": text.get("headline") or [], "attention": text.get("attention") or [],
        "recommendations": [{k: r.get(k, "") for k in ("action", "basis", "effect", "limits")}
                            for r in text.get("recommendations") or [] if isinstance(r, dict)],
    }, ensure_ascii=False)
    checked = apply(model, raw, model_name=str(text.get("model") or ""), duration_ms=int(text.get("durationMs") or 0))
    if checked["source"] == AI:
        checked["generatedAt"] = text.get("generatedAt") or checked["generatedAt"]
        checked["promptVersion"] = text.get("promptVersion") or PROMPT_VERSION
    else:
        checked["reason"] = checked["reason"].replace("текст ИИ отклонён", "текст ИИ не прошёл проверку на сервере")
    return checked


def template(model: dict, reason: str = "", model_name: str = "") -> dict:
    return {"source": TEMPLATE, "headline": list(model.get("headline") or []), "attention": [],
            "recommendations": [], "withheld": [], "model": model_name[:100], "promptVersion": PROMPT_VERSION,
            "reason": reason[:300]}


# --- показ: экран, PDF и Excel берут текст отсюда -----------------------------

def of(model: dict) -> dict:
    return model.get("narrative") or {}


def headline(model: dict) -> list[str]:
    n = of(model)
    return n["headline"] if n.get("source") == AI and n.get("headline") else list(model.get("headline") or [])


def attention(model: dict) -> list[str]:
    n = of(model)
    return n.get("attention") or [] if n.get("source") == AI else []


def recommendations(model: dict) -> list[dict]:
    n = of(model)
    return n.get("recommendations") or [] if n.get("source") == AI else []


def passport_line(model: dict) -> str | None:
    n = of(model)
    if not n:
        return None
    if n.get("source") == AI:
        return (f"Текст «Главное» и «На что обратить внимание» написал ИИ (модель {n.get('model') or '—'}, "
                f"подсказка {n.get('promptVersion')}) по цифрам этой справки; каждое число сверено с выпуском. "
                f"{RECOMMENDATION_NOTE if n.get('recommendations') else ''}".strip())
    reason = n.get("reason") or ""
    return f"Текст «Главное» — по шаблону{': ' + reason if reason else ''}."
