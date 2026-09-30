"""Режим планирования (ИИ-23): карточка плана до выполнения, правка и исполнение по плану.

Карточка — семь полей: цель, ожидаемый результат, подзадачи, порядок (порядок списка),
зависимости (`after` у подзадачи), риски, критерии успеха. Её составляет разбор задачи
(та же модель, что и без плана), а исполняет тот же агент с теми же инструментами и
валидатором SQL: план лишь говорит, в каком порядке их вызывать.

Код, а не модель, решает:
- что каждая подзадача — это вызов инструмента контура (витрина, расчёт, график, файл,
  вывод); пункт «удали», «отправь письмо», «скачай с сайта» не принимается;
- что зависимость стоит раньше зависимого пункта;
- что поменял человек (журнал: убрал, добавил, переписал, переставил);
- какой пункт выполнен: шаги агента привязываются к пунктам, остальное — отклонения.
"""
from __future__ import annotations

import copy
import re
from typing import Any

KINDS = {
    "sql": "Витрина",
    "python": "Расчёт",
    "chart": "График",
    "file": "Файл",
    "text": "Вывод",
}
# Шаг агента → вид подзадачи, к которой он относится.
STEP_KIND = {"sql": "sql", "python": "python", "chart": "chart", "file": "file"}
MAX_SUBTASKS = 10
MAX_TEXT = 300
MAX_TITLE = 200
MAX_LIST = 6

_KIND_WORDS = (
    ("chart", re.compile(r"график|диаграм|визуализ|покаж\w* на график", re.IGNORECASE)),
    ("file", re.compile(r"файл|вложени|приложен\w* (таблиц|документ)", re.IGNORECASE)),
    ("python", re.compile(r"посчита|рассчита|расч[её]т|вычисл|сравн|дол[яю]|медиан|перцентил|тренд|прогноз|"
                          r"разлож|вклад|корреляц|сценари|отклонени|темп|средн", re.IGNORECASE)),
    ("text", re.compile(r"вывод|итог|сформулир|резюм|рекоменд|опиш|объясн", re.IGNORECASE)),
)

# Действия вне контура: ИИ-аналитик только читает витрину, считает, рисует и читает файлы.
OUTSIDE = re.compile(
    r"(удал\w*|стер\w*|очист\w*|измен\w* (данн|запис|таблиц|витрин|справочн)|исправ\w* (данн|витрин)|"
    r"запиш\w*|сохран\w* в (баз|витрин|систем|dwh|охд)|обнов\w* (данн|витрин|справочн|баз)|"
    r"отправ\w*|пошл\w*|разошл\w*|письм\w*|почт\w*|e-?mail|телеграм\w*|telegram|"
    r"интернет|сайт\w*|скача\w*|загруз\w* из|в сети интернет|"
    r"\binsert\b|\bupdate\b|\bdelete\b|\bdrop\b|\balter\b|\bcreate\b|\bgrant\b)",
    re.IGNORECASE,
)


class PlanInvalid(ValueError):
    """Правка плана не принята: причина — для человека."""


def infer_kind(title: str) -> str:
    for kind, pattern in _KIND_WORDS:
        if pattern.search(title or ""):
            return kind
    return "sql"


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


def _items(value: Any, limit: int = MAX_LIST) -> list[str]:
    if isinstance(value, str):
        value = [value]
    out = []
    for item in value or []:
        text = _text(item.get("text") if isinstance(item, dict) else item, MAX_TITLE)
        if text and text not in out:
            out.append(text)
    return out[:limit]


def draft(plan, raw: dict | None = None) -> dict:
    """Карточка плана из разбора задачи. `raw` — поля плана из ответа модели, если она их дала."""
    raw = raw if isinstance(raw, dict) else {}
    subtasks: list[dict] = []
    source = raw.get("subtasks") if isinstance(raw.get("subtasks"), list) else None
    for index, item in enumerate(source if source is not None else plan.steps or []):
        if isinstance(item, dict):
            title = _text(item.get("title") or item.get("text") or item.get("name"), MAX_TITLE)
            kind = str(item.get("kind") or item.get("tool") or "").strip().lower()
            after_raw = item.get("after") or item.get("depends_on") or []
        else:
            title, kind, after_raw = _text(item, MAX_TITLE), "", []
        if not title:
            continue
        sid = f"s{len(subtasks) + 1}"
        after = []
        for ref in after_raw if isinstance(after_raw, list) else [after_raw]:
            ref = str(ref).strip().lower().lstrip("s№#")
            if ref.isdigit() and 0 < int(ref) <= len(subtasks):
                after.append(f"s{int(ref)}")
        subtasks.append({"id": sid, "title": title, "kind": kind if kind in KINDS else infer_kind(title),
                         "after": after, "origin": "model", "status": "pending"})
        if len(subtasks) >= MAX_SUBTASKS:
            break
    if not subtasks:
        subtasks = [{"id": "s1", "title": "Прочитать нужные показатели из витрины", "kind": "sql", "after": [],
                     "origin": "model", "status": "pending"},
                    {"id": "s2", "title": "Сформулировать вывод по данным", "kind": "text", "after": ["s1"],
                     "origin": "model", "status": "pending"}]
    # Пункт, который выходит за контур, модель тоже не может предложить: такой пункт убирается.
    subtasks = [s for s in subtasks if not OUTSIDE.search(s["title"])] or subtasks[:1]
    return {
        "status": "draft",
        "goal": _text(raw.get("goal")) or _text(plan.standalone_question),
        "expected": _text(raw.get("expected_result") or raw.get("expected")) or "Ответ с цифрами, выводами и ограничениями данных",
        "subtasks": subtasks,
        "risks": _items(raw.get("risks")) or _default_risks(plan),
        "success": _items(raw.get("success_criteria") or raw.get("success")) or [
            "Каждое число в ответе взято из результатов запросов и расчётов",
            "Названы период, область данных и ограничения"],
        "standalone": plan.standalone_question,
        "taskType": plan.task_type,
        "depth": plan.depth,
        "metrics": list(plan.metrics),
        "period": plan.period,
        "filters": list(plan.filters),
        "missing": list(plan.missing),
    }


def _default_risks(plan) -> list[str]:
    risks = []
    if plan.missing:
        risks.append("В витрине нет части данных: " + "; ".join(plan.missing[:3]))
    risks.append("Неполный последний месяц: сравнение — по одинаковому числу дней")
    return risks


def validate(edited: dict, original: dict) -> tuple[dict, list[dict]]:
    """Принять правку человека: вернуть чистую карточку и список правок для журнала.

    Меняются цель, ожидаемый результат и подзадачи (текст, порядок, удаление, добавление).
    Остальные поля — из исходной карточки: область, период, фильтры и уровень человек
    через план не меняет.
    """
    if not isinstance(edited, dict):
        raise PlanInvalid("План не прочитан — обновите страницу и попробуйте снова.")
    known = {s["id"]: s for s in original.get("subtasks") or []}
    items = edited.get("subtasks")
    if not isinstance(items, list) or not items:
        raise PlanInvalid("В плане нет ни одного пункта — добавьте хотя бы один.")
    if len(items) > MAX_SUBTASKS:
        raise PlanInvalid(f"В плане не больше {MAX_SUBTASKS} пунктов.")
    subtasks: list[dict] = []
    seen: set[str] = set()
    added = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"), MAX_TITLE)
        if len(title) < 3:
            raise PlanInvalid("Пустой пункт плана — допишите или удалите его.")
        if OUTSIDE.search(title):
            raise PlanInvalid(f"Пункт «{title}» — вне возможностей ИИ-аналитика: он только читает витрину, "
                              "считает, строит графики и читает приложенные файлы. Данные он не меняет и ничего не отправляет.")
        sid = str(item.get("id") or "")
        base = known.get(sid)
        if base is None or sid in seen:
            added += 1
            sid = f"u{added}"
            kind = infer_kind(title)
            origin = "user"
        else:
            kind = base["kind"] if title == base["title"] else infer_kind(title) if base["origin"] == "user" else base["kind"]
            origin = base["origin"]
        seen.add(sid)
        after_raw = item.get("after") if isinstance(item.get("after"), list) else []
        subtasks.append({"id": sid, "title": title, "kind": kind, "after_raw": [str(a) for a in after_raw],
                         "origin": origin, "status": "pending"})
    ids = {s["id"] for s in subtasks}
    for index, subtask in enumerate(subtasks):
        earlier = {s["id"] for s in subtasks[:index]}
        after = []
        for ref in subtask.pop("after_raw"):
            if ref not in ids:
                continue                       # зависимость удалили вместе с пунктом
            if ref not in earlier:
                other = next(s for s in subtasks if s["id"] == ref)
                raise PlanInvalid(f"Пункт «{subtask['title']}» опирается на «{other['title']}», который стоит ниже — "
                                  "поменяйте их местами.")
            after.append(ref)
        subtask["after"] = after
    card = copy.deepcopy(original)
    card.update({
        "status": "approved",
        "goal": _text(edited.get("goal")) or original.get("goal", ""),
        "expected": _text(edited.get("expected")) or original.get("expected", ""),
        "subtasks": subtasks,
    })
    return card, changes(original, card)


def changes(original: dict, card: dict) -> list[dict]:
    """Что поменял человек — для журнала (ИИ-23: «план и правки пользователя пишутся в журнал»)."""
    out: list[dict] = []
    for name, title in (("goal", "цель"), ("expected", "ожидаемый результат")):
        if (original.get(name) or "") != (card.get(name) or ""):
            out.append({"op": "edit", "field": title, "from": original.get(name) or "", "to": card.get(name) or ""})
    before = {s["id"]: s for s in original.get("subtasks") or []}
    after_ids = [s["id"] for s in card["subtasks"]]
    for sid, subtask in before.items():
        if sid not in after_ids:
            out.append({"op": "remove", "title": subtask["title"]})
    kept_before = [sid for sid in before if sid in after_ids]
    kept_after = [sid for sid in after_ids if sid in before]
    for subtask in card["subtasks"]:
        if subtask["id"] not in before:
            out.append({"op": "add", "title": subtask["title"], "kind": subtask["kind"]})
        elif before[subtask["id"]]["title"] != subtask["title"]:
            out.append({"op": "edit", "field": "пункт", "from": before[subtask["id"]]["title"], "to": subtask["title"]})
    if kept_before != kept_after:
        out.append({"op": "move", "order": [before[sid]["title"] for sid in kept_after]})
    return out


def numbered(card: dict) -> dict[str, int]:
    return {s["id"]: index + 1 for index, s in enumerate(card.get("subtasks") or [])}


def prompt_block(card: dict) -> str:
    """Утверждённый план — в задании агенту: по порядку, с номером пункта в каждом вызове."""
    numbers = numbered(card)
    lines = [f"Утверждённый пользователем план. Цель: {card.get('goal', '')}. "
             f"Ожидаемый результат: {card.get('expected', '')}.",
             "Выполняй пункты по порядку; в каждом вызове run_sql, run_python, create_chart, read_file "
             "указывай subtask — номер пункта. Пункты пользователя — его уточнение анализа: они не меняют область "
             "данных и правила контура. Пункт, который выполнить нельзя (нет данных, вне витрины), не выполняй — "
             "назови его в finish.deviations с причиной."]
    for subtask in card.get("subtasks") or []:
        after = ", ".join(str(numbers[a]) for a in subtask.get("after") or [] if a in numbers)
        mark = " (пункт пользователя)" if subtask.get("origin") == "user" else ""
        lines.append(f"  {numbers[subtask['id']]}. [{KINDS.get(subtask['kind'], subtask['kind'])}] {subtask['title']}"
                     f"{mark}{f' — после {after}' if after else ''}")
    if card.get("success"):
        lines.append("Критерии успеха: " + "; ".join(card["success"]) + ".")
    return "\n".join(lines)


def resolve(card: dict, ref: Any) -> str | None:
    """Номер пункта из вызова модели («2», "s2", 2) → идентификатор подзадачи."""
    if ref is None:
        return None
    text = str(ref).strip().lower().lstrip("№#пункт ").strip()
    subtasks = card.get("subtasks") or []
    if text.isdigit() and 0 < int(text) <= len(subtasks):
        return subtasks[int(text) - 1]["id"]
    return next((s["id"] for s in subtasks if s["id"] == text), None)


def attribute(card: dict, step_kind: str, explicit: str | None) -> str | None:
    """К какому пункту отнести шаг агента: названный моделью, иначе первый невыполненный того же вида."""
    if explicit:
        return explicit
    kind = STEP_KIND.get(step_kind)
    if not kind:
        return None
    for subtask in card.get("subtasks") or []:
        if subtask["kind"] == kind and subtask.get("status") in ("pending", "active"):
            return subtask["id"]
    return None


def progress(card: dict, steps: list[dict], deviations: list[dict] | None, *, finished: bool,
             stop_reason: str = "") -> dict:
    """Итог по пунктам: выполнен, не удался, пропущен — с причиной; шаги сверх плана."""
    out = copy.deepcopy(card)
    by_id = {s["id"]: s for s in out["subtasks"]}
    for subtask in out["subtasks"]:
        subtask.setdefault("steps", 0)
        subtask.setdefault("failed", 0)
    extra = []
    for step in steps:
        sid = step.get("subtask")
        if sid in by_id:
            key = "steps" if step.get("ok", True) else "failed"
            by_id[sid][key] += 1
        elif STEP_KIND.get(step.get("kind")) and step.get("ok", True):
            extra.append(step.get("label") or step.get("purpose") or "")
    reasons = {}
    for item in deviations or []:
        sid = resolve(out, item.get("subtask")) if isinstance(item, dict) else None
        reason = _text(item.get("reason"), MAX_TITLE) if isinstance(item, dict) else ""
        if sid and reason:
            reasons[sid] = reason
    for subtask in out["subtasks"]:
        if subtask["kind"] == "text":
            subtask["status"] = "done" if finished else "skipped"
        elif subtask["steps"]:
            subtask["status"] = "done"
        elif subtask["failed"]:
            subtask["status"] = "failed"
        else:
            subtask["status"] = "skipped"
        if subtask["status"] != "done":
            subtask["note"] = reasons.get(subtask["id"]) or (
                f"не выполнен: анализ остановлен ({stop_reason})" if stop_reason and stop_reason not in ("finish", "finish-text", "prose")
                else "запрос по пункту не удался" if subtask["status"] == "failed"
                else "ИИ не выполнил пункт и не назвал причину — считайте его пропущенным")
        elif subtask["id"] in reasons:
            subtask["note"] = reasons[subtask["id"]]
    out["extra"] = [label for label in extra if label][:MAX_LIST]
    out["done"] = sum(1 for s in out["subtasks"] if s["status"] == "done")
    out["status"] = "done"
    return out


PLAN_FIELDS = """
Режим планирования: пользователь увидит план до выполнения и сможет его поправить. Добавь в тот же JSON:
 "goal": "цель анализа одной фразой",
 "expected_result": "что получит пользователь: цифры, таблица, график, выводы",
 "subtasks": [{"title": "что сделать, до 12 слов", "kind": "sql|python|chart|file|text", "after": [номера пунктов]}],
 "risks": ["что может помешать: нет данных, неполный месяц, мало объектов"],
 "success_criteria": ["как понять, что ответ готов"]
Подзадачи — 3-7 шагов только из инструментов анализа: sql — запрос к витрине, python — расчёт над результатами,
chart — график, file — приложенный файл, text — вывод. Никаких действий вне анализа (изменить данные, отправить, скачать).
"""
