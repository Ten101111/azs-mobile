"""Сборка справки по витрине ОХД на компьютере владельца, копия у владельца и отправка в приложение.

Решение владельца 24.09.2026: справка строится только по витрине ОХД
(dm.data_for_ai_analytic_part_1 и part_2). Витрина видна только отсюда, под VPN,
поэтому собирает её этот модуль, а приложение лишь показывает готовый выпуск.

Куда уходит выпуск — REPORTS_IMPORT_URL: приложение, запущенное на этом маке
(http://127.0.0.1:8000, npm start), или сайт. Не задан — адрес сайта из KPI_IMPORT_URL.

Решение владельца 24.09.2026 (копия и очередь): справка не зависит от того, доступно
ли приложение. Каждый собранный выпуск сначала сохраняется здесь в PDF и Excel, затем
встаёт в очередь и уходит в приложение; не ушёл — ждёт следующего запуска и из ОХД
заново не собирается. Так развязаны VPN и сайт: собрали под VPN — выпуск уйдёт,
когда сайт станет доступен.

    ./scripts/python.sh -m backend.reports.publish               # как по расписанию
    ./scripts/python.sh -m backend.reports.publish --check       # что мешает справке выйти: ОХД, модель, приложение, очередь
    ./scripts/python.sh -m backend.reports.publish --dry-run     # собрать и показать; не сохранять и не отправлять
    ./scripts/python.sh -m backend.reports.publish --week 2026-W38

Порядок запуска:
1. Очередь: выпуски, которые приложение ещё не приняло, уходят первыми.
2. Приложение говорит, что нужно: неделя не опубликована или администратор запросил
   перевыпуск. Приложение недоступно — собирается последняя завершённая неделя, если
   её ещё нет в очереди.
3. Сборка по витрине ОХД → текст ИИ на локальной модели, каждое число сверено (СП-06).
4. Копия в папку «Справки АЗС» в домашней папке → очередь → приложение.
Не собралось (нет данных, неполные данные, нет VPN) — в приложение уходит причина.
О сбоях и о публикации — уведомление macOS, одно на неделю и причину; «не собрана»
по плановой неделе — только после понедельника 13:00 МСК: раньше это обычное
ожидание данных за воскресенье.

Переменные (.env.local): DWH_DB_* — доступ к ОХД; AI_CATALOG, AI_DB_BACKEND=postgres;
AI_MODEL, AI_OLLAMA_HOST — модель для текста; REPORTS_IMPORT_TOKEN — токен приёма справок;
REPORTS_IMPORT_URL — адрес приложения; REPORTS_LOCAL_DIR — папка копий
(по умолчанию ~/Справки АЗС); REPORTS_NOTIFY=0 — без уведомлений.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
MSK = timezone(timedelta(hours=3))
TEXT_ATTEMPTS = 2
LOCAL_DIR_NAME = "Справки АЗС"
LABEL = "ru.azs-classifier.reports"
RUNTIME_DIR = Path.home() / "Library" / "Application Support" / "AZS Classifier" / "reports"
LOG_DIR = Path.home() / "Library" / "Logs" / "AZS Classifier"
DEADLINE_HOUR = 13  # понедельник, МСК: после этого «нет данных за воскресенье» — уже сбой
NOTIFIED_KEEP = 200


def load_env(root: Path = ROOT) -> None:
    """.env.local, затем .env; заданное в окружении важнее. Вызывать до импорта модулей ИИ."""
    for path in (root / ".env.local", root / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    catalog = os.environ.get("AI_CATALOG", "")
    if catalog and not Path(catalog).is_absolute():
        os.environ["AI_CATALOG"] = str(root / catalog)


def site_url() -> str:
    url = os.environ.get("REPORTS_IMPORT_URL", "").strip()
    if url:
        return url.rstrip("/")
    kpi = os.environ.get("KPI_IMPORT_URL", "").strip()
    if kpi:
        parts = urlsplit(kpi)
        return f"{parts.scheme}://{parts.netloc}"
    return ""


def local_dir() -> Path:
    """Папка копий. Домашняя папка, а не «Документы»: launchd в «Документы» без полного доступа к диску не пишет."""
    custom = os.environ.get("REPORTS_LOCAL_DIR", "").strip()
    return Path(custom).expanduser() if custom else Path.home() / LOCAL_DIR_NAME


def _short(err) -> str:
    return " ".join(str(err).split())[:200]


def _now_iso() -> str:
    return datetime.now(MSK).isoformat(timespec="seconds")


def _stamp(iso) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%d.%m %H:%M")
    except ValueError:
        return "—"


# --- приложение ---------------------------------------------------------------

class HttpError(RuntimeError):
    def __init__(self, code: int, detail: str):
        super().__init__(f"ответ {code}: {detail}")
        self.code, self.detail = code, detail


class ServerUnavailable(RuntimeError):
    """Сайт не ответил или сейчас не может принять — выпуск ждёт в очереди."""


class ServerRejected(RuntimeError):
    """Сайт отклонил выпуск по существу — повтор не поможет, выпуск откладывается в сторону."""


def _post(url: str, payload: dict, token: str = "", timeout: float = 120) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read(500).decode("utf-8", "replace")
        raise HttpError(err.code, " ".join(detail.split())) from err


def _get(url: str, timeout: float = 10) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class Server:
    """Внутренние адреса справок в приложении (токен REPORTS_IMPORT_TOKEN): на этом маке или на сайте."""

    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token
        parts = urlsplit(self.base)
        self.host = parts.netloc
        self.local = (parts.hostname or "") in ("127.0.0.1", "localhost", "::1")

    def _call(self, path: str, payload: dict, timeout: float = 120) -> dict:
        restart = "перезапустите приложение (npm start), чтобы оно перечитало код и .env.local"
        deploy = "bash deploy/deploy.sh"
        try:
            return _post(f"{self.base}{path}", payload, self.token, timeout)
        except HttpError as err:
            if err.code in (400, 413, 422):
                who = "приложение отклонило" if self.local else "сайт отклонил"
                raise ServerRejected(f"{who}: {err.detail[:300]}") from err
            if err.code == 503 and "token is not configured" in err.detail:
                raise ServerUnavailable(f"в приложении не задан токен справок — {restart}" if self.local else
                                        f"на сайте не задан токен справок — отвезите его на сервер: {deploy}") from err
            if err.code in (401, 403):
                raise ServerUnavailable(f"приложение не приняло токен справок — {restart}" if self.local else
                                        f"сайт не принял токен справок — отвезите его на сервер: {deploy}") from err
            if err.code in (404, 405):
                raise ServerUnavailable(f"в запущенном приложении нет приёма справок — {restart}" if self.local else
                                        f"на сайте ещё нет приёма справок — нужен деплой: {deploy}") from err
            raise ServerUnavailable(f"приложение {self.host} ответило {err.code}" if self.local else
                                    f"сайт {self.host} ответил {err.code}") from err
        except ValueError as err:  # ответ не JSON — скорее всего, старая версия приложения
            raise ServerUnavailable(f"приложение ответило не так, как ожидалось — {restart}" if self.local else
                                    "сайт ответил не так, как ожидалось — вероятно, на нём старая версия; "
                                    "нужен деплой") from err
        except (OSError, http.client.HTTPException) as err:  # нет сети, таймаут, обрыв соединения
            reason = _short(getattr(err, "reason", err))
            raise ServerUnavailable(f"приложение {self.host} не запущено ({reason}) — запустите его: npm start"
                                    if self.local else f"сайт {self.host} не отвечает ({reason})") from err

    def status(self, week: str) -> dict:
        return self._call("/api/internal/reports/status", {"week": week}, timeout=30)

    def publish(self, model: dict, request_id: int | None = None) -> dict:
        return self._call("/api/internal/reports/import", {"model": model, "requestId": request_id}, timeout=300)

    def not_formed(self, week: str, reason: str, request_id: int | None = None) -> dict:
        return self._call("/api/internal/reports/not-formed",
                          {"week": week, "reason": reason[:1000], "requestId": request_id}, timeout=60)


# --- модель и сборка ----------------------------------------------------------

def ask_model(task: dict) -> tuple[str, str, int]:
    """Запрос к локальной Ollama: тот же, что собрал narrative.task()."""
    host = os.environ.get("AI_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("AI_MODEL", "qwen3:8b")
    # Тот же контекст, что у приложения (backend/ai/generator.py): при другом num_ctx
    # Ollama перезагрузила бы модель, которую держит для ИИ-аналитика.
    options = dict(task["options"], num_ctx=int(os.environ.get("AI_NUM_CTX") or os.environ.get("AI_AGENT_NUM_CTX") or 32768))
    keep_alive = (os.environ.get("AI_KEEP_ALIVE") or "24h").strip()
    payload = {"model": model, "messages": task["messages"], "stream": False, "think": False,
               "format": task["format"], "options": options,
               "keep_alive": int(keep_alive) if keep_alive.lstrip("-").isdigit() else keep_alive}
    started = time.time()
    data = _post(f"{host}/api/chat", payload, timeout=float(os.environ.get("AI_MODEL_TIMEOUT") or 900))
    return (data.get("message") or {}).get("content") or "", model, int((time.time() - started) * 1000)


def write_text(model: dict, ask=ask_model) -> dict:
    """Текст ИИ со сверкой; не вышло за две попытки или модель недоступна — шаблон с причиной."""
    from . import narrative

    text = None
    for attempt in range(1, TEXT_ATTEMPTS + 1):
        try:
            content, name, duration_ms = ask(narrative.task(model, attempt=attempt))
        except Exception as err:  # noqa: BLE001 - модель не должна срывать выпуск
            return narrative.template(model, reason=f"ИИ недоступен: {_short(err)}")
        text = narrative.apply(model, content, model_name=name, duration_ms=duration_ms)
        if text["source"] == narrative.AI:
            return text
    return text


def build_model(week) -> dict:
    """Модель выпуска по витрине ОХД: запросы идут через валидатор и исполнитель с доступом только на чтение."""
    from ..ai import executor
    from . import weekly

    timeout = float(os.environ.get("REPORTS_SQL_TIMEOUT") or 300)
    return weekly.build(week, run=lambda sql, limit: executor.run(sql, limit, timeout_s=timeout))


# --- копии и очередь ----------------------------------------------------------

def notify_mac(message: str) -> None:
    """Уведомление macOS; на другой системе и при REPORTS_NOTIFY=0 — ничего."""
    if sys.platform != "darwin" or os.environ.get("REPORTS_NOTIFY", "1").strip() == "0":
        return
    try:
        # Текст передаётся аргументом, а не внутри скрипта: кавычки в причине ничего не сломают.
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run",
                        message[:250], "Справки АЗС"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Outbox:
    """Папка «Справки АЗС»: копии выпусков, очередь в приложение и память об уведомлениях.

    <папка>/2026-W38 (14–20 сентября 2026)/Справка_сеть_2026-W38.pdf и .xlsx — копия для чтения;
    <папка>/.queue/*.json — собранные выпуски, которые приложение ещё не приняло;
    <папка>/.queue/rejected/ — выпуски, которые приложение отклонило (повтор не поможет);
    <папка>/.state.json — что собрано и отправлено, о чём уже уведомляли.
    Папка закрыта для других пользователей мака: в выпуске цифры по объектам.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.queue = self.root / ".queue"
        self.rejected = self.queue / "rejected"
        self.state_path = self.root / ".state.json"

    def _ensure(self) -> None:
        self.queue.mkdir(parents=True, exist_ok=True)
        for folder in (self.root, self.queue):
            try:
                folder.chmod(0o700)
            except OSError:
                pass

    # состояние
    def state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _update(self, change) -> None:
        self._ensure()
        state = self.state()
        change(state)
        _write_json(self.state_path, state)

    def week_state(self, iso: str) -> dict:
        return dict((self.state().get("weeks") or {}).get(iso) or {})

    def _mark(self, iso: str, **fields) -> None:
        self._update(lambda state: state.setdefault("weeks", {}).setdefault(iso, {}).update(fields))

    def once(self, key: str) -> bool:
        """True — о таком ещё не уведомляли (теперь запомнено); False — уже уведомляли."""
        if key in (self.state().get("notified") or {}):
            return False

        def change(state):
            notified = state.setdefault("notified", {})
            notified[key] = _now_iso()
            for old in sorted(notified, key=notified.get)[:-NOTIFIED_KEEP]:
                del notified[old]
        self._update(change)
        return True

    def last_copy(self) -> str:
        weeks = self.state().get("weeks") or {}
        copies = [w["copy"] for _, w in sorted(weeks.items()) if w.get("copy")]
        return copies[-1] if copies else ""

    # копия
    def save_copy(self, model: dict, reason: str = "") -> tuple[Path, Path]:
        """PDF и Excel выпуска в папку недели; повторная сборка той же недели — «_2», «_3», прежние не трогаются."""
        from . import render_pdf, render_xlsx

        self._ensure()
        week = model["week"]
        folder = self.root / f"{week['iso']} ({week['label']})"
        folder.mkdir(parents=True, exist_ok=True)
        stem = name = f"Справка_сеть_{week['iso']}"
        number = 1
        while (folder / f"{name}.pdf").exists() or (folder / f"{name}.xlsx").exists():
            number += 1
            name = f"{stem}_{number}"
        copy = dict(model, localCopy=True, reason=reason)
        pdf, xlsx = folder / f"{name}.pdf", folder / f"{name}.xlsx"
        render_pdf.render(copy, pdf)
        render_xlsx.render(copy, xlsx)
        self._mark(week["iso"], copy=str(pdf))
        return pdf, xlsx

    # очередь
    def put(self, model: dict, request_id: int | None = None) -> dict:
        self._ensure()
        iso = model["week"]["iso"]
        suffix = f"_r{request_id}" if request_id else ""
        path = self.queue / f"{iso}_{datetime.now(MSK):%Y%m%dT%H%M%S}{suffix}.json"
        item = {"week": iso, "label": model["week"]["label"], "requestId": request_id, "builtAt": _now_iso(),
                "attempts": 0, "lastError": "", "model": model}
        _write_json(path, item)
        self._mark(iso, builtAt=item["builtAt"])
        return dict(item, path=str(path))

    def items(self) -> list[dict]:
        if not self.queue.is_dir():
            return []
        found = []
        for path in self.queue.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(item, dict) and item.get("week") and isinstance(item.get("model"), dict):
                found.append(dict(item, path=str(path)))
        return sorted(found, key=lambda item: (str(item.get("builtAt")), item["path"]))

    def has(self, iso: str) -> bool:
        return any(item["week"] == iso for item in self.items())

    def rejected_items(self) -> list[Path]:
        return sorted(self.rejected.glob("*.json")) if self.rejected.is_dir() else []

    @staticmethod
    def _data(item: dict) -> dict:
        return {key: value for key, value in item.items() if key != "path"}

    def sent(self, item: dict, answer: dict) -> None:
        Path(item["path"]).unlink(missing_ok=True)
        self._mark(item["week"], sentAt=_now_iso(), version=(answer.get("issue") or {}).get("version"))

    def failed(self, item: dict, error: str) -> None:
        data = self._data(item)
        data["attempts"] = int(data.get("attempts") or 0) + 1
        data["lastError"], data["lastTryAt"] = error, _now_iso()
        _write_json(Path(item["path"]), data)

    def reject(self, item: dict, error: str) -> None:
        path = Path(item["path"])
        _write_json(self.rejected / path.name, dict(self._data(item), rejected=error, rejectedAt=_now_iso()))
        path.unlink(missing_ok=True)

    def lock(self):
        """Один запуск за раз: расписание и ручной запуск не должны собирать одну неделю дважды."""
        self._ensure()
        handle = open(self.root / ".lock", "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle


def _queued_note(outbox: Outbox, week_iso: str, label: str, reason: str, alert) -> None:
    alert(f"{week_iso}:queued:{reason[:60]}",
          f"Справка за {label} собрана и сохранена в папке «{outbox.root.name}», но в приложение не ушла: {reason}. "
          "Уйдёт сама, когда приложение станет доступно.")


def deliver(server: Server, outbox: Outbox, item: dict, log, alert) -> dict:
    """Отправить выпуск из очереди: принят — убрать из очереди; приложение недоступно — ждать; отклонён — отложить."""
    label = item.get("label") or item["week"]
    try:
        answer = server.publish(item["model"], item.get("requestId"))
    except ServerUnavailable as err:
        outbox.failed(item, str(err))
        log(f"  не отправлена: {err}. Выпуск ждёт в очереди.")
        _queued_note(outbox, item["week"], label, str(err), alert)
        return {"outcome": "queued", "week": item["week"], "reason": str(err)}
    except ServerRejected as err:
        outbox.reject(item, str(err))
        log(f"  {err}")
        alert(f"{item['week']}:rejected:{str(err)[:60]}", f"Справку за {label} {err}")
        return {"outcome": "rejected", "week": item["week"], "reason": str(err)}
    outbox.sent(item, answer)
    version = (answer.get("issue") or {}).get("version")
    log(f"  приложение: {answer.get('outcome')}" + (f", версия {version}" if version else ""))
    if answer.get("outcome") == "published":
        alert(f"{item['week']}:published:{version}",
              f"Справка за {label} опубликована в приложении" + (f", версия {version}." if version else "."))
    return answer


def _overdue(week, now: datetime) -> bool:
    """После понедельника 13:00 МСК «не собрана» — сбой; раньше данные за воскресенье просто ещё не пришли."""
    day = week.end + timedelta(days=1)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MSK)
    return now >= datetime(day.year, day.month, day.day, DEADLINE_HOUR, tzinfo=MSK)


def run(server: Server | None, *, week: str = "", dry_run: bool = False, build=build_model, ask=ask_model,
        today: date | None = None, now: datetime | None = None, log=print, outbox: Outbox | None = None,
        notify=None) -> list[dict]:
    """Один запуск: очередь, затем перевыпуск по запросу и неопубликованная неделя, если они есть."""
    from ..ai.executor import ExecutionError
    from . import narrative, periods, weekly

    now = now or datetime.now(MSK)
    target = periods.parse_iso(week) if week else periods.last_complete_week(today or now.date())
    sending = server is not None and not dry_run
    if sending and outbox is None:
        outbox = Outbox(local_dir())
    notify = notify or notify_mac

    def alert(key: str, text: str) -> None:
        if outbox is not None and outbox.once(key):
            notify(text)

    results: list[dict] = []
    offline = ""
    if sending:
        for item in outbox.items():
            log(f"Очередь: справка за {item.get('label') or item['week']} (собрана {_stamp(item.get('builtAt'))}) — отправляю…")
            answer = deliver(server, outbox, item, log, alert)
            results.append(answer)
            if answer.get("outcome") == "queued":
                offline = answer["reason"]
                break

    state = {"published": False, "request": None}
    if sending and not offline:
        try:
            state = server.status(target.iso)
        except (ServerUnavailable, ServerRejected) as err:
            offline = str(err)
    if offline:
        log(f"Приложение недоступно: {offline}")
        if outbox.has(target.iso):
            log(f"Справка за {target.label} уже собрана и ждёт в очереди — из ОХД заново не собираю.")
            if not any(r.get("week") == target.iso for r in results):
                results.append({"outcome": "queued", "week": target.iso, "reason": offline})
            return results
        if outbox.week_state(target.iso).get("sentAt"):
            log(f"Справка за {target.label} уже принята приложением — собирать нечего.")
            return results + [{"outcome": "exists", "week": target.iso}]

    jobs = []
    request = state.get("request")
    if request:
        jobs.append((periods.parse_iso(request["week"]), request))
    if not state.get("published") and not (request and request["week"] == target.iso):
        jobs.append((target, None))
    if not jobs:
        log(f"Справка за {target.label} уже опубликована — собирать нечего.")
        return results + [{"outcome": "exists", "week": target.iso}]

    for job_week, job_request in jobs:
        request_id = job_request["id"] if job_request else None
        request_reason = job_request["reason"] if job_request else ""
        label = f"перевыпуск по запросу: {request_reason}" if job_request else "плановый выпуск"
        log(f"Справка за {job_week.label} ({label}): собираю по витрине ОХД…")
        started = time.time()
        try:
            model = build(job_week)
        except (weekly.ReportError, ExecutionError) as err:
            reason = str(err) if isinstance(err, weekly.ReportError) else f"нет доступа к витрине ОХД: {err}"
            log(f"  не собрана: {reason}")
            if sending and not offline:
                try:
                    server.not_formed(job_week.iso, reason, request_id)
                except (ServerUnavailable, ServerRejected) as send_err:
                    log(f"  причину в приложение передать не удалось: {send_err}")
            if sending and (job_request or _overdue(job_week, now)):
                alert(f"{job_week.iso}:not_formed:{reason[:60]}", f"Справка за {job_week.label} не собрана: {reason}")
            results.append({"outcome": "not_formed", "week": job_week.iso, "reason": reason})
            continue
        log(f"  цифры готовы за {time.time() - started:.0f} с; модель пишет текст…")
        model["narrative"] = write_text(model, ask)
        text = model["narrative"]
        log("  текст: " + ("ИИ, числа сверены" if text["source"] == narrative.AI else f"шаблон — {text['reason'] or 'без ИИ'}"))
        if not sending:
            for line in narrative.headline(model):
                log(f"    • {line}")
            for line in narrative.attention(model):
                log(f"    ! {line}")
            for rec in narrative.recommendations(model):
                log(f"    → {rec['action']}")
            results.append({"outcome": "dry_run", "week": job_week.iso, "model": model})
            continue
        try:
            pdf, _ = outbox.save_copy(model, request_reason)
            log(f"  копия: {pdf}")
        except Exception as err:  # noqa: BLE001 - без копии выпуск всё равно уходит в приложение
            log(f"  копию сохранить не удалось: {_short(err)}")
            alert(f"{job_week.iso}:copy:{_short(err)[:60]}",
                  f"Копия справки за {job_week.label} не сохранена: {_short(err)}")
        item = outbox.put(model, request_id)
        if offline:
            outbox.failed(item, offline)
            log("  приложение недоступно — выпуск ждёт в очереди и уйдёт при следующем запуске.")
            _queued_note(outbox, job_week.iso, job_week.label, offline, alert)
            results.append({"outcome": "queued", "week": job_week.iso, "reason": offline})
            continue
        answer = deliver(server, outbox, item, log, alert)
        if answer.get("outcome") == "queued":
            offline = answer["reason"]
        results.append(answer)
    return results


# --- проверка -----------------------------------------------------------------

def dwh_latest(today: date) -> date | None:
    """Последний день в витрине за три недели: тот же валидатор и исполнитель, что у сборки, короткий запрос."""
    from ..ai import executor
    from ..ai.catalog import CATALOG
    from ..ai.validator import Scope
    from . import weekly

    src = weekly.Source(CATALOG, lambda sql, limit: executor.run(sql, limit, timeout_s=60), Scope.all_network())
    rows = src.query(f'SELECT MAX({src.date}) AS "latest" FROM {src.facts} AS f '
                     f'WHERE {src.span(today - timedelta(days=21), today)}', 1)
    raw = rows[0]["latest"] if rows else None
    if raw is None:
        return None
    return raw if isinstance(raw, date) and not isinstance(raw, datetime) else date.fromisoformat(str(raw)[:10])


def ollama_models(host: str) -> list[str]:
    return [m.get("name", "") for m in (_get(f"{host}/api/tags", timeout=5).get("models") or [])]


def _code_digest(root: Path) -> str:
    digest = hashlib.sha256()
    base = root / "backend"
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(base)
        if rel.parts[0] == "tests" or "__pycache__" in rel.parts:
            continue
        digest.update(str(rel).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def schedule_status() -> tuple[str, str, str] | None:
    """Расписание launchd на маке: установлено ли, свежий ли у него код, когда был последний запуск."""
    if sys.platform != "darwin":
        return None
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if not plist.exists():
        return "!", "Расписание", "не установлено — scripts/install_reports_schedule.sh"
    try:
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True,
                                timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        loaded = False
    parts = ["каждый час 07:10–21:10" if loaded else "файл есть, но не загружен — scripts/install_reports_schedule.sh"]
    log_file = LOG_DIR / "reports.log"
    if log_file.exists():
        parts.append(f"последний запуск — {datetime.fromtimestamp(log_file.stat().st_mtime):%d.%m %H:%M}, журнал {log_file}")
    stale = False
    if ROOT.resolve() != RUNTIME_DIR.resolve() and (RUNTIME_DIR / "backend").is_dir():
        stale = _code_digest(ROOT) != _code_digest(RUNTIME_DIR)
        if stale:
            parts.append("код расписания старее проекта — перезапустите scripts/install_reports_schedule.sh")
    return ("✓" if loaded and not stale else "!"), "Расписание", "; ".join(parts)


def check(*, server=None, outbox: Outbox | None = None, latest=None, models=None, schedule=None,
          now: datetime | None = None, log=print) -> int:
    """Что мешает справке выйти — без сборки. 0 — витрина и приложение доступны."""
    from . import periods

    now = now or datetime.now(MSK)
    target = periods.last_complete_week(now.date())
    outbox = outbox or Outbox(local_dir())
    dwh_ok = site_ok = True

    def line(mark: str, title: str, text: str) -> None:
        log(f"{mark} {title}: {text}")

    log(f"Справки АЗС — проверка {now:%d.%m.%Y %H:%M} МСК. Ближайшая справка — за {target.label} ({target.iso}).")

    try:
        day = (latest or dwh_latest)(now.date())
    except Exception as err:  # noqa: BLE001 - проверка показывает любую причину
        dwh_ok = False
        line("✗", "Витрина ОХД", f"нет доступа — {_short(err)}. Нужен подключённый VPN и DWH_* в .env.local.")
    else:
        if day is None:
            dwh_ok = False
            line("✗", "Витрина ОХД", "доступна, но за последние три недели в ней нет данных")
        elif day < target.end:
            line("!", "Витрина ОХД", f"доступна; последний день — {day:%d.%m.%Y}. Данных за {target.end:%d.%m.%Y} "
                                     "ещё нет — справка соберётся, когда они появятся.")
        else:
            line("✓", "Витрина ОХД", f"доступна; последний день — {day:%d.%m.%Y}")

    host = os.environ.get("AI_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    name = os.environ.get("AI_MODEL", "qwen3:8b")
    try:
        names = (models or ollama_models)(host)
    except Exception as err:  # noqa: BLE001
        line("!", "Локальная модель", f"Ollama не отвечает на {host} ({_short(err)}) — текст справки будет "
                                      "по шаблону. Запустите Ollama.")
    else:
        if name in names or f"{name}:latest" in names:
            line("✓", "Локальная модель", f"Ollama отвечает, модель {name} есть")
        else:
            line("!", "Локальная модель", f"в Ollama нет модели {name}: ollama pull {name}. "
                                          "Без неё текст справки будет по шаблону.")

    base, token = site_url(), os.environ.get("REPORTS_IMPORT_TOKEN", "").strip()
    if server is None and (not base or not token):
        site_ok = False
        line("✗", "Приложение", "в .env.local нет REPORTS_IMPORT_TOKEN или адреса приложения (REPORTS_IMPORT_URL, KPI_IMPORT_URL); "
                          "токен создаст scripts/install_reports_schedule.sh")
    else:
        server = server or Server(base, token)
        try:
            state = server.status(target.iso)
        except (ServerUnavailable, ServerRejected) as err:
            site_ok = False
            hint = (" Если включён корпоративный VPN, он может закрывать сайт — выпуск подождёт в очереди."
                    if "не отвечает" in str(err) else "")
            line("✗", "Приложение", f"{err}.{hint}")
        else:
            published = "уже опубликована" if state.get("published") else "ещё не опубликована"
            request = state.get("request")
            extra = f"; ждёт перевыпуск за {request['week']}: {request['reason']}" if request else ""
            line("✓", "Приложение", f"{urlsplit(base).netloc or 'приложение'} принимает справки, токен принят; "
                              f"справка за {target.label} {published}{extra}")

    items = outbox.items()
    if items:
        listed = "; ".join(f"{i.get('label') or i['week']} — собрана {_stamp(i.get('builtAt'))}, "
                           f"попыток {i.get('attempts') or 0}" for i in items)
        line("!", "Очередь", f"ждут отправки {len(items)}: {listed}. "
                             f"Последняя причина: {items[-1].get('lastError') or 'ещё не отправляли'}")
    else:
        line("✓", "Очередь", "пуста — всё собранное принято приложением")
    rejected = outbox.rejected_items()
    if rejected:
        line("!", "Отклонённые", f"{len(rejected)} — приложение их не приняло, причина внутри файлов в {outbox.rejected}")
    copy = outbox.last_copy()
    line("•", "Копии", f"{outbox.root}" + (f"; последняя — {copy}" if copy else "; пока пусто"))

    status = (schedule or schedule_status)()
    if status:
        line(*status)

    if dwh_ok and site_ok:
        log("Итог: справка соберётся и уйдёт в приложение по расписанию.")
    elif dwh_ok:
        log(f"Итог: справка соберётся и сохранится в «{outbox.root}»; в приложение уйдёт, когда оно станет доступно.")
    elif site_ok:
        log("Итог: витрина недоступна — справку не из чего собрать. Подключите VPN; расписание повторит попытку "
            "в ближайший час.")
    else:
        log("Итог: недоступны и витрина, и приложение. Подключите VPN и запустите приложение.")
    return 0 if dwh_ok and site_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Справка по сети: сборка по витрине ОХД, копия на этом компьютере и отправка в приложение.")
    parser.add_argument("--week", default="", help="Неделя в виде 2026-W38; по умолчанию — последняя завершённая.")
    parser.add_argument("--dry-run", action="store_true", help="Собрать и показать; не сохранять и не отправлять.")
    parser.add_argument("--check", action="store_true",
                        help="Проверить витрину ОХД, локальную модель, приложение, очередь и расписание; ничего не собирать.")
    args = parser.parse_args(argv)
    load_env()
    if args.check:
        return check()
    if args.dry_run:
        results = run(None, week=args.week, dry_run=True)
        return 0 if all(r.get("outcome") == "dry_run" for r in results) else 1

    outbox = Outbox(local_dir())
    base, token = site_url(), os.environ.get("REPORTS_IMPORT_TOKEN", "").strip()
    if not base or not token:
        message = "не заданы REPORTS_IMPORT_TOKEN и адрес приложения (REPORTS_IMPORT_URL или KPI_IMPORT_URL) в .env.local"
        print(f"Справки: {message}.", file=sys.stderr)
        if outbox.once(f"config:{datetime.now(MSK):%Y-%m-%d}"):
            notify_mac(f"Справки не собираются: {message}.")
        return 2
    lock = outbox.lock()
    if lock is None:
        print("Другой запуск справок ещё идёт — этот пропускаю.")
        return 0
    try:
        results = run(Server(base, token), week=args.week, outbox=outbox)
    except Exception as err:  # noqa: BLE001 - о сбое сборщика владелец узнаёт не только из журнала
        if outbox.once(f"crash:{datetime.now(MSK):%Y-%m-%d}:{_short(err)[:60]}"):
            notify_mac(f"Сбой сборщика справок: {_short(err)}. Подробности — {LOG_DIR / 'reports.error.log'}")
        raise
    finally:
        lock.close()
    return 0 if all(r.get("outcome") in ("exists", "published") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
