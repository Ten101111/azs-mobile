from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field


APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DATA_DIR = PROJECT_DIR / "data"

GASOLINE_OCTANE_GRADES = (80, 91, 92, 93, 95, 98, 100)
CANONICAL_FUELS = tuple(
    fuel
    for grade in GASOLINE_OCTANE_GRADES
    for fuel in (f"АБ{grade}", f"АБ{grade} ЭКТО")
) + ("ДТ", "ДТ ЭКТО", "СУГ", "КПГ")
UNMAPPED_FUEL = "unmapped"
FUEL_SORT_ORDER = {fuel: index for index, fuel in enumerate((*CANONICAL_FUELS, UNMAPPED_FUEL))}
STORED_VOLUME_UNITS_PER_TON = 1000.0
DEFAULT_STALE_AFTER_SECONDS = 90 * 60
DEFAULT_MIN_COVERAGE_RATIO = 0.5
ARITHMETIC_ABS_TOLERANCE = 0.1
PERCENT_ABS_TOLERANCE = 0.1


class FuelStockImportError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FuelStockImportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    ksss: str = Field(min_length=1, max_length=32)
    canonicalFuel: str = Field(min_length=1, max_length=40)
    capacityLiters: float = Field(gt=0)
    volumeLiters: float = Field(ge=0)
    deadRestLiters: float = Field(ge=0)
    availableLiters: float = Field(ge=0)
    fillPercent: float = Field(ge=0)
    tanksCount: int = Field(ge=1)
    sourceFuelNames: list[str] = Field(min_length=1, max_length=100)
    sourceFuelNameCounts: dict[str, int] = Field(min_length=1, max_length=100)


class FuelStockImportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(default="dwh-fuel-stock-sync", max_length=120)
    accountDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    snapshotAt: Optional[str] = Field(default=None, max_length=80)
    checksum: Optional[str] = Field(default=None, max_length=128)
    records: list[FuelStockImportRecord] = Field(min_length=1)


class FuelStockItem(BaseModel):
    canonicalFuel: str
    fuelCode: str
    fuelName: str
    capacityLiters: float
    physicalVolumeLiters: float
    volumeLiters: float
    deadRestLiters: float
    availableVolumeLiters: float
    availableLiters: float
    capacityTons: float
    physicalVolumeTons: float
    volumeTons: float
    deadRestTons: float
    availableVolumeTons: float
    availableTons: float
    percentage: float
    fillPercent: float
    status: str
    isLow: bool
    businessLow: bool
    tanksCount: int
    sourceFuelNames: list[str]
    sourceFuelNameCount: int
    sourceFuelNameCounts: dict[str, int]


class FuelStockStationResponse(BaseModel):
    ksss: str
    source: str
    accountDate: str
    snapshotAt: str
    importedAt: str
    stale: bool
    staleAfterSeconds: int
    items: list[FuelStockItem]


class FuelStockImportResponse(BaseModel):
    ok: bool
    unchanged: bool = False
    imported: int
    stations: int
    accountDate: str
    snapshotAt: str
    importedAt: str
    stale: bool


@dataclass(frozen=True)
class ValidatedFuelStockRow:
    ksss: str
    canonical_fuel: str
    capacity_liters: float
    volume_liters: float
    dead_rest_liters: float
    available_liters: float
    fill_percent: float
    status: str
    business_low: bool
    tanks_count: int
    source_fuel_names: list[str]
    source_fuel_name_counts: dict[str, int]


def fuel_stock_db_path() -> Path:
    configured = os.getenv("FUEL_STOCK_DB_PATH", "").strip()
    return Path(configured) if configured else DATA_DIR / "fuel_stock.sqlite3"


def stale_after_seconds() -> int:
    try:
        return max(60, int(os.getenv("FUEL_STOCK_STALE_AFTER_SECONDS", str(DEFAULT_STALE_AFTER_SECONDS))))
    except ValueError:
        return DEFAULT_STALE_AFTER_SECONDS


def min_coverage_ratio() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("FUEL_STOCK_MIN_COVERAGE_RATIO", str(DEFAULT_MIN_COVERAGE_RATIO)))))
    except ValueError:
        return DEFAULT_MIN_COVERAGE_RATIO


def dwh_source_timezone() -> ZoneInfo:
    name = os.getenv("DWH_SOURCE_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


def utc_now_iso(now: Optional[datetime] = None) -> str:
    resolved = now or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FuelStockImportError(422, "snapshotAt must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise FuelStockImportError(422, "snapshotAt must include timezone")
    return parsed.astimezone(timezone.utc)


def normalize_source_timestamp(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dwh_source_timezone())
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize_snapshot_at(value: Optional[str], fallback: str) -> str:
    if not value:
        return fallback
    return parse_iso_datetime(value).isoformat(timespec="seconds")


def validate_account_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise FuelStockImportError(422, "accountDate must use YYYY-MM-DD format") from exc


def is_stale(snapshot_at: str, stale_seconds: Optional[int] = None, now: Optional[datetime] = None) -> bool:
    try:
        snapshot = parse_iso_datetime(snapshot_at)
    except FuelStockImportError:
        return True
    resolved_now = now or datetime.now(timezone.utc)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=timezone.utc)
    return (resolved_now.astimezone(timezone.utc) - snapshot).total_seconds() > (stale_seconds or stale_after_seconds())


def _secure_db_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.chmod(0o600)
        except FileNotFoundError:
            pass


def fuel_stock_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or fuel_stock_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    _secure_db_files(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    _secure_db_files(path)
    return conn


def init_fuel_stock_db(db_path: Optional[Path] = None) -> None:
    with fuel_stock_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fuel_stock_current (
                ksss TEXT NOT NULL,
                canonical_fuel TEXT NOT NULL,
                capacity_liters REAL NOT NULL CHECK (capacity_liters > 0),
                volume_liters REAL NOT NULL CHECK (volume_liters >= 0),
                dead_rest_liters REAL NOT NULL CHECK (dead_rest_liters >= 0),
                available_liters REAL NOT NULL CHECK (available_liters >= 0),
                fill_percent REAL NOT NULL CHECK (fill_percent >= 0),
                status TEXT NOT NULL CHECK (status IN ('red', 'orange', 'green')),
                business_low INTEGER NOT NULL CHECK (business_low IN (0, 1)),
                tanks_count INTEGER NOT NULL CHECK (tanks_count > 0),
                source_fuel_names_json TEXT NOT NULL,
                source_fuel_name_counts_json TEXT NOT NULL,
                account_date TEXT NOT NULL CHECK (length(account_date) = 10),
                snapshot_at TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (ksss, canonical_fuel)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fuel_stock_current_ksss ON fuel_stock_current(ksss)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fuel_stock_current_imported ON fuel_stock_current(imported_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fuel_stock_snapshot_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                source TEXT NOT NULL,
                account_date TEXT NOT NULL CHECK (length(account_date) = 10),
                snapshot_at TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                row_count INTEGER NOT NULL CHECK (row_count >= 0),
                station_count INTEGER NOT NULL CHECK (station_count >= 0)
            )
            """
        )
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(fuel_stock_snapshot_meta)").fetchall()}
        if "checksum" not in columns:
            conn.execute("ALTER TABLE fuel_stock_snapshot_meta ADD COLUMN checksum TEXT NOT NULL DEFAULT ''")


def _fold_fuel_text(value: str) -> tuple[str, str, str]:
    upper = str(value or "").strip().upper().replace("Ё", "Е")
    raw_compact = re.sub(r"[^A-ZА-Я0-9]+", "", upper)
    cyr_like = upper.translate(
        str.maketrans(
            {
                "A": "А",
                "B": "В",
                "E": "Е",
                "K": "К",
                "M": "М",
                "H": "Н",
                "O": "О",
                "P": "Р",
                "C": "С",
                "T": "Т",
                "X": "Х",
                "Y": "У",
            }
        )
    )
    tokens = re.sub(r"[^A-ZА-Я0-9]+", " ", upper).strip()
    compact = re.sub(r"[^A-ZА-Я0-9]+", "", cyr_like)
    return tokens, compact, raw_compact


def normalize_fuel_name(value: str) -> str:
    tokens, compact, raw_compact = _fold_fuel_text(value)
    if not compact and not raw_compact:
        return UNMAPPED_FUEL

    excluded_markers = ("ПРИСАД", "ADDITIVE")
    if any(marker in compact or marker in raw_compact for marker in excluded_markers):
        return UNMAPPED_FUEL

    is_lpg = any(
        marker in compact or marker in raw_compact
        for marker in ("СУГ", "СПБТ", "СЖИЖЕН", "ПРОПАН", "ПБА", "LPG")
    )
    if is_lpg:
        return "СУГ"

    is_cng = any(marker in compact or marker in raw_compact for marker in ("КПГ", "МЕТАН", "CNG"))
    if is_cng:
        return "КПГ"

    is_ecto = any(marker in compact for marker in ("ЭКТО", "ЕКТО")) or any(marker in raw_compact for marker in ("ECTO", "EKTO"))
    diesel_pattern = r"(^|[^A-ZА-Я0-9])(ДТ|ДИЗЕЛ|ДИЗЕЛЬ|DT|DIESEL)([^A-ZА-Я0-9]|$)"
    is_diesel = (
        bool(re.search(diesel_pattern, tokens))
        or compact.startswith("ДТ")
        or "ДИЗЕЛ" in compact
        or any(marker in compact for marker in ("ДТЛ", "ДТЗ", "ДТЕ"))
    )
    if is_diesel:
        return "ДТ ЭКТО" if is_ecto else "ДТ"

    grades = {
        grade
        for grade in GASOLINE_OCTANE_GRADES
        if bool(re.search(rf"(?<!\d){grade}(?!\d)", tokens))
        or any(
            marker in compact or marker in raw_compact
            for marker in (f"АИ{grade}", f"АБ{grade}", f"AI{grade}", f"AB{grade}")
        )
    }
    if grades == {91, 92, 93}:
        grades = {92}
    if len(grades) == 1:
        grade = grades.pop()
        return f"АБ{grade} ЭКТО" if is_ecto else f"АБ{grade}"
    return UNMAPPED_FUEL


def status_for_percent(percent: float) -> str:
    if percent < 30:
        return "red"
    if percent <= 70:
        return "orange"
    return "green"


def business_low_for_percent(percent: float) -> bool:
    return percent < 20


def _is_close(actual: float, expected: float, absolute_tolerance: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-6, abs_tol=absolute_tolerance)


def _clean_source_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    return cleaned[:120] if cleaned else "unknown"


def _validate_source_names(record: FuelStockImportRecord) -> tuple[list[str], dict[str, int]]:
    source_names = [_clean_source_name(item) for item in record.sourceFuelNames]
    source_counts = {_clean_source_name(key): int(value) for key, value in record.sourceFuelNameCounts.items()}
    if any(count <= 0 for count in source_counts.values()):
        raise FuelStockImportError(422, "sourceFuelNameCounts values must be positive")
    if set(source_names) != set(source_counts):
        raise FuelStockImportError(422, "sourceFuelNames must match sourceFuelNameCounts keys")
    if sum(source_counts.values()) != record.tanksCount:
        raise FuelStockImportError(422, "sourceFuelNameCounts must sum to tanksCount")
    normalized_sources = {normalize_fuel_name(name) for name in source_names}
    if normalized_sources != {record.canonicalFuel}:
        raise FuelStockImportError(422, "source fuel names do not match canonicalFuel")
    return sorted(set(source_names)), dict(sorted(source_counts.items()))


def validate_import_record(record: FuelStockImportRecord) -> ValidatedFuelStockRow:
    canonical_fuel = record.canonicalFuel.strip()
    if canonical_fuel not in CANONICAL_FUELS:
        raise FuelStockImportError(422, f"Unsupported canonicalFuel: {canonical_fuel}")
    ksss = record.ksss.strip()
    if not re.fullmatch(r"\d{1,32}", ksss):
        raise FuelStockImportError(422, "ksss must contain digits only")

    source_names, source_counts = _validate_source_names(record)
    capacity = float(record.capacityLiters)
    volume = float(record.volumeLiters)
    dead_rest = float(record.deadRestLiters)
    available = float(record.availableLiters)
    lower_available_bound = max(volume - dead_rest, 0)
    if available + ARITHMETIC_ABS_TOLERANCE < lower_available_bound:
        raise FuelStockImportError(422, "availableLiters is lower than aggregate volume minus dead rest")
    if available > volume + ARITHMETIC_ABS_TOLERANCE:
        raise FuelStockImportError(422, "availableLiters cannot exceed volumeLiters")

    expected_percent = (available / capacity) * 100
    if not _is_close(float(record.fillPercent), expected_percent, PERCENT_ABS_TOLERANCE):
        raise FuelStockImportError(422, "fillPercent must equal availableLiters / capacityLiters * 100")

    fill_percent = round(expected_percent, 4)
    return ValidatedFuelStockRow(
        ksss=ksss,
        canonical_fuel=canonical_fuel,
        capacity_liters=round(capacity, 4),
        volume_liters=round(volume, 4),
        dead_rest_liters=round(dead_rest, 4),
        available_liters=round(available, 4),
        fill_percent=fill_percent,
        status=status_for_percent(fill_percent),
        business_low=business_low_for_percent(fill_percent),
        tanks_count=int(record.tanksCount),
        source_fuel_names=source_names,
        source_fuel_name_counts=source_counts,
    )


def snapshot_checksum(rows: list[ValidatedFuelStockRow]) -> str:
    payload = [
        {
            "ksss": row.ksss,
            "canonicalFuel": row.canonical_fuel,
            "capacityLiters": row.capacity_liters,
            "volumeLiters": row.volume_liters,
            "deadRestLiters": row.dead_rest_liters,
            "availableLiters": row.available_liters,
            "fillPercent": row.fill_percent,
            "tanksCount": row.tanks_count,
            "sourceFuelNameCounts": row.source_fuel_name_counts,
        }
        for row in sorted(rows, key=lambda item: (item.ksss, FUEL_SORT_ORDER.get(item.canonical_fuel, 99), item.canonical_fuel))
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _previous_station_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(DISTINCT ksss) AS count FROM fuel_stock_current").fetchone()
    return int(row["count"] or 0) if row else 0


def replace_fuel_stock_snapshot(
    payload: FuelStockImportPayload,
    db_path: Optional[Path] = None,
    coverage_ratio: Optional[float] = None,
    now: Optional[datetime] = None,
) -> FuelStockImportResponse:
    if not payload.records:
        raise FuelStockImportError(422, "Fuel stock snapshot must not be empty")

    account_date = validate_account_date(payload.accountDate)
    imported_at = utc_now_iso(now)
    snapshot_at = normalize_snapshot_at(payload.snapshotAt, imported_at)
    source = (payload.source or "dwh-fuel-stock-sync").strip()[:120]
    rows = [validate_import_record(record) for record in payload.records]
    checksum = snapshot_checksum(rows)
    provided_checksum = (payload.checksum or "").strip().lower()
    if provided_checksum and not secrets.compare_digest(provided_checksum, checksum):
        raise FuelStockImportError(422, "checksum does not match the normalized snapshot")

    seen_keys: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.ksss, row.canonical_fuel)
        if key in seen_keys:
            raise FuelStockImportError(422, "Fuel stock snapshot must contain one row per (ksss, canonicalFuel)")
        seen_keys.add(key)

    station_count = len({row.ksss for row in rows})
    if station_count == 0:
        raise FuelStockImportError(422, "Fuel stock snapshot must include at least one station")

    init_fuel_stock_db(db_path)
    ratio = min_coverage_ratio() if coverage_ratio is None else max(0.0, min(1.0, coverage_ratio))
    with fuel_stock_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing_meta = conn.execute("SELECT * FROM fuel_stock_snapshot_meta WHERE id = 1").fetchone()
            if (
                existing_meta
                and str(existing_meta["snapshot_at"] or "") == snapshot_at
                and int(existing_meta["row_count"] or 0) == len(rows)
                and str(existing_meta["checksum"] or "") == checksum
            ):
                conn.commit()
                return FuelStockImportResponse(
                    ok=True,
                    unchanged=True,
                    imported=0,
                    stations=int(existing_meta["station_count"] or station_count),
                    accountDate=str(existing_meta["account_date"] or account_date),
                    snapshotAt=snapshot_at,
                    importedAt=str(existing_meta["imported_at"] or imported_at),
                    stale=is_stale(snapshot_at, now=now),
                )

            previous_count = _previous_station_count(conn)
            if previous_count and ratio and station_count < previous_count * ratio:
                raise FuelStockImportError(
                    409,
                    f"Fuel stock snapshot station coverage dropped from {previous_count} to {station_count}",
                )

            conn.execute("DELETE FROM fuel_stock_current")
            conn.executemany(
                """
                INSERT INTO fuel_stock_current (
                    ksss, canonical_fuel, capacity_liters, volume_liters, dead_rest_liters,
                    available_liters, fill_percent, status, business_low, tanks_count,
                    source_fuel_names_json, source_fuel_name_counts_json,
                    account_date, snapshot_at, imported_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row.ksss,
                        row.canonical_fuel,
                        row.capacity_liters,
                        row.volume_liters,
                        row.dead_rest_liters,
                        row.available_liters,
                        row.fill_percent,
                        row.status,
                        1 if row.business_low else 0,
                        row.tanks_count,
                        json.dumps(row.source_fuel_names, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(row.source_fuel_name_counts, ensure_ascii=False, separators=(",", ":")),
                        account_date,
                        snapshot_at,
                        imported_at,
                        source,
                    )
                    for row in rows
                ],
            )
            conn.execute(
                """
                INSERT INTO fuel_stock_snapshot_meta (
                    id, source, account_date, snapshot_at, imported_at, row_count, station_count, checksum
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    account_date = excluded.account_date,
                    snapshot_at = excluded.snapshot_at,
                    imported_at = excluded.imported_at,
                    row_count = excluded.row_count,
                    station_count = excluded.station_count,
                    checksum = excluded.checksum
                """,
                (source, account_date, snapshot_at, imported_at, len(rows), station_count, checksum),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return FuelStockImportResponse(
        ok=True,
        unchanged=False,
        imported=len(rows),
        stations=station_count,
        accountDate=account_date,
        snapshotAt=snapshot_at,
        importedAt=imported_at,
        stale=is_stale(snapshot_at, now=now),
    )


def fuel_item_from_row(row: sqlite3.Row) -> FuelStockItem:
    source_names = json.loads(str(row["source_fuel_names_json"] or "[]"))
    source_counts = json.loads(str(row["source_fuel_name_counts_json"] or "{}"))
    capacity = float(row["capacity_liters"] or 0)
    physical_volume = float(row["volume_liters"] or 0)
    dead_rest = float(row["dead_rest_liters"] or 0)
    available = float(row["available_liters"] or 0)
    return FuelStockItem(
        canonicalFuel=str(row["canonical_fuel"]),
        fuelCode=str(row["canonical_fuel"]),
        fuelName=str(row["canonical_fuel"]),
        capacityLiters=capacity,
        physicalVolumeLiters=physical_volume,
        volumeLiters=physical_volume,
        deadRestLiters=dead_rest,
        availableVolumeLiters=available,
        availableLiters=available,
        capacityTons=round(capacity / STORED_VOLUME_UNITS_PER_TON, 4),
        physicalVolumeTons=round(physical_volume / STORED_VOLUME_UNITS_PER_TON, 4),
        volumeTons=round(physical_volume / STORED_VOLUME_UNITS_PER_TON, 4),
        deadRestTons=round(dead_rest / STORED_VOLUME_UNITS_PER_TON, 4),
        availableVolumeTons=round(available / STORED_VOLUME_UNITS_PER_TON, 4),
        availableTons=round(available / STORED_VOLUME_UNITS_PER_TON, 4),
        percentage=float(row["fill_percent"] or 0),
        fillPercent=float(row["fill_percent"] or 0),
        status=str(row["status"]),
        isLow=bool(row["business_low"]),
        businessLow=bool(row["business_low"]),
        tanksCount=int(row["tanks_count"] or 0),
        sourceFuelNames=[str(item) for item in source_names],
        sourceFuelNameCount=len(source_names),
        sourceFuelNameCounts={str(key): int(value) for key, value in source_counts.items()},
    )


def get_station_fuel_stock(ksss: str, db_path: Optional[Path] = None) -> Optional[FuelStockStationResponse]:
    init_fuel_stock_db(db_path)
    with fuel_stock_connection(db_path) as conn:
        meta = conn.execute("SELECT * FROM fuel_stock_snapshot_meta WHERE id = 1").fetchone()
        rows = conn.execute(
            """
            SELECT *
            FROM fuel_stock_current
            WHERE ksss = ?
            ORDER BY canonical_fuel
            """,
            (ksss,),
        ).fetchall()

    if not meta or not rows:
        return None

    stale_seconds = stale_after_seconds()
    snapshot_at = str(meta["snapshot_at"] or "")
    imported_at = str(meta["imported_at"] or "")
    return FuelStockStationResponse(
        ksss=ksss,
        source=str(meta["source"] or ""),
        accountDate=str(meta["account_date"] or ""),
        snapshotAt=snapshot_at,
        importedAt=imported_at,
        stale=is_stale(snapshot_at, stale_seconds),
        staleAfterSeconds=stale_seconds,
        items=sorted(
            (fuel_item_from_row(row) for row in rows),
            key=lambda item: (FUEL_SORT_ORDER.get(item.canonicalFuel, 99), item.canonicalFuel),
        ),
    )


def fuel_stock_health(db_path: Optional[Path] = None) -> dict[str, Any]:
    init_fuel_stock_db(db_path)
    with fuel_stock_connection(db_path) as conn:
        meta = conn.execute("SELECT * FROM fuel_stock_snapshot_meta WHERE id = 1").fetchone()
        rows = conn.execute(
            "SELECT COUNT(*) AS rows_count, COUNT(DISTINCT ksss) AS station_count FROM fuel_stock_current"
        ).fetchone()
    imported_at = str(meta["imported_at"] or "") if meta else ""
    snapshot_at = str(meta["snapshot_at"] or "") if meta else ""
    return {
        "ok": True,
        "rows": int(rows["rows_count"] or 0) if rows else 0,
        "stations": int(rows["station_count"] or 0) if rows else 0,
        "snapshotAt": snapshot_at,
        "importedAt": imported_at,
        "stale": is_stale(snapshot_at) if snapshot_at else True,
    }


def _pick(row: Mapping[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return None


def _as_float(value: Any, field: str) -> float:
    if value is None or value == "":
        raise ValueError(f"empty {field}")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty account_date")
    return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()


def normalize_tank_row(row: Mapping[str, Any]) -> dict[str, Any]:
    account_date = _iso_date(_pick(row, "accountDate", "account_date", "report_date", "date"))
    ksss = str(_pick(row, "ksss", "station_id", "station", "azs_id") or "").strip()
    source_fuel_name = _clean_source_name(str(_pick(row, "fuelName", "fuel_name", "oil_name", "product_name") or ""))
    return {
        "accountDate": account_date,
        "ksss": ksss,
        "sourceFuelName": source_fuel_name,
        "canonicalFuel": normalize_fuel_name(source_fuel_name),
        "capacityLiters": _as_float(_pick(row, "capacityLiters", "capacity_liters", "oil_tn"), "capacityLiters"),
        "volumeLiters": _as_float(_pick(row, "volumeLiters", "volume_liters", "fact_volume"), "volumeLiters"),
        "deadRestLiters": _as_float(_pick(row, "deadRestLiters", "dead_rest_liters", "dead_rest"), "deadRestLiters"),
        "sourceTimestamp": _pick(row, "sourceTimestamp", "source_timestamp", "dt_ins"),
        "tankId": str(_pick(row, "numStor", "num_stor", "tank_id") or "").strip(),
        "dedupStationKey": str(_pick(row, "ent_name_crc", "station_crc", "station_key") or ksss).strip(),
    }


def _latest_row(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_ts = normalize_source_timestamp(left.get("sourceTimestamp")) or ""
    right_ts = normalize_source_timestamp(right.get("sourceTimestamp")) or ""
    return right if right_ts >= left_ts else left


def aggregate_tank_rows(
    rows: list[Mapping[str, Any]],
    volume_multiplier: float = 1.0,
    include_unmapped: bool = False,
) -> dict[str, Any]:
    if volume_multiplier <= 0:
        raise ValueError("volume_multiplier must be positive")

    diagnostics: dict[str, Any] = {
        "rawRows": len(rows),
        "normalizedRows": 0,
        "uniqueTankKeys": 0,
        "duplicateTankRows": 0,
        "skippedMissingKsss": 0,
        "skippedMissingTankId": 0,
        "skippedInvalidRows": 0,
        "skippedNonCanonicalFuel": 0,
        "skippedNonPositiveCapacity": 0,
        "sourceTimestamps": 0,
        "groups": 0,
        "stations": 0,
        "fuelGroups": {},
        "unmappedFuelNames": {},
    }

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows:
        try:
            normalized = normalize_tank_row(raw)
        except ValueError:
            diagnostics["skippedInvalidRows"] += 1
            continue
        if not normalized["ksss"]:
            diagnostics["skippedMissingKsss"] += 1
            continue
        if not normalized["tankId"]:
            diagnostics["skippedMissingTankId"] += 1
            continue
        dedup_key = (str(normalized["dedupStationKey"]), str(normalized["tankId"]))
        if dedup_key in deduped:
            diagnostics["duplicateTankRows"] += 1
            deduped[dedup_key] = _latest_row(deduped[dedup_key], normalized)
        else:
            deduped[dedup_key] = normalized

    filtered_rows = []
    unmapped_names: Counter[str] = Counter()
    for normalized in deduped.values():
        non_positive_capacity = float(normalized["capacityLiters"]) <= 0
        if non_positive_capacity:
            diagnostics["skippedNonPositiveCapacity"] += 1
        if normalized["canonicalFuel"] == UNMAPPED_FUEL and not include_unmapped:
            diagnostics["skippedNonCanonicalFuel"] += 1
            unmapped_names[str(normalized["sourceFuelName"])] += 1
            continue
        if non_positive_capacity:
            continue
        filtered_rows.append(normalized)

    normalized_rows = filtered_rows
    diagnostics["normalizedRows"] = len(normalized_rows)
    diagnostics["uniqueTankKeys"] = len(deduped)
    diagnostics["unmappedFuelNames"] = dict(unmapped_names.most_common())
    if not normalized_rows:
        return {"accountDate": "", "snapshotAt": "", "records": [], "diagnostics": diagnostics}

    account_dates = {str(row["accountDate"]) for row in normalized_rows}
    if len(account_dates) != 1:
        raise ValueError("fuel stock rows must belong to one accountDate")

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in normalized_rows:
        key = (str(row["ksss"]), str(row["canonicalFuel"]))
        group = groups.setdefault(
            key,
            {
                "ksss": row["ksss"],
                "canonicalFuel": row["canonicalFuel"],
                "capacityLiters": 0.0,
                "volumeLiters": 0.0,
                "deadRestLiters": 0.0,
                "availableLiters": 0.0,
                "tanksCount": 0,
                "sourceNames": Counter(),
            },
        )
        capacity = float(row["capacityLiters"]) * volume_multiplier
        volume = float(row["volumeLiters"]) * volume_multiplier
        dead_rest = float(row["deadRestLiters"]) * volume_multiplier
        group["capacityLiters"] += capacity
        group["volumeLiters"] += volume
        group["deadRestLiters"] += dead_rest
        group["availableLiters"] += max(volume - dead_rest, 0)
        group["tanksCount"] += 1
        group["sourceNames"][str(row["sourceFuelName"])] += 1

    records = []
    for group in groups.values():
        capacity = float(group["capacityLiters"])
        available = float(group["availableLiters"])
        percent = (available / capacity) * 100 if capacity else 0
        source_counts = dict(sorted(group["sourceNames"].items()))
        records.append(
            {
                "ksss": str(group["ksss"]),
                "canonicalFuel": str(group["canonicalFuel"]),
                "capacityLiters": round(capacity, 4),
                "volumeLiters": round(float(group["volumeLiters"]), 4),
                "deadRestLiters": round(float(group["deadRestLiters"]), 4),
                "availableLiters": round(available, 4),
                "fillPercent": round(percent, 4),
                "tanksCount": int(group["tanksCount"]),
                "sourceFuelNames": sorted(source_counts),
                "sourceFuelNameCounts": source_counts,
            }
        )

    records.sort(key=lambda item: (item["ksss"], FUEL_SORT_ORDER.get(str(item["canonicalFuel"]), 99), str(item["canonicalFuel"])))
    timestamps = [normalize_source_timestamp(row.get("sourceTimestamp")) for row in normalized_rows]
    timestamps = [timestamp for timestamp in timestamps if timestamp]
    diagnostics["sourceTimestamps"] = len(timestamps)
    diagnostics["groups"] = len(records)
    diagnostics["stations"] = len({str(item["ksss"]) for item in records})
    diagnostics["fuelGroups"] = dict(Counter(str(item["canonicalFuel"]) for item in records))
    return {
        "accountDate": next(iter(account_dates)),
        "snapshotAt": max(timestamps) if timestamps else "",
        "records": records,
        "diagnostics": diagnostics,
    }
