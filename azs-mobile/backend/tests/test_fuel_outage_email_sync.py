from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.fuel_outages import xlsx_from_rows


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sync_fuel_outages_from_email.py"
SPEC = importlib.util.spec_from_file_location("sync_fuel_outages_from_email", SCRIPT_PATH)
assert SPEC and SPEC.loader
sync_fuel_outages_from_email = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_fuel_outages_from_email)


class LocalFuelOutageSnapshotTests(unittest.TestCase):
    def test_loads_current_mail_credentials_from_configured_source_env_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime_dir = temp_path / "runtime"
            source_env_path = temp_path / "source.env"
            runtime_dir.mkdir()
            (runtime_dir / ".env.local").write_text(
                f"FUEL_OUTAGE_SOURCE_ENV_PATH={source_env_path}\n",
                encoding="utf-8",
            )
            source_env_path.write_text("IMAP_PASSWORD=current-app-password\n", encoding="utf-8")

            with patch.object(sync_fuel_outages_from_email, "PROJECT_DIR", runtime_dir):
                with patch.dict("os.environ", {}, clear=True):
                    sync_fuel_outages_from_email.load_env()
                    self.assertEqual(os.environ["IMAP_PASSWORD"], "current-app-password")

    def test_writes_email_report_to_configured_application_paths(self):
        rows = [
            {
                "npo": "NPO",
                "region": "Москва",
                "station": "АЗС 1",
                "ksss": "1001",
                "product": "АИ-95",
                "hours": 2.5,
                "date": "2026-09-01",
                "startTime": "08:00",
                "endTime": "",
                "expectedSalesLiters": 320,
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "application-data"
            snapshot_path = data_dir / "fuel_outages_latest.json"
            workbook_path = data_dir / "fuel_outages_latest.xlsx"
            with patch.dict(
                "os.environ",
                {
                    "FUEL_OUTAGE_SNAPSHOT_PATH": str(snapshot_path),
                    "FUEL_OUTAGE_XLSX_PATH": str(workbook_path),
                },
            ):
                result = sync_fuel_outages_from_email.store_local_snapshot(
                    xlsx_from_rows(rows),
                    message_id="<latest-report@example.test>",
                    received_at="2026-09-01T06:10:00+00:00",
                    from_email="reports@example.test",
                )

            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            workbook_exists = workbook_path.exists()

        self.assertFalse(result["unchanged"])
        self.assertEqual(payload["rowCount"], 1)
        self.assertEqual(payload["activeCount"], 1)
        self.assertEqual(payload["sourceMessageId"], "<latest-report@example.test>")
        self.assertTrue(workbook_exists)


if __name__ == "__main__":
    unittest.main()
