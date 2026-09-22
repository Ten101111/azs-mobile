"""Свод аналитики: состав панели и значения.

Период считается так же, как в справках: завершённый месяц берётся целиком,
текущий — с первого числа по сегодня, а сравнение идёт с тем же отрезком
годом ранее. Сравнивать неполный сентябрь с полным сентябрём прошлого года
нельзя — это главная причина, по которой «падение» оказывается мнимым.
"""
from __future__ import annotations

import calendar
import json
from datetime import date
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import summary
from .ai import executor
from .ai import scope as ai_scope
from .ai.validator import Rejected, validate


class TileValue(BaseModel):
    code: str
    title: str
    unit: str
    group: str
    decimals: int = 0
    hint: str = ""
    lowerIsBetter: bool = False
    value: float | None = None
    previous: float | None = None
    delta: float | None = None          # изменение к прошлому году
    isShare: bool = False               # для процентов разница в пунктах
    deltaUnit: str = "%"                # в чём измерено изменение
    deltaDecimals: int = 1


class SummaryResponse(BaseModel):
    period: str
    periodLabel: str
    comparedTo: str
    scopeLabel: str
    tiles: list[TileValue]
    available: list[dict]
    selected: list[str]
    maxTiles: int = summary.MAX_TILES
    minTiles: int = summary.MIN_TILES
    latestDate: str = ""                # последний день, который есть в витрине
    source: str
    error: str = ""


class TilesRequest(BaseModel):
    tiles: list[str] = Field(default_factory=list)


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")
MONTHS_NOM = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")


def month_range(period: str, today: date | None = None) -> tuple[str, str, str]:
    """Границы периода и человеческая подпись."""
    today = today or date.today()
    year, month = int(period[:4]), int(period[5:7])
    first = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    if (year, month) == (today.year, today.month):
        last = today
        label = f"{MONTHS_NOM[month - 1]} {year}, с 1 по {today.day} {MONTHS[month - 1]}"
    else:
        last = date(year, month, last_day)
        label = f"{MONTHS_NOM[month - 1]} {year}"
    return first.isoformat(), last.isoformat(), label


def previous_year_range(date_from: str, date_to: str) -> tuple[str, str, str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    prev_start = date(start.year - 1, start.month, min(start.day, calendar.monthrange(start.year - 1, start.month)[1]))
    prev_end = date(end.year - 1, end.month, min(end.day, calendar.monthrange(end.year - 1, end.month)[1]))
    label = f"тем же отрезком {prev_start.year} года"
    return prev_start.isoformat(), prev_end.isoformat(), label


def _run(tiles: list[summary.Tile], date_from: str, date_to: str, user) -> dict:
    sql = summary.build_query(tiles, date_from, date_to)
    checked = validate(sql, ai_scope.from_user(user))
    result = executor.run(checked.sql, checked.row_limit)
    if not result.rows:
        return {}
    return dict(zip(result.columns, result.rows[0]))


def _latest_date(user) -> date | None:
    checked = validate(summary.latest_date_query(), ai_scope.from_user(user))
    result = executor.run(checked.sql, 1)
    if not result.rows or result.rows[0][0] is None:
        return None
    raw = result.rows[0][0]
    return raw if isinstance(raw, date) else date.fromisoformat(str(raw)[:10])


def build_router(require_user: Callable, auth_connection: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/summary", tags=["summary"])

    def read_selection(user) -> list[str]:
        with auth_connection() as conn:
            row = conn.execute("SELECT summary_tiles FROM users WHERE id = ?", (user.id,)).fetchone()
        raw = (row["summary_tiles"] if row and "summary_tiles" in row.keys() else "") or ""
        try:
            saved = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            saved = []
        return [code for code in saved if isinstance(code, str)]

    @router.get("", response_model=SummaryResponse)
    def summary_values(period: str = "", user=Depends(require_user)):
        today = date.today()
        period = period or f"{today.year:04d}-{today.month:02d}"
        if len(period) != 7 or period[4] != "-":
            raise HTTPException(status_code=400, detail="Период задаётся как ГГГГ-ММ")

        available = summary.available_tiles()
        selected = read_selection(user)
        tiles = summary.resolve_selection(selected or None, user.role)
        payload = SummaryResponse(
            period=period, periodLabel="", comparedTo="", scopeLabel=user.scopeLabel,
            tiles=[], available=[summary.describe(tile) for tile in available],
            selected=[tile.code for tile in tiles], source=executor.BACKEND,
        )
        if not tiles:
            payload.error = ("Для текущей витрины нет ни одного доступного показателя. "
                             "Плитки появятся, когда в витрине будут нужные столбцы.")
            return payload

        # Текущий месяц обрезаем по последнему дню, который реально есть в
        # витрине. Иначе неполные сутки сравниваются с полными годом ранее,
        # и падение показателей оказывается мнимым.
        boundary = today
        try:
            latest = _latest_date(user)
            if latest:
                boundary = min(today, latest)
                payload.latestDate = latest.isoformat()
        except (Rejected, executor.ExecutionError):
            pass

        date_from, date_to, label = month_range(period, boundary)
        prev_from, prev_to, prev_label = previous_year_range(date_from, date_to)
        payload.periodLabel, payload.comparedTo = label, prev_label

        try:
            current = _run(tiles, date_from, date_to, user)
            previous = _run(tiles, prev_from, prev_to, user)
        except Rejected as err:
            payload.error = f"Запрос отклонён проверкой: {err.message}"
            return payload
        except executor.ExecutionError as err:
            payload.error = str(err)
            return payload

        for tile in tiles:
            value = current.get(tile.code)
            was = previous.get(tile.code)
            delta = None
            is_share = tile.unit == "%"
            # Доли сравниваем в пунктах, балльную оценку — в баллах: «средняя
            # оценка упала на 1 %» читается как проценты сервиса и вводит в
            # заблуждение. Всё остальное — в процентах к прошлому году.
            in_place = tile.unit in ("%", "балл")
            decimals = 3 if tile.unit == "балл" else 1
            if value is not None and was is not None:
                if in_place:
                    delta = round(float(value) - float(was), decimals)
                elif float(was):
                    delta = round((float(value) / float(was) - 1) * 100, 1)
            payload.tiles.append(TileValue(
                **summary.describe(tile),
                value=None if value is None else float(value),
                previous=None if was is None else float(was),
                delta=delta,
                isShare=is_share,
                deltaUnit="п.п." if is_share else ("балла" if tile.unit == "балл" else "%"),
                deltaDecimals=decimals if in_place else 1,
            ))
        return payload

    @router.get("/catalog")
    def catalog(user=Depends(require_user)):
        """Состав каталога без обращения к витрине.

        Нужен фронту, чтобы понять, есть ли свод вообще, и показать окно
        настройки, не гоняя тяжёлый запрос за значениями.
        """
        available = summary.available_tiles()
        selected = read_selection(user)
        return {
            "available": [summary.describe(tile) for tile in available],
            "selected": [tile.code for tile in summary.resolve_selection(selected or None, user.role)],
            "default": summary.template_for(user.role),
            "maxTiles": summary.MAX_TILES,
            "minTiles": summary.MIN_TILES,
            "scopeLabel": user.scopeLabel,
        }

    @router.get("/periods")
    def periods(user=Depends(require_user)):
        """Месяцы, за которые в витрине есть данные, от свежего к старому."""
        try:
            checked = validate(summary.months_query(), ai_scope.from_user(user), row_limit=240)
            result = executor.run(checked.sql, checked.row_limit)
            months = {str(row[0])[:7] for row in result.rows if row and row[0]}
        except (Rejected, executor.ExecutionError):
            months = set()
        return {"periods": sorted((m for m in months if len(m) == 7 and m[4] == "-"), reverse=True)}

    @router.post("/tiles")
    def save_tiles(payload: TilesRequest, user=Depends(require_user)):
        available = {tile.code for tile in summary.available_tiles()}
        chosen = [code for code in payload.tiles if code in available][: summary.MAX_TILES]
        if len(chosen) < summary.MIN_TILES:
            raise HTTPException(
                status_code=400,
                detail=f"Оставьте не меньше {summary.MIN_TILES} плиток",
            )
        with auth_connection() as conn:
            conn.execute("UPDATE users SET summary_tiles = ? WHERE id = ?",
                         (json.dumps(chosen, ensure_ascii=False), user.id))
            conn.commit()
        return {"selected": chosen}

    @router.post("/reset")
    def reset_tiles(user=Depends(require_user)):
        with auth_connection() as conn:
            conn.execute("UPDATE users SET summary_tiles = '' WHERE id = ?", (user.id,))
            conn.commit()
        return {"selected": summary.template_for(user.role)}

    return router
