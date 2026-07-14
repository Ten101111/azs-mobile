#!/usr/bin/env python3
"""Sync aggregated DWH KPI rows to the site API.

Run this script only from a corporate machine after the CheckPoint VPN is
connected manually. It sends daily station-level aggregates, not raw checks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - local convenience
    load_dotenv = None

try:
    import psycopg2
    import psycopg2.extras
except ImportError as exc:  # pragma: no cover - dependency guidance
    raise SystemExit("Install backend requirements first: pip install -r backend/requirements.txt") from exc


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SQL_PATH = PROJECT_DIR / "backend" / "sql" / "dwh_kpi_daily_export.sql"
DEFAULT_MIN_DATE = "2024-01-01"


def load_env() -> None:
    if not load_dotenv:
        return
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(PROJECT_DIR / ".env.local", override=True)


def current_period() -> str:
    now = date.today()
    return f"{now.year}-{now.month:02d}"


def period_bounds(period: str) -> tuple[date, date]:
    start = datetime.strptime(period, "%Y-%m").date().replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def shift_period(period: str, months: int) -> str:
    start = datetime.strptime(period, "%Y-%m").date().replace(day=1)
    month_index = start.year * 12 + (start.month - 1) + months
    year = month_index // 12
    month = (month_index % 12) + 1
    return f"{year}-{month:02d}"


def iter_periods(start_period: str, end_period: str) -> list[str]:
    start = datetime.strptime(start_period, "%Y-%m").date().replace(day=1)
    end = datetime.strptime(end_period, "%Y-%m").date().replace(day=1)
    if start > end:
        raise SystemExit("--from-period must be earlier than or equal to --to-period")
    periods = []
    period = start_period
    while True:
        periods.append(period)
        if period == end_period:
            return periods
        period = shift_period(period, 1)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def require_env(names: list[str]) -> dict[str, str]:
    values = {name: env(name) for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    return values


def api_url_from_env() -> str:
    explicit = env("KPI_IMPORT_URL")
    if explicit:
        return explicit
    public_url = env("APP_PUBLIC_URL", "http://localhost:5174").rstrip("/")
    return f"{public_url}/api/internal/kpi/import"


def read_sql(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"SQL template not found: {path}")
    sql = path.read_text(encoding="utf-8")
    if "TODO_" in sql:
        raise SystemExit(f"Fill all TODO_* DWH table and column names in {path} before running sync.")
    return sql


def connect_dwh():
    values = require_env(["DWH_DB_HOST", "DWH_DB_NAME", "DWH_DB_USER", "DWH_DB_PASSWORD"])
    return psycopg2.connect(
        host=values["DWH_DB_HOST"],
        port=env("DWH_DB_PORT", "5432"),
        dbname=values["DWH_DB_NAME"],
        user=values["DWH_DB_USER"],
        password=values["DWH_DB_PASSWORD"],
        connect_timeout=int(env("DWH_CONNECT_TIMEOUT_SECONDS", "10")),
        options=f"-c statement_timeout={int(env('DWH_STATEMENT_TIMEOUT_MS', '3600000'))}",
    )


def pick(row: dict, *names: str):
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return None


def iso_date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty date")
    return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()


def as_float(value, field: str) -> float:
    if value is None or value == "":
        raise ValueError(f"empty {field}")
    return float(value)


def normalize_records(rows: list[dict]) -> list[dict]:
    normalized = []
    for index, row in enumerate(rows, start=1):
        try:
            metric_date = iso_date(pick(row, "date", "metric_date", "day", "report_date"))
            ksss = str(pick(row, "ksss", "station_id", "station", "azs_id") or "").strip()
            if not ksss:
                raise ValueError("empty ksss")
            record = {
                "date": metric_date,
                "ksss": ksss,
                "revenue": as_float(pick(row, "revenue", "revenue_gross", "revenue_total"), "revenue"),
                "fuelVolume": as_float(
                    pick(row, "fuelVolume", "fuel_volume", "fuel_volume_liters", "volume_liters"),
                    "fuelVolume",
                ),
                "checks": as_float(pick(row, "checks", "checks_count", "check_count", "receipts"), "checks"),
                "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            optional_metrics = (
                ("revenueNtu", ("revenueNtu", "revenue_ntu")),
                ("checksNtu", ("checksNtu", "checks_ntu")),
                ("avgCheck", ("avgCheck", "avg_check")),
            )
            for target, source_names in optional_metrics:
                value = pick(row, *source_names)
                if value is not None:
                    record[target] = as_float(value, target)
            normalized.append(record)
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"Cannot normalize DWH row #{index}: {exc}. Row keys: {sorted(row.keys())}") from exc
    return normalized


def fetch_dwh_rows(sql: str, period: str, min_date: date, limit: int | None) -> list[dict]:
    period_start, period_end = period_bounds(period)
    if period_end <= min_date:
        return []
    params = {
        "period": period,
        "period_start": max(period_start, min_date),
        "period_end": period_end,
        "min_date": min_date,
    }
    with connect_dwh() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(sql, params)
            rows = [dict(row) for row in cursor.fetchall()]
    return rows[:limit] if limit else rows


def post_records(api_url: str, token: str, period: str, records: list[dict], replace_period: bool, chunk_size: int) -> int:
    imported = 0
    for offset in range(0, len(records), chunk_size):
        chunk = records[offset : offset + chunk_size]
        payload = {
            "source": env("KPI_IMPORT_SOURCE", "dwh-sync"),
            "period": period,
            "replacePeriod": replace_period and offset == 0,
            "records": chunk,
        }
        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=int(env("KPI_IMPORT_TIMEOUT_SECONDS", "60"))) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SystemExit(f"Import API failed with HTTP {exc.code}: {detail}") from exc
        imported += int(body.get("imported") or len(chunk))
    return imported


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync aggregated DWH KPI rows to the site.")
    parser.add_argument("--period", default=current_period(), help="Month to sync in YYYY-MM format.")
    parser.add_argument("--from-period", default="", help="Backfill from month YYYY-MM. Overrides --period.")
    parser.add_argument("--to-period", default="", help="Backfill through month YYYY-MM. Defaults to current month.")
    parser.add_argument("--min-date", default=env("DWH_MIN_DATE", DEFAULT_MIN_DATE), help="Hard lower bound date, default 2024-01-01.")
    parser.add_argument("--sql", default=str(DEFAULT_SQL_PATH), help="Path to KPI export SQL.")
    parser.add_argument("--api-url", default=api_url_from_env(), help="Import API URL.")
    parser.add_argument("--token", default=env("KPI_IMPORT_TOKEN"), help="Import API bearer token.")
    parser.add_argument("--chunk-size", type=int, default=int(env("KPI_IMPORT_CHUNK_SIZE", "3000")))
    parser.add_argument("--limit", type=int, default=0, help="Read only first N rows for testing.")
    parser.add_argument("--dry-run", action="store_true", help="Read and normalize data, but do not upload.")
    parser.add_argument("--no-replace-period", action="store_true", help="Do not delete existing rows for the period first.")
    return parser.parse_args()


def main() -> int:
    load_env()
    args = parse_args()
    if args.chunk_size <= 0:
        raise SystemExit("--chunk-size must be positive")

    min_date = parse_date(args.min_date)
    min_period = f"{min_date.year}-{min_date.month:02d}"
    periods = iter_periods(args.from_period, args.to_period or current_period()) if args.from_period else [args.period]
    periods = [period for period in periods if period >= min_period]
    if not periods:
        print(f"No periods to sync: all requested periods are earlier than {args.min_date}.")
        return 0

    sql = read_sql(Path(args.sql))
    total_imported = 0
    for period in periods:
        rows = fetch_dwh_rows(sql, period, min_date, args.limit or None)
        records = normalize_records(rows)

        print(f"Prepared {len(records)} aggregated KPI rows for {period}.")
        if records:
            print(json.dumps(records[:3], ensure_ascii=False, indent=2))

        if args.dry_run:
            continue

        if not args.token:
            raise SystemExit("KPI_IMPORT_TOKEN is required for upload.")

        imported = post_records(
            api_url=args.api_url,
            token=args.token,
            period=period,
            records=records,
            replace_period=not args.no_replace_period,
            chunk_size=args.chunk_size,
        )
        total_imported += imported
        print(f"Imported {imported} rows for {period} to {args.api_url}")

    if args.dry_run:
        print("Dry run: import API was not called.")
    else:
        print(f"Imported {total_imported} rows total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
