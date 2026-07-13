from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.fuel_stock import (
    FuelStockImportError,
    FuelStockImportPayload,
    aggregate_tank_rows,
    business_low_for_percent,
    get_station_fuel_stock,
    normalize_fuel_name,
    replace_fuel_stock_snapshot,
    status_for_percent,
)


class FuelNameNormalizationTests(unittest.TestCase):
    def test_maps_live_dwh_variants(self):
        cases = {
            "Автобензины ЭКТО-92": "АБ92 ЭКТО",
            "Автобензины Регуляр Евро-92": "АБ92",
            "Бензин SUPER-92 (АИ-92-К5)": "АБ92",
            "Автобензины ЭКТО-95": "АБ95 ЭКТО",
            "Автобензины Премиум ЕВРО-95": "АБ95",
            "Бензин АИ-95-К5": "АБ95",
            "Бензин АИ-98-К5": "АБ98",
            "Автобензины ЭКТО-100": "АБ100 ЭКТО",
            "Бензин ЭКТО 100": "АБ100 ЭКТО",
            "Топливо дизельное ЭКТО": "ДТ ЭКТО",
            "ДТ ЭКТО": "ДТ ЭКТО",
            "Топливо диз ЕВРО с.С К5 (ДТ-Л-К5)": "ДТ",
            "Дизельное топливо": "ДТ",
            "Сжиженные газы": "СУГ",
            "Газ сжиженный ПБА": "СУГ",
            "Метан КПГ": "КПГ",
            "Автобензины А-91/А-92/АИ-93 прочие": "АБ92",
        }
        for source_name, expected in cases.items():
            with self.subTest(source_name=source_name):
                self.assertEqual(normalize_fuel_name(source_name), expected)

    def test_rejects_unknown_products_and_additives(self):
        for source_name in (
            "Присадка Greenpur DT ECTO",
            "Масло моторное 5W-40",
            "unknown",
        ):
            with self.subTest(source_name=source_name):
                self.assertEqual(normalize_fuel_name(source_name), "unmapped")


class FuelAggregationTests(unittest.TestCase):
    def test_aggregates_tanks_and_subtracts_dead_rest_per_tank(self):
        rows = [
            {
                "date": "2026-07-13",
                "ksss": "1001",
                "ent_name_crc": "station-a",
                "num_stor": "1",
                "dt_ins": datetime(2026, 7, 13, 11, 31),
                "fuel_name": "Автобензины ЭКТО-95",
                "oil_tn": 20,
                "fact_volume": 12,
                "dead_rest": 2,
            },
            {
                "date": "2026-07-13",
                "ksss": "1001",
                "ent_name_crc": "station-a",
                "num_stor": "2",
                "dt_ins": datetime(2026, 7, 13, 11, 31),
                "fuel_name": "Автобензины ЭКТО-95",
                "oil_tn": 10,
                "fact_volume": 1,
                "dead_rest": 2,
            },
        ]

        snapshot = aggregate_tank_rows(rows, volume_multiplier=1000)
        self.assertEqual(snapshot["accountDate"], "2026-07-13")
        self.assertEqual(snapshot["snapshotAt"], "2026-07-13T08:31:00+00:00")
        self.assertEqual(snapshot["diagnostics"]["stations"], 1)
        self.assertEqual(len(snapshot["records"]), 1)

        record = snapshot["records"][0]
        self.assertEqual(record["canonicalFuel"], "АБ95 ЭКТО")
        self.assertEqual(record["capacityLiters"], 30000)
        self.assertEqual(record["volumeLiters"], 13000)
        self.assertEqual(record["deadRestLiters"], 4000)
        self.assertEqual(record["availableLiters"], 10000)
        self.assertEqual(record["tanksCount"], 2)
        self.assertAlmostEqual(record["fillPercent"], 33.3333, places=4)

    def test_reports_unmapped_names_without_uploading_them(self):
        rows = [
            {
                "date": "2026-07-13",
                "ksss": "1001",
                "ent_name_crc": "station-a",
                "num_stor": "1",
                "dt_ins": datetime(2026, 7, 13, 11, 31),
                "fuel_name": "Присадка Greenpur DT ECTO",
                "oil_tn": 20,
                "fact_volume": 10,
                "dead_rest": 1,
            }
        ]
        snapshot = aggregate_tank_rows(rows, volume_multiplier=1000)
        self.assertEqual(snapshot["records"], [])
        self.assertEqual(snapshot["diagnostics"]["unmappedFuelNames"], {"Присадка Greenpur DT ECTO": 1})

    def test_color_and_business_threshold_boundaries(self):
        self.assertEqual(status_for_percent(29.999), "red")
        self.assertEqual(status_for_percent(30), "orange")
        self.assertEqual(status_for_percent(70), "orange")
        self.assertEqual(status_for_percent(70.001), "green")
        self.assertTrue(business_low_for_percent(19.999))
        self.assertFalse(business_low_for_percent(20))


def import_record(ksss: str, available: float = 10000) -> dict:
    return {
        "ksss": ksss,
        "canonicalFuel": "АБ95",
        "capacityLiters": 20000,
        "volumeLiters": 12000,
        "deadRestLiters": 2000,
        "availableLiters": available,
        "fillPercent": available / 20000 * 100,
        "tanksCount": 1,
        "sourceFuelNames": ["Бензин АИ-95-К5"],
        "sourceFuelNameCounts": {"Бензин АИ-95-К5": 1},
    }


class FuelSnapshotStoreTests(unittest.TestCase):
    def test_replaces_atomically_and_detects_unchanged_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            payload = FuelStockImportPayload(
                source="test",
                accountDate="2026-07-13",
                snapshotAt="2026-07-13T08:31:00+00:00",
                records=[import_record("1001"), import_record("1002")],
            )
            now = datetime(2026, 7, 13, 8, 45, tzinfo=timezone.utc)

            first = replace_fuel_stock_snapshot(payload, db_path=db_path, now=now)
            second = replace_fuel_stock_snapshot(payload, db_path=db_path, now=now)

            self.assertFalse(first.unchanged)
            self.assertEqual(first.imported, 2)
            self.assertTrue(second.unchanged)
            self.assertEqual(second.imported, 0)
            station = get_station_fuel_stock("1001", db_path=db_path)
            self.assertIsNotNone(station)
            self.assertEqual(station.items[0].availableVolumeLiters, 10000)
            self.assertEqual(station.items[0].availableVolumeTons, 10)
            self.assertEqual(station.items[0].availableTons, 10)

    def test_rejects_large_coverage_drop_and_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            initial = FuelStockImportPayload(
                source="test",
                accountDate="2026-07-13",
                snapshotAt="2026-07-13T08:31:00+00:00",
                records=[import_record("1001"), import_record("1002")],
            )
            reduced = FuelStockImportPayload(
                source="test",
                accountDate="2026-07-13",
                snapshotAt="2026-07-13T09:31:00+00:00",
                records=[import_record("1001")],
            )
            replace_fuel_stock_snapshot(initial, db_path=db_path)

            with self.assertRaises(FuelStockImportError) as error:
                replace_fuel_stock_snapshot(reduced, db_path=db_path, coverage_ratio=0.75)

            self.assertEqual(error.exception.status_code, 409)
            self.assertIsNotNone(get_station_fuel_stock("1002", db_path=db_path))

    def test_rejects_unmapped_import_record(self):
        record = import_record("1001")
        record["canonicalFuel"] = "unmapped"
        record["sourceFuelNames"] = ["Автобензины ЭКТО-92"]
        record["sourceFuelNameCounts"] = {"Автобензины ЭКТО-92": 1}
        payload = FuelStockImportPayload(
            source="test",
            accountDate="2026-07-13",
            snapshotAt="2026-07-13T08:31:00+00:00",
            records=[record],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FuelStockImportError):
                replace_fuel_stock_snapshot(payload, db_path=Path(temp_dir) / "fuel.sqlite3")

    def test_rejects_mismatched_checksum(self):
        payload = FuelStockImportPayload(
            source="test",
            accountDate="2026-07-13",
            snapshotAt="2026-07-13T08:31:00+00:00",
            checksum="0" * 64,
            records=[import_record("1001")],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FuelStockImportError) as error:
                replace_fuel_stock_snapshot(payload, db_path=Path(temp_dir) / "fuel.sqlite3")
        self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
