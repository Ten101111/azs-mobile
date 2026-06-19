import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

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
SQL_TEMPLATE = APP_DIR / "sql" / "station_kpis.sql"


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


app = FastAPI(title="AZS KPI API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def current_period() -> str:
    return datetime.now().strftime("%Y-%m")


def validate_period(period: str) -> str:
    try:
        datetime.strptime(period, "%Y-%m")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="period must use YYYY-MM format") from exc
    return period


def mock_number(ksss: str, period: str, metric_id: str, minimum: int, maximum: int) -> int:
    seed = f"{ksss}:{period}:{metric_id}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    value = int(digest[:10], 16)
    return minimum + value % (maximum - minimum + 1)


def mock_pct(ksss: str, period: str, metric_id: str, salt: str) -> float:
    raw = mock_number(ksss, period, f"{metric_id}:{salt}", -120, 180)
    return round(raw / 10, 1)


def mock_kpis(ksss: str, period: str) -> StationKpiResponse:
    revenue = mock_number(ksss, period, "revenue", 4_000_000, 28_000_000)
    fuel_volume = mock_number(ksss, period, "fuelVolume", 120_000, 850_000)
    checks = mock_number(ksss, period, "checks", 8_000, 62_000)
    avg_check = round(revenue / max(checks, 1))

    metrics = [
        ("revenue", "Выручка", revenue, "₽"),
        ("fuelVolume", "Объем топлива", fuel_volume, "л"),
        ("checks", "Чеки", checks, "шт"),
        ("avgCheck", "Средний чек", avg_check, "₽"),
    ]

    return StationKpiResponse(
        ksss=ksss,
        period=period,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        metrics=[
            KpiMetric(
                id=metric_id,
                label=label,
                value=value,
                unit=unit,
                momPct=mock_pct(ksss, period, metric_id, "mom"),
                yoyPct=mock_pct(ksss, period, metric_id, "yoy"),
            )
            for metric_id, label, value, unit in metrics
        ],
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

    period_start = datetime.strptime(period, "%Y-%m").date().replace(day=1)
    if period_start.month == 12:
        period_end = period_start.replace(year=period_start.year + 1, month=1)
    else:
        period_end = period_start.replace(month=period_start.month + 1)

    if period_start.month == 1:
        previous_period_start = period_start.replace(year=period_start.year - 1, month=12)
    else:
        previous_period_start = period_start.replace(month=period_start.month - 1)
    previous_year_start = period_start.replace(year=period_start.year - 1)

    params = {
        "ksss": ksss,
        "period_start": period_start,
        "period_end": period_end,
        "previous_period_start": previous_period_start,
        "previous_year_start": previous_year_start,
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


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": os.getenv("KPI_DATA_MODE", "mock")}


@app.get("/api/stations/{ksss}/kpis", response_model=StationKpiResponse)
def station_kpis(ksss: str, period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$")):
    period = validate_period(period)
    mode = os.getenv("KPI_DATA_MODE", "mock").lower()

    if mode == "mock":
        return mock_kpis(ksss, period)
    if mode == "db":
        return db_kpis(ksss, period)

    raise HTTPException(status_code=500, detail=f"Unsupported KPI_DATA_MODE: {mode}")
