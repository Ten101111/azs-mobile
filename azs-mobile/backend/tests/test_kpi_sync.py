from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sync_kpi_metrics.py"
SPEC = importlib.util.spec_from_file_location("sync_kpi_metrics", SCRIPT_PATH)
assert SPEC and SPEC.loader
sync_kpi_metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_kpi_metrics)


class LocalKpiMirrorTests(unittest.TestCase):
    def test_uses_configured_application_database_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            configured_path = Path(temp_dir) / "application" / "kpi_metrics.sqlite3"
            with patch.dict("os.environ", {"KPI_LOCAL_DB_PATH": str(configured_path)}):
                self.assertEqual(sync_kpi_metrics.local_kpi_db_path(), configured_path)

    def test_replaces_only_synced_period_with_uploaded_records(self):
        records = [
            {
                "date": "2026-06-01",
                "ksss": "1001",
                "revenue": 1200,
                "revenueNtu": 200,
                "fuelVolume": 350,
                "checks": 40,
                "checksNtu": 8,
                "avgCheck": 30,
                "updatedAt": "2026-06-30T12:00:00+03:00",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "kpi_metrics.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE station_kpi_daily (
                        metric_date TEXT NOT NULL,
                        period TEXT NOT NULL,
                        ksss TEXT NOT NULL,
                        revenue REAL NOT NULL,
                        revenue_ntu REAL,
                        fuel_volume REAL NOT NULL,
                        checks REAL NOT NULL,
                        checks_ntu REAL,
                        avg_check REAL,
                        updated_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        PRIMARY KEY (metric_date, ksss)
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO station_kpi_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("2026-06-02", "2026-06", "1002", 999, None, 1, 1, None, None, "old", "old-sync"),
                )
                conn.execute(
                    "INSERT INTO station_kpi_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("2026-07-01", "2026-07", "2001", 777, None, 2, 2, None, None, "old", "old-sync"),
                )

            sync_kpi_metrics.mirror_period_to_local_db(
                period="2026-06",
                records=records,
                db_path=db_path,
                replace_period=True,
            )

            with sqlite3.connect(db_path) as conn:
                june_rows = conn.execute(
                    "SELECT metric_date, ksss, revenue, source FROM station_kpi_daily WHERE period = ?",
                    ("2026-06",),
                ).fetchall()
                july_rows = conn.execute(
                    "SELECT metric_date, ksss, revenue FROM station_kpi_daily WHERE period = ?",
                    ("2026-07",),
                ).fetchall()

        self.assertEqual(june_rows, [("2026-06-01", "1001", 1200.0, "dwh-sync")])
        self.assertEqual(july_rows, [("2026-07-01", "2001", 777.0)])


if __name__ == "__main__":
    unittest.main()
