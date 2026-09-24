"""Рекомендации (ИИ-16): шаблон из шести полей, стоп-лист, закрытые темы.

Решение Р-9: у рекомендации шесть обязательных полей — действие, основание,
ожидаемый эффект, ограничения применимости, источник, уверенность; стоп-лист
утверждает владелец. Первые четыре поля пишет модель, источник и
уверенность ставит код — по тому, на что на самом деле опирается основание.
Проверку делает код, а не модель:

- неполная рекомендация не показывается;
- основание должно опираться на данные ответа (число или объект из результата)
  или на методику — иначе «Недостаточно данных для рекомендации»;
- категоричные слова из стоп-листа смягчаются («необходимо» → «стоит»);
- рекомендации об оценке и санкциях к работникам (К13) и об акциях и скидках
  (решение владельца 23.09.2026) снимаются целиком.

Стоп-лист и закрытые темы — в `backend/ai/recommendation_rules.json`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parents[1] / "recommendation_rules.json"
MODEL_FIELDS = ("action", "basis", "effect", "limits")
CONFIDENCE = ("низкая", "средняя", "высокая")
MAX_RECOMMENDATIONS = 3
# Решение владельца 23.09.2026: рекомендации — не в каждом ответе. Если о них
# не просили, модель может дать одну, когда без неё ответ неполон, — и только
# с уверенностью не ниже средней.
UNSOLICITED_MAX = 1
REQUEST_RE = re.compile(
    r"рекоменд|посоветуй|совет|предлож\w* (меры|действия)|предложи\b|как\w* меры|что предпринять|план действий|"
    r"что (мне |нам |им )?(можно |нужно |стоит |следует )?(делать|сделать)|"
    r"как (можно )?(улучшить|исправить|поднять|повысить|увеличить|нарастить|снизить|сократить|закрыть|выполнить|догнать|"
    r"вернуть|добиться|подтянуть|компенсировать)",
    re.IGNORECASE,
)
MAX_FIELD = 300
INSUFFICIENT = "Недостаточно данных для рекомендации."
METHOD_RE = re.compile(r"методик|регламент|стандарт компании|база знаний|базы знаний|нормативн", re.IGNORECASE)
SYNONYMS = {
    "action": ("action", "text", "what", "действие"),
    "basis": ("basis", "reason", "grounds", "основание"),
    "effect": ("effect", "expected_effect", "эффект", "ожидаемый эффект"),
    "limits": ("limits", "limitations", "constraints", "when_not", "ограничения"),
    "source": ("source", "источник"),
    "confidence": ("confidence", "уверенность"),
}


@dataclass
class Review:
    kept: list[dict] = field(default_factory=list)
    withheld: list[dict] = field(default_factory=list)


@lru_cache(maxsize=1)
def rules() -> dict:
    try:
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    soften = [(str(a), str(b)) for a, b in data.get("soften", []) if a]
    forbidden = []
    for item in data.get("forbidden", []):
        patterns = [re.compile(p, re.IGNORECASE) for p in item.get("patterns", []) if p]
        forbidden.append((item.get("topic", ""), item.get("reason", ""), patterns))
    return {"soften": soften, "forbidden": forbidden, "version": data.get("version", "")}


def normalize(item) -> dict:
    """Рекомендация от модели → словарь полей (строка — только действие)."""
    if isinstance(item, str):
        item = {"action": item}
    if not isinstance(item, dict):
        item = {}
    out = {}
    for name, keys in SYNONYMS.items():
        value = next((item[k] for k in keys if item.get(k)), "")
        if isinstance(value, list):
            value = "; ".join(str(v) for v in value if v)
        out[name] = " ".join(str(value).split())[:MAX_FIELD]
    return out


def soften(text: str) -> tuple[str, bool]:
    """Заменить категоричные слова мягкими; вернуть текст и признак замены."""
    result = text
    for word, replacement in rules()["soften"]:
        pattern = re.compile(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", re.IGNORECASE)
        result = pattern.sub(replacement, result)
    if result == text:
        return text, False
    result = re.sub(r"\s{2,}", " ", result).strip()
    result = re.sub(r"\s+([,.;:])", r"\1", result).lstrip(",;: ")
    if text[:1].isupper() and result[:1].islower():
        result = result[:1].upper() + result[1:]
    return result, True


def forbidden_topic(text: str) -> tuple[str, str] | None:
    for topic, reason, patterns in rules()["forbidden"]:
        if any(p.search(text) for p in patterns):
            return topic, reason
    return None


def _confidence(value: str) -> str | None:
    value = (value or "").strip().lower()
    for level in CONFIDENCE:
        if value.startswith(level[:4]):
            return level
    return {"high": "высокая", "medium": "средняя", "low": "низкая"}.get(value)


def requested(*texts: str) -> bool:
    """Просит ли пользователь совета: «что сделать», «как поднять», «рекомендации»."""
    return any(REQUEST_RE.search(text or "") for text in texts)


def review(items: list, ctx, asked: bool = True) -> Review:
    """Проверить рекомендации модели; `ctx` — контекст разметки из claims.

    `asked` — просил ли пользователь рекомендаций; если нет, остаётся не больше
    одной и только с уверенностью не ниже средней.
    """
    from .claims import HYPOTHESIS_RE  # общий словарь предположительных формулировок

    out = Review()
    for raw in items or []:
        rec = normalize(raw)
        if not rec["action"]:
            continue
        missing = [name for name in MODEL_FIELDS if len(rec[name]) < 3]
        if missing:
            out.withheld.append({"action": rec["action"], "reason": "не заполнены поля: " + ", ".join(missing)})
            continue
        joined = " ".join(rec[name] for name in MODEL_FIELDS)
        closed = forbidden_topic(joined)
        if closed:
            out.withheld.append({"action": rec["action"], "reason": closed[1], "topic": closed[0]})
            continue
        proofs, unmatched = ctx.numbers(rec["basis"])
        sources = ctx.sources_of(proofs) or ctx.link(rec["basis"])
        method = bool(METHOD_RE.search(rec["source"] + " " + rec["basis"]))
        if unmatched or (not sources and not method):
            out.withheld.append({"action": rec["action"], "reason": "основание не опирается на данные ответа или методику"})
            continue
        action, changed_action = soften(rec["action"])
        effect, changed_effect = soften(rec["effect"])
        effect_proofs, _ = ctx.numbers(effect)

        parts = []
        if sources:
            parts.append("анализ витрины: " + "; ".join(f"«{s['title']}» ({s['id']})" for s in sources[:2]))
        if method:
            named = rec["source"] if METHOD_RE.search(rec["source"]) else ""
            parts.append(named or "методика")
        source = "; ".join(parts)
        source = source[:1].upper() + source[1:]

        cap = "высокая" if sources else "средняя"
        if HYPOTHESIS_RE.search(rec["basis"]):
            cap = "низкая"
        wanted = _confidence(rec["confidence"]) or ("средняя" if sources else "низкая")
        confidence = min(wanted, cap, key=CONFIDENCE.index)

        item = {
            "action": action, "basis": rec["basis"], "effect": effect, "limits": rec["limits"],
            "source": source, "confidence": confidence, "sources": sources[:3],
        }
        formulas = [ctx.formula(m) for m in effect_proofs if m.kind != "fact"]
        if formulas:
            item["effectFormula"] = list(dict.fromkeys(formulas))[:2]
        if changed_action or changed_effect:
            item["softened"] = True
        out.kept.append(item)
    if not asked:
        confident = [r for r in out.kept if r["confidence"] != "низкая"]
        for rec in out.kept:
            if rec not in confident[:UNSOLICITED_MAX]:
                out.withheld.append({"action": rec["action"], "reason": "рекомендаций не просили"})
        out.kept = confident[:UNSOLICITED_MAX]
    if len(out.kept) > MAX_RECOMMENDATIONS:
        for extra in out.kept[MAX_RECOMMENDATIONS:]:
            out.withheld.append({"action": extra["action"], "reason": "больше трёх рекомендаций"})
        out.kept = out.kept[:MAX_RECOMMENDATIONS]
    return out
