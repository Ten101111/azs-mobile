import json
import hashlib
import os
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience
    load_dotenv = None

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

if load_dotenv:
    load_dotenv()

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
STATIONS_PATH = PROJECT_DIR / "public" / "stations.json"
STATIONS_SAMPLE_PATH = PROJECT_DIR / "public" / "stations.sample.json"
SQL_TEMPLATE = APP_DIR / "sql" / "station_kpis.sql"
STAFF_SQL_TEMPLATE = APP_DIR / "sql" / "station_staff.sql"
ANALYTICS_SQL_TEMPLATE = APP_DIR / "sql" / "analytics_overview.sql"
SIMILAR_SQL_TEMPLATE = APP_DIR / "sql" / "station_similar.sql"
COMPARE_SQL_TEMPLATE = APP_DIR / "sql" / "analytics_compare.sql"


class KpiMetric(BaseModel):
    id: str
    label: str
    value: float
    unit: str
    momPct: float
    yoyPct: float


class StationKpiResponse(BaseModel):
    ksss: str
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: str
    updatedAt: str
    metrics: list[KpiMetric]


class StaffDay(BaseModel):
    date: str
    label: str
    day: int
    night: int


class StationStaffResponse(BaseModel):
    ksss: str
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: str
    updatedAt: str
    staffTotal: int
    today: StaffDay
    days: list[StaffDay]


class AnalyticsOverviewRow(BaseModel):
    id: str
    label: str
    count: int
    metrics: list[KpiMetric]


class AnalyticsOverviewResponse(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    groupBy: str
    source: str
    updatedAt: str
    rows: list[AnalyticsOverviewRow]


class SimilarStation(BaseModel):
    ksss: str
    stationNumber: str
    name: str
    subject: str
    score: int
    reasons: list[str]
    metrics: list[KpiMetric]


class SimilarStationsResponse(BaseModel):
    ksss: str
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: str
    updatedAt: str
    items: list[SimilarStation]


class CompareStation(BaseModel):
    ksss: str
    stationNumber: str
    name: str
    subject: str
    regionalManager: str
    territoryManager: str
    format: str
    location: str
    trkCount: Optional[float] = None
    postsCount: Optional[float] = None
    staffTotal: int
    metrics: list[KpiMetric]


class CompareResponse(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: str
    updatedAt: str
    items: list[CompareStation]


app = FastAPI(title="AZS KPI API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def current_period() -> str:
    return datetime.now().strftime("%Y-%m")


def data_mode() -> str:
    return os.getenv("APP_DATA_MODE") or os.getenv("KPI_DATA_MODE", "mock")


def validate_period(period: str) -> str:
    try:
        datetime.strptime(period, "%Y-%m")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="period must use YYYY-MM format") from exc
    return period


def period_bounds(period: str) -> dict[str, date]:
    period_start = datetime.strptime(period, "%Y-%m").date().replace(day=1)
    if period_start.month == 12:
        period_end = period_start.replace(year=period_start.year + 1, month=1)
    else:
        period_end = period_start.replace(month=period_start.month + 1)

    if period_start.month == 1:
        previous_period_start = period_start.replace(year=period_start.year - 1, month=12)
    else:
        previous_period_start = period_start.replace(month=period_start.month - 1)

    return {
        "period_start": period_start,
        "period_end": period_end,
        "previous_period_start": previous_period_start,
        "previous_year_start": period_start.replace(year=period_start.year - 1),
    }


@lru_cache(maxsize=1)
def load_stations() -> list[dict]:
    path = STATIONS_PATH if STATIONS_PATH.exists() else STATIONS_SAMPLE_PATH
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("stations", [])


def station_by_ksss(ksss: str) -> Optional[dict]:
    return next((station for station in load_stations() if str(station.get("ksss")) == str(ksss)), None)


def station_name(station: dict) -> str:
    return station.get("name") or f"АЗС № {station.get('stationNumber', '')}".strip()


def mock_number(ksss: str, period: str, metric_id: str, minimum: int, maximum: int) -> int:
    seed = f"{ksss}:{period}:{metric_id}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    value = int(digest[:10], 16)
    return minimum + value % (maximum - minimum + 1)


def mock_pct(ksss: str, period: str, metric_id: str, salt: str) -> float:
    raw = mock_number(ksss, period, f"{metric_id}:{salt}", -120, 180)
    return round(raw / 10, 1)


def mock_metric_values(ksss: str, period: str) -> dict[str, float]:
    revenue = mock_number(ksss, period, "revenue", 4_000_000, 28_000_000)
    fuel_volume = mock_number(ksss, period, "fuelVolume", 120_000, 850_000)
    checks = mock_number(ksss, period, "checks", 8_000, 62_000)
    avg_check = round(revenue / max(checks, 1))
    return {
        "revenue": revenue,
        "fuelVolume": fuel_volume,
        "checks": checks,
        "avgCheck": avg_check,
    }


def make_metrics(values: dict[str, float], period: str, seed_key: str) -> list[KpiMetric]:
    metrics = [
        ("revenue", "Выручка", values.get("revenue", 0), "₽"),
        ("fuelVolume", "Объем топлива", values.get("fuelVolume", 0), "л"),
        ("checks", "Чеки", values.get("checks", 0), "шт"),
        ("avgCheck", "Средний чек", values.get("avgCheck", 0), "₽"),
    ]
    return [
        KpiMetric(
            id=metric_id,
            label=label,
            value=value,
            unit=unit,
            momPct=mock_pct(seed_key, period, metric_id, "mom"),
            yoyPct=mock_pct(seed_key, period, metric_id, "yoy"),
        )
        for metric_id, label, value, unit in metrics
    ]


def mock_kpis(ksss: str, period: str) -> StationKpiResponse:
    return StationKpiResponse(
        ksss=ksss,
        period=period,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        metrics=make_metrics(mock_metric_values(ksss, period), period, ksss),
    )


def db_kpis(ksss: str, period: str) -> StationKpiResponse:
    try:
        import pandas as pd
        import psycopg2
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Install backend requirements to use KPI_DATA_MODE=db") from exc

    required = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing DB env vars: {', '.join(missing)}")

    if not SQL_TEMPLATE.exists():
        raise HTTPException(status_code=500, detail="SQL template not found")

    params = {
        "ksss": ksss,
        **period_bounds(period),
    }

    with psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        connect_timeout=10,
        options="-c statement_timeout=3600000",
    ) as conn:
        df = pd.read_sql(SQL_TEMPLATE.read_text(encoding="utf-8"), conn, params=params)

    if df.empty:
        raise HTTPException(status_code=404, detail="KPI data not found")

    row = df.iloc[0].to_dict()
    return StationKpiResponse(
        ksss=ksss,
        period=period,
        source="db",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        metrics=[
            KpiMetric(id="revenue", label="Выручка", value=float(row.get("revenue") or 0), unit="₽", momPct=float(row.get("revenue_mom_pct") or 0), yoyPct=float(row.get("revenue_yoy_pct") or 0)),
            KpiMetric(id="fuelVolume", label="Объем топлива", value=float(row.get("fuel_volume") or 0), unit="л", momPct=float(row.get("fuel_volume_mom_pct") or 0), yoyPct=float(row.get("fuel_volume_yoy_pct") or 0)),
            KpiMetric(id="checks", label="Чеки", value=float(row.get("checks") or 0), unit="шт", momPct=float(row.get("checks_mom_pct") or 0), yoyPct=float(row.get("checks_yoy_pct") or 0)),
            KpiMetric(id="avgCheck", label="Средний чек", value=float(row.get("avg_check") or 0), unit="₽", momPct=float(row.get("avg_check_mom_pct") or 0), yoyPct=float(row.get("avg_check_yoy_pct") or 0)),
        ],
    )


def mock_staff_total(ksss: str, period: str) -> int:
    return mock_number(ksss, period, "staffTotal", 9, 24)


def make_staff_day(ksss: str, day_date: date, staff_total: int) -> StaffDay:
    seed_period = day_date.strftime("%Y-%m")
    weekday = day_date.weekday()
    day_min = max(2, round(staff_total * 0.34))
    day_max = max(day_min, round(staff_total * 0.58))
    night_min = max(1, round(staff_total * 0.18))
    night_max = max(night_min, round(staff_total * 0.34))
    day_value = mock_number(ksss, seed_period, f"staffDay:{day_date.day}", day_min, day_max)
    night_value = mock_number(ksss, seed_period, f"staffNight:{day_date.day}", night_min, night_max)

    if weekday >= 5:
        day_value = max(day_min, day_value - 1)

    label = day_date.strftime("%a").replace(".", "")
    return StaffDay(
        date=day_date.isoformat(),
        label=label,
        day=day_value,
        night=night_value,
    )


def mock_staff(ksss: str, period: str) -> StationStaffResponse:
    bounds = period_bounds(period)
    period_start = bounds["period_start"]
    days_in_month = monthrange(period_start.year, period_start.month)[1]
    staff_total = mock_staff_total(ksss, period)
    days = [
        make_staff_day(ksss, period_start + timedelta(days=offset), staff_total)
        for offset in range(days_in_month)
    ]
    today_iso = datetime.now().date().isoformat()
    today = next((item for item in days if item.date == today_iso), days[0])

    return StationStaffResponse(
        ksss=ksss,
        period=period,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        staffTotal=staff_total,
        today=today,
        days=days,
    )


def aggregate_metric_values(ksss_values: list[str], period: str) -> dict[str, float]:
    rows = [mock_metric_values(ksss, period) for ksss in ksss_values]
    if not rows:
        return {"revenue": 0, "fuelVolume": 0, "checks": 0, "avgCheck": 0}
    revenue = sum(row["revenue"] for row in rows)
    fuel_volume = sum(row["fuelVolume"] for row in rows)
    checks = sum(row["checks"] for row in rows)
    return {
        "revenue": revenue,
        "fuelVolume": fuel_volume,
        "checks": checks,
        "avgCheck": round(revenue / max(checks, 1)),
    }


def mock_overview(period: str, group_by: str) -> AnalyticsOverviewResponse:
    getter_map = {
        "territoryManager": lambda station: station.get("territoryManager") or "ТМ не заполнен",
        "regionalManager": lambda station: station.get("regionalManager") or "РУ не заполнен",
        "station": lambda station: station_name(station),
    }
    if group_by not in getter_map:
        raise HTTPException(status_code=422, detail="groupBy must be territoryManager, regionalManager or station")

    groups: dict[str, list[dict]] = {}
    for station in load_stations():
        if not station.get("ksss"):
            continue
        label = getter_map[group_by](station)
        groups.setdefault(label, []).append(station)

    rows = []
    for label, stations in groups.items():
        ksss_values = [str(station.get("ksss")) for station in stations if station.get("ksss")]
        values = aggregate_metric_values(ksss_values, period)
        rows.append(
            AnalyticsOverviewRow(
                id=label,
                label=label,
                count=len(stations),
                metrics=make_metrics(values, period, f"{group_by}:{label}"),
            )
        )

    rows.sort(key=lambda row: next((metric.value for metric in row.metrics if metric.id == "revenue"), 0), reverse=True)
    return AnalyticsOverviewResponse(
        period=period,
        groupBy=group_by,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        rows=rows[:30],
    )


def station_similarity(base: dict, candidate: dict, period: str) -> tuple[int, list[str]]:
    score = 42
    reasons = []

    for field, label, points in [
        ("formatLevel2", "формат", 14),
        ("location", "локация", 12),
        ("subject", "регион", 8),
        ("serviceCluster", "сервисный кластер", 8),
        ("paymentType", "тип оплаты", 6),
    ]:
        if base.get(field) and base.get(field) == candidate.get(field):
            score += points
            reasons.append(label)

    base_flags = base.get("flags", {})
    candidate_flags = candidate.get("flags", {})
    for flag, label in [("hasCafe", "кафе"), ("hasShop", "магазин"), ("hasToilet", "санузел")]:
        if base_flags.get(flag) and candidate_flags.get(flag):
            score += 4
            reasons.append(label)

    base_values = mock_metric_values(str(base.get("ksss")), period)
    candidate_values = mock_metric_values(str(candidate.get("ksss")), period)
    revenue_delta = abs(base_values["revenue"] - candidate_values["revenue"]) / max(base_values["revenue"], 1)
    volume_delta = abs(base_values["fuelVolume"] - candidate_values["fuelVolume"]) / max(base_values["fuelVolume"], 1)

    if revenue_delta < 0.18:
        score += 10
        reasons.append("близкая выручка")
    if volume_delta < 0.18:
        score += 10
        reasons.append("близкий объем")

    return min(score, 100), reasons[:4] or ["экономический профиль"]


def mock_similar(ksss: str, period: str, limit: int) -> SimilarStationsResponse:
    base = station_by_ksss(ksss)
    if not base:
        raise HTTPException(status_code=404, detail="Station not found")

    items = []
    for candidate in load_stations():
        candidate_ksss = str(candidate.get("ksss") or "")
        if not candidate_ksss or candidate_ksss == str(ksss):
            continue
        score, reasons = station_similarity(base, candidate, period)
        items.append(
            SimilarStation(
                ksss=candidate_ksss,
                stationNumber=str(candidate.get("stationNumber") or ""),
                name=station_name(candidate),
                subject=str(candidate.get("subject") or candidate.get("address") or ""),
                score=score,
                reasons=reasons,
                metrics=make_metrics(mock_metric_values(candidate_ksss, period), period, candidate_ksss),
            )
        )

    items.sort(key=lambda item: item.score, reverse=True)
    return SimilarStationsResponse(
        ksss=ksss,
        period=period,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        items=items[:limit],
    )


def mock_compare(ksss_values: list[str], period: str) -> CompareResponse:
    unique_ksss = []
    for ksss in ksss_values:
        if ksss and ksss not in unique_ksss:
            unique_ksss.append(ksss)
    if len(unique_ksss) > 5:
        raise HTTPException(status_code=422, detail="Compare supports up to 5 stations")

    items = []
    for ksss in unique_ksss:
        station = station_by_ksss(ksss)
        if not station:
            continue
        items.append(
            CompareStation(
                ksss=ksss,
                stationNumber=str(station.get("stationNumber") or ""),
                name=station_name(station),
                subject=str(station.get("subject") or station.get("address") or ""),
                regionalManager=str(station.get("regionalManager") or ""),
                territoryManager=str(station.get("territoryManager") or ""),
                format=str(station.get("formatLevel2") or station.get("format") or ""),
                location=str(station.get("location") or ""),
                trkCount=station.get("trkCount"),
                postsCount=station.get("postsCount"),
                staffTotal=mock_staff_total(ksss, period),
                metrics=make_metrics(mock_metric_values(ksss, period), period, ksss),
            )
        )

    return CompareResponse(
        period=period,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        items=items,
    )


def db_extension_not_ready(template: Path):
    raise HTTPException(
        status_code=501,
        detail=f"DB mode is reserved for real SQL. Fill {template.name} after table structure is known.",
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": data_mode()}


@app.get("/api/stations/{ksss}/kpis", response_model=StationKpiResponse)
def station_kpis(ksss: str, period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$")):
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_kpis(ksss, period)
    if mode == "db":
        return db_kpis(ksss, period)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")


@app.get("/api/stations/{ksss}/staff", response_model=StationStaffResponse)
def station_staff(ksss: str, period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$")):
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_staff(ksss, period)
    if mode == "db":
        db_extension_not_ready(STAFF_SQL_TEMPLATE)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")


@app.get("/api/analytics/overview", response_model=AnalyticsOverviewResponse)
def analytics_overview(
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    groupBy: str = Query("territoryManager"),
):
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_overview(period, groupBy)
    if mode == "db":
        db_extension_not_ready(ANALYTICS_SQL_TEMPLATE)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")


@app.get("/api/stations/{ksss}/similar", response_model=SimilarStationsResponse)
def station_similar(
    ksss: str,
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=30),
):
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_similar(ksss, period, limit)
    if mode == "db":
        db_extension_not_ready(SIMILAR_SQL_TEMPLATE)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")


@app.get("/api/analytics/compare", response_model=CompareResponse)
def analytics_compare(
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    ksss: list[str] = Query(default_factory=list),
):
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_compare(ksss, period)
    if mode == "db":
        db_extension_not_ready(COMPARE_SQL_TEMPLATE)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")
