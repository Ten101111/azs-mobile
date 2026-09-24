"""СП-03, СП-06. Сервер справок: приём готовых выпусков, версии, файлы, запросы на перевыпуск.

Решение владельца 24.09.2026: справка строится только по витрине ОХД, а она видна
только с компьютера владельца под VPN. Поэтому сервер справку не собирает: её
собирает `backend/reports/publish.py` на компьютере владельца и присылает готовую
модель выпуска (цифры и текст). Сервер проверяет модель, ещё раз сверяет текст ИИ
с цифрами, рисует PDF и Excel, хранит версии и показывает выпуск.

Идемпотентность: опубликованная неделя повторно не принимается. Новая версия —
только по запросу на перевыпуск («Сформировать заново» в интерфейсе): запрос
лежит на сервере, сборщик забирает его при следующем запуске и присылает выпуск
с номером запроса; прежняя версия остаётся в архиве со статусом «заменён».
Исключение — выпуск, построенный не по витрине ОХД (до решения 24.09.2026 справка
собиралась по базе KPI): его заменяет первый же выпуск по витрине, без запроса.
Выпуск закрывает и открытые запросы той же недели, поданные до его сборки, —
иначе сборщик собрал бы неделю ещё раз.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path

from . import narrative, periods, registry, render_pdf, render_xlsx, store, weekly

MODEL_KEYS = ("type", "title", "level", "scopeKey", "scopeLabel", "week", "compare", "headline", "metrics",
              "dynamics", "onpo", "onpoTotal", "fuelUnit", "attention", "pending", "passport")
SOURCE_PREFIX = "Витрина ОХД"
LEGACY_REASON = "замена справки, собранной не по витрине ОХД"


class InvalidModel(ValueError):
    """Присланная модель выпуска не годится для публикации."""


def today_msk() -> date:
    return datetime.now(weekly.MSK).date()


def file_name(model: dict, version: int, ext: str) -> str:
    """Русское имя файла для скачивания: «Справка_сеть_2026-W37_v1.pdf»."""
    return f"Справка_сеть_{model['week']['iso']}_v{version}.{ext}"


def check_model(model) -> periods.Week:
    """Модель выпуска с компьютера владельца: полная, нужного типа и построена по витрине ОХД."""
    if not isinstance(model, dict):
        raise InvalidModel("модель выпуска — не объект")
    missing = [key for key in MODEL_KEYS if key not in model]
    if missing:
        raise InvalidModel("в модели нет разделов: " + ", ".join(missing))
    if model["type"] not in registry.REPORT_TYPES:
        raise InvalidModel(f"неизвестный тип справки «{model['type']}»")
    try:
        week = periods.parse_iso(model["week"]["iso"])
    except (KeyError, TypeError, ValueError) as err:
        raise InvalidModel("неделя выпуска задана неверно") from err
    if (model["week"].get("from"), model["week"].get("to")) != (week.start.isoformat(), week.end.isoformat()):
        raise InvalidModel("даты недели не совпадают с её номером")
    if not from_mart(model):
        raise InvalidModel("справка принимается только построенной по витрине ОХД")
    if not isinstance(model["metrics"], list) or not all(isinstance(m, dict) and "code" in m for m in model["metrics"]):
        raise InvalidModel("цифры недели заданы неверно")
    return week


def from_mart(model: dict) -> bool:
    """Выпуск построен по витрине ОХД: в паспорте — таблица фактов витрины, а не просто слово «ОХД»."""
    source = str((model.get("passport") or {}).get("source") or "")
    return source.startswith(SOURCE_PREFIX) and weekly.MART_FACTS in source


def _generated_at(model: dict) -> float:
    try:
        return datetime.fromisoformat(model["passport"]["generatedAt"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return time.time()


def accept(model: dict, *, request_id: int | None = None, files_dir: Path | None = None,
           db_path: Path | None = None) -> dict:
    """Принять готовый выпуск: exists — неделя уже опубликована; published — новая версия."""
    week = check_model(model)
    rt = registry.get(model["type"])
    started = time.time()
    digest = narrative.digest(model)
    with store.connect(db_path) as conn:
        request = None
        if request_id:
            request = conn.execute("SELECT * FROM report_requests WHERE id = ? AND done_at IS NULL AND week = ?",
                                   (request_id, week.iso)).fetchone()
        existing = store.published(conn, rt.code, rt.scope_key, week.iso)
        legacy = existing is not None and not from_mart(json.loads(existing["model_json"] or "{}"))
        if existing is not None and request is None and not legacy:
            store.log_run(conn, rt.code, week.iso, started, "import", "exists")
            return {"outcome": "exists", "issue": store.summary(existing), "week": week.as_dict(),
                    "same": existing["digest"] == digest}
    text = narrative.recheck(model)
    reason = request["reason"] if request else (LEGACY_REASON if legacy else "")
    result = _publish(rt, week, model, text, trigger="request" if request else "import", reason=reason,
                      created_by=request["created_by"] if request else "",
                      started=started, digest=digest, files_dir=files_dir, db_path=db_path)
    with store.connect(db_path) as conn:
        covered = conn.execute(
            "SELECT id FROM report_requests WHERE type = ? AND scope_key = ? AND week = ? AND done_at IS NULL "
            "AND (id = ? OR created_at <= ?)",
            (rt.code, rt.scope_key, week.iso, request_id or 0, _generated_at(model))).fetchall()
        for row in covered:
            store.close_request(conn, int(row["id"]), "published", result["issue"]["id"])
    return result


def _publish(rt, week: periods.Week, model: dict, text: dict, *, trigger: str, reason: str, created_by: str,
             started: float, digest: str, files_dir: Path | None, db_path: Path | None) -> dict:
    folder = Path(files_dir or store.FILES_DIR) / rt.code
    folder.mkdir(parents=True, exist_ok=True)
    with store.connect(db_path) as conn:
        version = store.next_version(conn, rt.code, rt.scope_key, week.iso)
    model = dict(model, version=version, reason=reason, narrative=text)
    model["passportLines"] = render_pdf.passport_lines(model)
    pdf = folder / f"{week.iso}_v{version}.pdf"
    xlsx = folder / f"{week.iso}_v{version}.xlsx"
    render_pdf.render(model, pdf)
    render_xlsx.render(model, xlsx)
    with store.connect(db_path) as conn:
        issue_id = store.publish(conn, model, pdf, xlsx, version=version, reason=reason, created_by=created_by,
                                 digest=digest)
        store.clear_not_formed(conn, rt.code, rt.scope_key, week.iso)
        store.log_run(conn, rt.code, week.iso, started, trigger, "published",
                      completeness=model["passport"].get("completeness", {}).get("sharePct"),
                      detail="текст ИИ" if text.get("source") == narrative.AI
                      else f"текст по шаблону {text.get('reason', '')}".strip())
        issue = store.summary(store.get(conn, issue_id))
    return {"outcome": "published", "issue": issue, "week": week.as_dict(), "text": text.get("source")}


def not_formed(week_iso: str, reason: str, *, request_id: int | None = None, type_code: str | None = None,
               db_path: Path | None = None) -> dict:
    """Сборщик не смог собрать неделю: причина видна на экране, прошлая справка остаётся."""
    rt = registry.get(type_code)
    week = periods.parse_iso(week_iso)
    reason = " ".join(str(reason or "").split())[:500] or "причина не указана"
    with store.connect(db_path) as conn:
        changed = store.not_formed(conn, rt.code, rt.level, rt.scope_key, week.as_dict(), reason)
        store.log_run(conn, rt.code, week.iso, time.time(), "import", "not_formed", detail=reason)
        if request_id:
            store.close_request(conn, request_id, f"не сформирована: {reason}"[:300])
    return {"outcome": "not_formed", "reason": reason, "week": week.as_dict(), "changed": changed}


def status(week_iso: str | None = None, type_code: str | None = None, db_path: Path | None = None) -> dict:
    """Что нужно сборщику: опубликована ли неделя и нет ли запроса на перевыпуск."""
    rt = registry.get(type_code)
    week = periods.parse_iso(week_iso) if week_iso else periods.last_complete_week(today_msk())
    with store.connect(db_path) as conn:
        row = store.published(conn, rt.code, rt.scope_key, week.iso)
        request = store.request_summary(store.open_request(conn, rt.code, rt.scope_key))
    return {"type": rt.code, "week": week.as_dict(), "published": row is not None,
            "version": int(row["version"]) if row is not None else 0, "digest": row["digest"] if row is not None else "",
            "request": request}


def request_reissue(week_iso: str, reason: str, created_by: str = "", type_code: str | None = None,
                    db_path: Path | None = None) -> dict:
    rt = registry.get(type_code)
    week = periods.parse_iso(week_iso)
    with store.connect(db_path) as conn:
        request_id = store.add_request(conn, rt.code, rt.scope_key, week.iso, reason, created_by)
        request = store.request_summary(conn.execute("SELECT * FROM report_requests WHERE id = ?", (request_id,)).fetchone())
    return {"outcome": "requested", "request": request, "week": week.as_dict()}


def already_notified(week_iso: str, type_code: str, db_path: Path | None = None) -> bool:
    with store.connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM report_runs WHERE type = ? AND week = ? AND outcome = 'notified' LIMIT 1",
                           (type_code, week_iso)).fetchone()
    return row is not None


def mark_notified(week_iso: str, type_code: str, detail: str, db_path: Path | None = None) -> None:
    with store.connect(db_path) as conn:
        store.log_run(conn, type_code, week_iso, time.time(), "import", "notified", detail=detail)
