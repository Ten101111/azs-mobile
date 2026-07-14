import os
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from backend.fuel_outages import (
    DETAIL_COLUMNS,
    FuelOutageImportError,
    aggregate_outage_snapshot,
    get_outage_snapshot,
    is_outage_ongoing,
    replace_outage_snapshot,
    rows_from_email,
    rows_from_html,
    rows_from_xlsx,
    xlsx_from_rows,
)


SAMPLE_ROWS = [
    {
        "npo": "УНП",
        "region": "Республика Башкортостан",
        "station": "АЗС №02003",
        "ksss": "2707",
        "product": "АБ95 ЭКТО",
        "hours": 2.5,
        "date": "2026-07-13",
        "startTime": "10:15",
        "endTime": "12:45",
        "expectedSalesLiters": 1250.75,
    },
    {
        "npo": "УНП",
        "region": "Республика Башкортостан",
        "station": "АЗС №02004",
        "ksss": "2709",
        "product": "ДТ ЭКТО",
        "hours": 1.0,
        "date": "2026-07-13",
        "startTime": "14:00",
        "endTime": "",
        "expectedSalesLiters": 500.0,
    },
]


SYNC_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sync_fuel_outages_from_email.py"
SYNC_SCRIPT_SPEC = spec_from_file_location("fuel_outage_email_sync", SYNC_SCRIPT_PATH)
assert SYNC_SCRIPT_SPEC and SYNC_SCRIPT_SPEC.loader
fuel_outage_email_sync = module_from_spec(SYNC_SCRIPT_SPEC)
SYNC_SCRIPT_SPEC.loader.exec_module(fuel_outage_email_sync)


class FakeImapClient:
    def __init__(self, messages):
        self.messages = messages

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b" ".join(self.messages)]
        uid, request = args
        message = self.messages[uid]
        if "HEADER.FIELDS" in request:
            content = message.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"
        else:
            content = message.as_bytes()
        return "OK", [(b"response", content)]


class FuelOutageTests(unittest.TestCase):
    def test_2359_is_treated_as_ongoing_and_aggregated_by_station_directory(self):
        first = {**SAMPLE_ROWS[0], "endTime": "23:59"}
        second = {**SAMPLE_ROWS[1], "endTime": ""}
        snapshot = {
            "sourceReceivedAt": "2026-07-13T16:00:00+03:00",
            "importedAt": "2026-07-13T16:10:00+03:00",
            "items": [first, second],
        }
        stations = [
            {
                "ksss": "2707",
                "subject": "Республика Башкортостан",
                "regionalManager": "Руководитель 1",
                "territoryManager": "Территория 1",
            },
            {
                "ksss": "2709",
                "subject": "Республика Башкортостан",
                "regionalManager": "Руководитель 1",
                "territoryManager": "Территория 2",
            },
        ]

        result = aggregate_outage_snapshot(snapshot, stations, "regionalManager")

        self.assertTrue(is_outage_ongoing(first))
        self.assertEqual(result["totals"]["ongoingCount"], 2)
        self.assertEqual(result["totals"]["stationCount"], 2)
        self.assertEqual(result["rows"][0]["label"], "Руководитель 1")
        self.assertEqual(result["rows"][0]["ongoingCount"], 2)
        self.assertEqual(result["rows"][0]["totalHours"], 3.5)

    def test_html_table_is_parsed_by_required_headers(self):
        header = "".join(f"<th>{column}</th>" for column in DETAIL_COLUMNS)
        values = ["УНП", "Башкортостан", "АЗС №02003", "2707", "АБ95 ЭКТО", "2,5", "13.07.2026", "10:15", "12:45", "1 250,75"]
        row = "".join(f"<td>{value}</td>" for value in values)
        rows = rows_from_html(f"<html><table><tr>{header}</tr><tr>{row}</tr></table></html>")
        self.assertEqual(rows[0]["ksss"], "2707")
        self.assertEqual(rows[0]["date"], "2026-07-13")
        self.assertEqual(rows[0]["hours"], 2.5)
        self.assertEqual(rows[0]["expectedSalesLiters"], 1250.75)

    def test_xlsx_round_trip_preserves_normalized_rows(self):
        content = xlsx_from_rows(SAMPLE_ROWS)
        self.assertEqual(rows_from_xlsx(content), SAMPLE_ROWS)

    def test_email_html_table_is_supported(self):
        message = EmailMessage()
        message.set_content("Отчет")
        header = "".join(f"<th>{column}</th>" for column in DETAIL_COLUMNS)
        values = ["УНП", "Башкортостан", "АЗС №02003", "2707", "АБ95", "2", "13.07.2026", "10:00", "12:00", "1000"]
        row = "".join(f"<td>{value}</td>" for value in values)
        message.add_alternative(f"<table><tr>{header}</tr><tr>{row}</tr></table>", subtype="html")
        self.assertEqual(rows_from_email(message)[0]["ksss"], "2707")

    def test_email_xlsx_attachment_is_supported(self):
        message = EmailMessage()
        message.set_content("Отчет во вложении")
        message.add_attachment(
            xlsx_from_rows(SAMPLE_ROWS),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="report.xlsx",
        )
        self.assertEqual(rows_from_email(message), SAMPLE_ROWS)

    def test_email_sync_skips_newer_matching_message_without_report(self):
        valid_message = EmailMessage()
        valid_message["From"] = "Artem.Manokhin@lukoil.com"
        valid_message["Subject"] = "Отчет по простоям объектов из-за отсутствия топлива"
        valid_message["Message-ID"] = "<valid@example.test>"
        valid_message.set_content("Отчет")
        header = "".join(f"<th>{column}</th>" for column in DETAIL_COLUMNS)
        values = ["УНП", "Башкортостан", "АЗС №02003", "2707", "АБ95", "2", "13.07.2026", "10:00", "12:00", "1000"]
        row = "".join(f"<td>{value}</td>" for value in values)
        valid_message.add_alternative(f"<table><tr>{header}</tr><tr>{row}</tr></table>", subtype="html")

        empty_message = EmailMessage()
        empty_message["From"] = "Artem.Manokhin@lukoil.com"
        empty_message["Subject"] = "FW: Отчет по простоям объектов из-за отсутствия топлива"
        empty_message["Message-ID"] = "<empty@example.test>"
        empty_message.set_content("Письмо без таблицы")

        client = FakeImapClient({b"1": valid_message, b"2": empty_message})
        uid, message, rows, skipped = fuel_outage_email_sync.latest_matching_message(
            client,
            "Artem.Manokhin@lukoil.com",
            "Отчет по простоям объектов из-за отсутствия топлива",
            300,
        )

        self.assertEqual(uid, "1")
        self.assertEqual(message["Message-ID"], "<valid@example.test>")
        self.assertEqual(rows[0]["ksss"], "2707")
        self.assertEqual(skipped, 1)

    def test_replace_snapshot_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            xlsx_path = Path(directory) / "latest.xlsx"
            snapshot_path = Path(directory) / "latest.json"
            env = {
                "FUEL_OUTAGE_XLSX_PATH": str(xlsx_path),
                "FUEL_OUTAGE_SNAPSHOT_PATH": str(snapshot_path),
            }
            with patch.dict(os.environ, env):
                content = xlsx_from_rows(SAMPLE_ROWS)
                now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
                first = replace_outage_snapshot(
                    content,
                    source_message_id="<report-1@example.test>",
                    source_received_at="2026-07-13T11:55:00+00:00",
                    source_email_from="Artem.Manokhin@lukoil.com",
                    now=now,
                )
                second = replace_outage_snapshot(
                    content,
                    source_message_id="<report-1@example.test>",
                    source_received_at="2026-07-13T11:55:00+00:00",
                    source_email_from="Artem.Manokhin@lukoil.com",
                    now=now,
                )
                snapshot = get_outage_snapshot()

            self.assertFalse(first["unchanged"])
            self.assertTrue(second["unchanged"])
            self.assertEqual(snapshot["rowCount"], 2)
            self.assertEqual(snapshot["stationCount"], 2)
            self.assertEqual(snapshot["activeCount"], 1)
            self.assertTrue(xlsx_path.exists())

    def test_missing_required_columns_is_rejected(self):
        with self.assertRaises(FuelOutageImportError):
            rows_from_html("<table><tr><th>АЗС</th></tr><tr><td>02003</td></tr></table>")


if __name__ == "__main__":
    unittest.main()
