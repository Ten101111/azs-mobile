#!/usr/bin/env python3
"""Upload the current DWH fuel-stock snapshot as station-level aggregates.

Run this script only on the corporate Mac after CheckPoint VPN is connected.
Raw tank rows stay on that machine; the HTTPS payload contains one aggregate
per station and canonical fuel group.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - local dependency guidance
    load_dotenv = None

try:
    import psycopg2
    import psycopg2.extras
except ImportError as exc:  # pragma: no cover - local dependency guidance
    raise SystemExit("Install backend requirements first: pip install -r backend/requirements.txt") from exc


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from backend.fuel_stock import aggregate_tank_rows  # noqa: E402


DEFAULT_SQL_PATH = PROJECT_DIR / "backend" / "sql" / "dwh_fuel_stock_current.sql"


def load_env() -> None:
    if not load_dotenv:
        return
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(PROJECT_DIR / ".env.local", override=True)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def require_env(names: list[str]) -> dict[str, str]:
    values = {name: env(name) for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    return values


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD format") from exc


def api_url_from_env() -> str:
    explicit = env("FUEL_STOCK_IMPORT_URL")
    if explicit:
        return explicit
    public_url = env("APP_PUBLIC_URL", "http://localhost:5174").rstrip("/")
    return f"{public_url}/api/internal/fuel-stock/import"


def read_sql(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"SQL template not found: {path}")
    sql = path.read_text(encoding="utf-8")
    if "TODO_" in sql:
        raise SystemExit(f"Fill all TODO_* names in {path} before running sync")
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
        options=f"-c statement_timeout={int(env('DWH_FUEL_STOCK_STATEMENT_TIMEOUT_MS', '180000'))}",
    )


def fetch_dwh_rows(sql: str, account_date: date, limit: int | None = None) -> list[dict]:
    start = datetime.combine(account_date, datetime_time.min)
    end = start + timedelta(days=1)
    params = {"account_date_start": start, "account_date_end": end}
    with connect_dwh() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(sql, params)
            rows = [dict(row) for row in cursor.fetchall()]
    return rows[:limit] if limit else rows


def retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After", "").strip()
    if retry_after.isdigit():
        return min(300.0, max(1.0, float(retry_after)))
    return min(60.0, float(2**attempt))


def post_snapshot(api_url: str, token: str, payload: dict, timeout: int, retries: int) -> tuple[dict, int]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8")), len(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt >= retries:
                raise SystemExit(f"Fuel stock import failed with HTTP {exc.code}: {detail}") from exc
            delay = retry_delay(exc, attempt)
            print(f"Import API returned HTTP {exc.code}; retrying in {delay:.0f}s.", flush=True)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= retries:
                reason = getattr(exc, "reason", str(exc))
                raise SystemExit(f"Fuel stock import connection failed: {reason}") from exc
            delay = min(60.0, float(2**attempt))
            print(f"Import API is unavailable; retrying in {delay:.0f}s.", flush=True)
            time.sleep(delay)

    raise SystemExit("Fuel stock import failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync the current aggregated fuel-stock snapshot to the site")
    parser.add_argument("--date", type=parse_date, default=date.today(), help="DWH account date, default today")
    parser.add_argument("--sql", default=str(DEFAULT_SQL_PATH), help="Path to the DWH export SQL")
    parser.add_argument("--api-url", default=api_url_from_env(), help="Fuel-stock import API URL")
    parser.add_argument("--token", default=env("FUEL_STOCK_IMPORT_TOKEN"), help="Import API bearer token")
    parser.add_argument("--source", default=env("FUEL_STOCK_IMPORT_SOURCE", "dwh-fuel-stock-sync"))
    parser.add_argument(
        "--volume-multiplier",
        type=float,
        default=float(env("DWH_FUEL_TONS_TO_STORAGE_UNITS", env("DWH_FUEL_VOLUME_TO_LITERS", "1000"))),
        help="Conversion from DWH tonnes to legacy API storage units",
    )
    parser.add_argument(
        "--min-stations",
        type=int,
        default=int(env("FUEL_STOCK_MIN_LOCAL_STATIONS", "1000")),
        help="Reject unexpectedly small snapshots before upload",
    )
    parser.add_argument("--limit", type=int, default=0, help="Read only the first N raw rows for diagnostics")
    parser.add_argument("--dry-run", action="store_true", help="Read and aggregate DWH data without uploading")
    parser.add_argument(
        "--allow-small-snapshot",
        action="store_true",
        help="Bypass the local minimum station guard for deliberate tests",
    )
    parser.add_argument("--unmapped-limit", type=int, default=20, help="Number of unmapped source names to print")
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(env("FUEL_STOCK_IMPORT_TIMEOUT_SECONDS", "120")),
        help="Import API timeout in seconds",
    )
    parser.add_argument("--retries", type=int, default=int(env("FUEL_STOCK_IMPORT_RETRIES", "4")))
    return parser.parse_args()


def main() -> int:
    load_env()
    args = parse_args()
    if args.volume_multiplier <= 0:
        raise SystemExit("--volume-multiplier must be positive")
    if args.min_stations < 1:
        raise SystemExit("--min-stations must be positive")
    if args.limit < 0 or args.timeout <= 0 or args.retries < 0:
        raise SystemExit("--limit, --timeout and --retries must be non-negative")
    if args.limit and not args.dry_run and not args.allow_small_snapshot:
        raise SystemExit("Refusing a limited upload; use --dry-run or explicitly add --allow-small-snapshot")

    sql = read_sql(Path(args.sql))
    rows = fetch_dwh_rows(sql, args.date, args.limit or None)
    snapshot = aggregate_tank_rows(rows, volume_multiplier=args.volume_multiplier)
    records = snapshot["records"]
    diagnostics = snapshot["diagnostics"]

    print(
        "Fuel stock prepared: "
        f"raw={diagnostics['rawRows']}, groups={diagnostics['groups']}, "
        f"stations={diagnostics['stations']}, snapshotAt={snapshot['snapshotAt'] or 'missing'}"
    )
    print(f"Canonical groups: {json.dumps(diagnostics['fuelGroups'], ensure_ascii=False, sort_keys=True)}")
    print(
        "Skipped rows: "
        f"no-ksss={diagnostics['skippedMissingKsss']}, "
        f"no-tank={diagnostics['skippedMissingTankId']}, "
        f"invalid={diagnostics['skippedInvalidRows']}, "
        f"non-canonical={diagnostics['skippedNonCanonicalFuel']}, "
        f"non-positive-capacity={diagnostics['skippedNonPositiveCapacity']}"
    )
    print(
        "Stock states: "
        f"capacity-exceeded={diagnostics['capacityExceededGroups']}, "
        f"dead-stock={diagnostics['deadStockGroups']}"
    )
    unmapped = list(diagnostics.get("unmappedFuelNames", {}).items())[: max(0, args.unmapped_limit)]
    if unmapped:
        print("Top unmapped fuel names:")
        for name, count in unmapped:
            print(f"  {name}: {count}")

    if not records:
        raise SystemExit("Refusing to upload an empty fuel-stock snapshot")
    if not snapshot["snapshotAt"]:
        raise SystemExit("Refusing to upload a snapshot without a DWH source timestamp")
    if diagnostics["stations"] < args.min_stations and not args.allow_small_snapshot:
        raise SystemExit(
            f"Refusing small snapshot with {diagnostics['stations']} stations; "
            f"minimum is {args.min_stations}"
        )

    payload = {
        "source": args.source,
        "accountDate": snapshot["accountDate"],
        "snapshotAt": snapshot["snapshotAt"],
        "records": records,
    }
    body_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    max_body_size = int(env("FUEL_STOCK_IMPORT_MAX_BODY_BYTES", str(5 * 1024 * 1024)))
    if body_size > max_body_size:
        raise SystemExit(f"Fuel-stock payload is {body_size} bytes; configured maximum is {max_body_size}")

    if args.dry_run:
        print(f"Dry run: API was not called; payload size={body_size} bytes")
        return 0
    if not args.token:
        raise SystemExit("FUEL_STOCK_IMPORT_TOKEN is required for upload")

    response, uploaded_bytes = post_snapshot(args.api_url, args.token, payload, args.timeout, args.retries)
    print(
        f"Fuel stock import complete: imported={response.get('imported', 0)}, "
        f"stations={response.get('stations', 0)}, unchanged={bool(response.get('unchanged'))}, "
        f"payload={uploaded_bytes} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
