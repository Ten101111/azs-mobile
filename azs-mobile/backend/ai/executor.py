"""Исполнение проверенного SQL.

Два исполнителя с одинаковым интерфейсом:

  sqlite   — демонстрационный стенд: локальная база агрегатов, открытая
             только на чтение;
  postgres — витрина данных в ОХД, соединение в режиме READ ONLY с
             ограничением времени выполнения на стороне сервера.

Переключение — переменной AI_DB_BACKEND. Остальной контур — контракт,
валидатор, область данных, журнал — от выбора исполнителя не зависит.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
KPI_DB = Path(os.environ.get("AI_KPI_DB") or DATA / "kpi_metrics.sqlite3")
REFERENCE_DB = Path(os.environ.get("AI_REFERENCE_DB") or DATA / "ai_reference.sqlite3")

BACKEND = (os.environ.get("AI_DB_BACKEND") or "sqlite").strip().lower()
DEFAULT_TIMEOUT_S = float(os.environ.get("AI_SQL_TIMEOUT", "15"))
# Потолок строк одного запроса: выше не поднимет ни роль, ни вкладка «Лимиты ИИ» (ИИ-26).
MAX_ROWS = int(os.environ.get("AI_MAX_ROWS", "10000"))


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    elapsed_ms: int
    truncated: bool


class ExecutionError(Exception):
    pass


# --- SQLite: демонстрационный стенд ----------------------------------------

def _sqlite_connect() -> sqlite3.Connection:
    if not KPI_DB.exists():
        raise ExecutionError(f"База КПЭ не найдена: {KPI_DB}")
    conn = sqlite3.connect(f"file:{KPI_DB}?mode=ro", uri=True, timeout=5)
    if REFERENCE_DB.exists():
        conn.execute("ATTACH DATABASE ? AS ref", (f"file:{REFERENCE_DB}?mode=ro",))
    return conn


def _sqlite_run(sql: str, row_limit: int, timeout_s: float) -> tuple[list[str], list]:
    conn = _sqlite_connect()
    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in (cursor.description or [])]
        rows = cursor.fetchmany(row_limit + 1)
        return columns, rows
    except sqlite3.OperationalError as err:
        if "interrupted" in str(err).lower():
            raise ExecutionError(f"Запрос остановлен по времени ({timeout_s:.0f} с)") from err
        raise ExecutionError(f"Ошибка выполнения: {err}") from err
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


# --- PostgreSQL: витрина данных в ОХД ---------------------------------------

def _postgres_run(sql: str, row_limit: int, timeout_s: float) -> tuple[list[str], list]:
    try:
        import psycopg2
    except ImportError as err:  # pragma: no cover
        raise ExecutionError("Не установлен psycopg2") from err

    required = ["DWH_DB_HOST", "DWH_DB_NAME", "DWH_DB_USER", "DWH_DB_PASSWORD"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ExecutionError("Не заданы переменные окружения: " + ", ".join(missing))

    statement_timeout_ms = int(timeout_s * 1000)
    try:
        conn = psycopg2.connect(
            host=os.environ["DWH_DB_HOST"],
            port=os.getenv("DWH_DB_PORT", "5432"),
            dbname=os.environ["DWH_DB_NAME"],
            user=os.environ["DWH_DB_USER"],
            password=os.environ["DWH_DB_PASSWORD"],
            connect_timeout=int(os.getenv("DWH_CONNECT_TIMEOUT_SECONDS", "10")),
            options=f"-c statement_timeout={statement_timeout_ms}",
        )
    except Exception as err:  # noqa: BLE001
        raise ExecutionError(f"Нет соединения с витриной: {err}") from err

    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cursor:
            cursor.execute(sql)
            columns = [d[0] for d in (cursor.description or [])]
            rows = cursor.fetchmany(row_limit + 1)
        conn.rollback()
        return columns, rows
    except Exception as err:  # noqa: BLE001
        message = str(err).strip()
        if "statement timeout" in message.lower():
            raise ExecutionError(f"Запрос остановлен по времени ({timeout_s:.0f} с)") from err
        raise ExecutionError(f"Ошибка выполнения: {message}") from err
    finally:
        conn.close()


# --- Приведение типов ------------------------------------------------------
#
# Драйверы отдают разные типы для одних и тех же данных: sqlite3 — только
# int/float/str/bytes/None, psycopg2 — Decimal для NUMERIC, date/datetime для
# дат, UUID, memoryview. Дальше значения уходят в json.dumps: в поток ответа
# (SSE), в журнал обращений, в диалог и в выгрузку. Без приведения первый же
# запрос с суммой выручки падает с «Object of type Decimal is not JSON
# serializable», причём только на витрине — на стенде SQLite такого типа нет.
#
# Приводим здесь, на границе исполнителя, а не в каждом потребителе: иначе
# один из них рано или поздно останется без обработки. Целевые типы — те же,
# что даёт SQLite, чтобы один вопрос давал одинаковый ответ на обоих
# исполнителях.

_MAX_EXACT_INT = 2 ** 53  # дальше JavaScript теряет точность в Number


def _normalize(value):
    """Значение драйвера → то, что переживёт json.dumps и JSON.parse."""
    if value is None or isinstance(value, (bool, str)):
        return value

    if isinstance(value, int):
        # NUMERIC без дробной части и BIGINT: за пределами 2^53 браузер
        # округлит число молча, поэтому отдаём строкой — пусть лучше
        # не посчитается, чем посчитается неверно.
        return value if abs(value) < _MAX_EXACT_INT else str(value)

    if isinstance(value, float):
        # float8 в Postgres умеет NaN и Infinity; json.dumps напишет их
        # литералами NaN/Infinity, и JSON.parse в браузере на этом упадёт.
        return value if math.isfinite(value) else None

    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if value == value.to_integral_value():
            exact = int(value)
            return exact if abs(exact) < _MAX_EXACT_INT else str(exact)
        return float(value)

    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.timedelta):
        return str(value)

    if isinstance(value, uuid.UUID):
        return str(value)

    if isinstance(value, (bytes, bytearray, memoryview)):
        # Двоичным данным в ответе витрины делать нечего, но молча терять
        # их тоже нельзя — показываем размер.
        return f"<{len(bytes(value))} байт>"

    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize(v) for v in value]

    return str(value)


RUNNERS = {"sqlite": _sqlite_run, "postgres": _postgres_run}


def run(sql: str, row_limit: int, timeout_s: float = DEFAULT_TIMEOUT_S) -> Result:
    runner = RUNNERS.get(BACKEND)
    if runner is None:
        raise ExecutionError(
            f"Неизвестный исполнитель AI_DB_BACKEND={BACKEND!r}; "
            f"допустимые значения: {', '.join(sorted(RUNNERS))}"
        )
    row_limit = max(1, min(int(row_limit), MAX_ROWS))
    started = time.monotonic()
    columns, rows = runner(sql, row_limit, timeout_s)
    truncated = len(rows) > row_limit
    return Result(
        columns=columns,
        rows=[tuple(_normalize(v) for v in r) for r in rows[:row_limit]],
        elapsed_ms=int((time.monotonic() - started) * 1000),
        truncated=truncated,
    )
