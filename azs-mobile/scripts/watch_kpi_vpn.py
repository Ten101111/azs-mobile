#!/usr/bin/env python3
"""Catch up the current KPI month after the corporate VPN becomes available."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - local convenience
    load_dotenv = None


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CATCHUP_AFTER_SECONDS = 4 * 60 * 60 + 15 * 60
DEFAULT_VPN_PROBE_TIMEOUT_SECONDS = 3
DEFAULT_SERVER_HEALTH_TIMEOUT_SECONDS = 10


def load_env() -> None:
    if load_dotenv:
        load_dotenv(PROJECT_DIR / ".env")
        load_dotenv(PROJECT_DIR / ".env.local", override=True)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(env(name, str(default))))
    except ValueError:
        return default


def health_url_from_import_url(import_url: str) -> str:
    parsed = urlsplit(import_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("KPI_IMPORT_URL must be an absolute URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/health", "", ""))


def parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def server_kpi_is_recent(payload: dict[str, Any], catchup_after_seconds: int, now: datetime) -> bool:
    imported_at = parse_iso_datetime(payload.get("kpiUpdatedAt"))
    if not imported_at:
        return False
    return (now.astimezone(timezone.utc) - imported_at).total_seconds() < catchup_after_seconds


def read_server_health(url: str, timeout_seconds: int) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def dwh_is_reachable(host: str, port: int, timeout_seconds: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Catch up KPI import after VPN connection")
    parser.add_argument("--force", action="store_true", help="Run even when the server KPI import is recent")
    return parser.parse_args()


def main() -> int:
    load_env()
    args = parse_args()
    import_url = env("KPI_IMPORT_URL")
    dwh_host = env("DWH_DB_HOST")
    if not import_url or not dwh_host:
        raise SystemExit("KPI_IMPORT_URL and DWH_DB_HOST are required")

    catchup_after_seconds = positive_int("KPI_CATCHUP_AFTER_SECONDS", DEFAULT_CATCHUP_AFTER_SECONDS)
    probe_timeout_seconds = positive_int("KPI_VPN_PROBE_TIMEOUT_SECONDS", DEFAULT_VPN_PROBE_TIMEOUT_SECONDS)
    health_timeout_seconds = positive_int("KPI_SERVER_HEALTH_TIMEOUT_SECONDS", DEFAULT_SERVER_HEALTH_TIMEOUT_SECONDS)
    health = read_server_health(health_url_from_import_url(import_url), health_timeout_seconds)
    if health is None:
        return 0
    if not args.force and server_kpi_is_recent(health, catchup_after_seconds, datetime.now(timezone.utc)):
        return 0

    if not dwh_is_reachable(dwh_host, positive_int("DWH_DB_PORT", 5432), probe_timeout_seconds):
        return 0

    print("DWH is reachable and KPI data needs catch-up; starting current-month import.", flush=True)
    return subprocess.run([sys.executable, str(PROJECT_DIR / "scripts" / "sync_kpi_metrics.py")], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
