from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.fuel_stock import (
    USABLE_PERCENT_SQL,
    FuelStockImportError,
    FuelStockImportPayload,
    aggregate_tank_rows,
    available_min_percent,
    business_low_for_percent,
    calculated_fill_percent,
    calculated_usable_percent,
    fuel_stock_connection,
    get_station_fuel_stock,
    list_fuel_stock_options,
    list_stations_with_available_fuel,
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

    def test_caps_over_capacity_percentage_and_preserves_anomaly(self):
        rows = [
            {
                "date": "2026-07-13",
                "ksss": "1001",
                "ent_name_crc": "station-a",
                "num_stor": "1",
                "dt_ins": datetime(2026, 7, 13, 11, 31),
                "fuel_name": "Бензин АИ-95-К5",
                "oil_tn": 10,
                "fact_volume": 13,
                "dead_rest": 1,
            }
        ]
        snapshot = aggregate_tank_rows(rows, volume_multiplier=1000)
        self.assertEqual(snapshot["records"][0]["fillPercent"], 100)
        self.assertEqual(snapshot["diagnostics"]["capacityExceededGroups"], 1)

        payload = FuelStockImportPayload(
            source="test",
            accountDate=snapshot["accountDate"],
            snapshotAt=snapshot["snapshotAt"],
            records=snapshot["records"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            replace_fuel_stock_snapshot(payload, db_path=db_path)
            item = get_station_fuel_stock("1001", db_path=db_path).items[0]
        self.assertEqual(item.fillPercent, 100)
        self.assertEqual(item.rawFillPercent, 120)
        self.assertTrue(item.capacityExceeded)

    def test_keeps_fuel_that_is_on_dead_stock(self):
        rows = [
            {
                "date": "2026-07-13",
                "ksss": "1001",
                "ent_name_crc": "station-a",
                "num_stor": "1",
                "dt_ins": datetime(2026, 7, 13, 11, 31),
                "fuel_name": "Автобензины ЭКТО-92",
                "oil_tn": 20,
                "fact_volume": 2,
                "dead_rest": 3,
            }
        ]
        snapshot = aggregate_tank_rows(rows, volume_multiplier=1000)
        self.assertEqual(snapshot["records"][0]["availableLiters"], 0)
        self.assertEqual(snapshot["records"][0]["fillPercent"], 0)
        self.assertEqual(snapshot["diagnostics"]["deadStockGroups"], 1)

        payload = FuelStockImportPayload(
            source="test",
            accountDate=snapshot["accountDate"],
            snapshotAt=snapshot["snapshotAt"],
            records=snapshot["records"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            replace_fuel_stock_snapshot(payload, db_path=db_path)
            item = get_station_fuel_stock("1001", db_path=db_path).items[0]
        self.assertEqual(item.canonicalFuel, "АБ92 ЭКТО")
        self.assertEqual(item.availableTons, 0)
        self.assertTrue(item.onDeadStock)

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


def availability_record(
    ksss: str,
    canonical_fuel: str = "АБ95",
    capacity: float = 20000,
    dead_rest: float = 2000,
    available: float = 900,
) -> dict:
    source_name = {"АБ95": "Бензин АИ-95-К5", "ДТ": "Дизельное топливо"}[canonical_fuel]
    return {
        "ksss": ksss,
        "canonicalFuel": canonical_fuel,
        "capacityLiters": capacity,
        "volumeLiters": available + dead_rest,
        "deadRestLiters": dead_rest,
        "availableLiters": available,
        "fillPercent": min(100.0, available / capacity * 100),
        "tanksCount": 1,
        "sourceFuelNames": [source_name],
        "sourceFuelNameCounts": {source_name: 1},
    }


class FuelAvailabilityFilterTests(unittest.TestCase):
    """The filter measures available fuel against the dispensable capacity.

    Dispensable capacity is capacity minus the dead rest, so the percentage differs
    from the stored fill_percent, which uses the whole tank as its denominator.
    """

    def test_percent_uses_dispensable_capacity_not_full_tank(self):
        raw, capped = calculated_usable_percent(available=900, capacity=20000, dead_rest=2000)
        self.assertAlmostEqual(raw, 5.0)
        self.assertAlmostEqual(capped, 5.0)
        # The same numbers read lower against the full tank.
        self.assertAlmostEqual(calculated_fill_percent(900, 20000)[1], 4.5)

    def test_percent_is_zero_when_dead_rest_swallows_the_tank(self):
        self.assertEqual(calculated_usable_percent(available=0, capacity=2000, dead_rest=2000), (0.0, 0.0))
        self.assertEqual(calculated_usable_percent(available=0, capacity=1000, dead_rest=2000), (0.0, 0.0))

    def _seed(self, db_path: Path, records: list[dict]) -> None:
        payload = FuelStockImportPayload(
            source="test",
            accountDate="2026-07-13",
            snapshotAt="2026-07-13T08:31:00+00:00",
            records=records,
        )
        replace_fuel_stock_snapshot(payload, db_path=db_path)

    def test_selects_stations_at_or_above_the_threshold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            self._seed(
                db_path,
                [
                    availability_record("1001", available=900),  # exactly 5.0%
                    availability_record("1002", available=899),  # 4.99%
                    availability_record("1003", available=9000),  # 50%
                    availability_record("1004", available=0),  # on dead stock
                    availability_record("1005", canonical_fuel="ДТ", available=9000),
                ],
            )

            result = list_stations_with_available_fuel(["АБ95"], 5, db_path=db_path)

            self.assertEqual(result.ksss, ["1001", "1003"])
            self.assertEqual(result.matched, 2)
            self.assertEqual(result.stations, 5)
            self.assertEqual(result.minPercent, 5)
            self.assertEqual(result.canonicalFuels, ["АБ95"])

    def test_several_fuels_require_all_of_them_at_the_same_station(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            self._seed(
                db_path,
                [
                    availability_record("1001", available=9000),  # АБ95 only
                    availability_record("1002", canonical_fuel="ДТ", available=9000),  # ДТ only
                    availability_record("1003", available=9000),
                    availability_record("1003", canonical_fuel="ДТ", available=9000),  # both
                    availability_record("1004", available=9000),
                    availability_record("1004", canonical_fuel="ДТ", available=0),  # ДТ on dead stock
                ],
            )

            both = list_stations_with_available_fuel(["АБ95", "ДТ"], 5, db_path=db_path)
            single = list_stations_with_available_fuel(["АБ95"], 5, db_path=db_path)

            self.assertEqual(both.ksss, ["1003"])
            self.assertEqual(both.canonicalFuels, ["АБ95", "ДТ"])
            # Adding a fuel narrows the result, never widens it.
            self.assertTrue(set(both.ksss).issubset(set(single.ksss)))

    def test_ignores_duplicate_and_blank_fuel_arguments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            self._seed(db_path, [availability_record("1001", available=9000)])

            result = list_stations_with_available_fuel(["АБ95", " АБ95 ", ""], 5, db_path=db_path)

            self.assertEqual(result.canonicalFuels, ["АБ95"])
            self.assertEqual(result.ksss, ["1001"])

    def test_rejects_an_empty_fuel_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            self._seed(db_path, [availability_record("1001")])

            with self.assertRaises(FuelStockImportError) as error:
                list_stations_with_available_fuel([], 5, db_path=db_path)

        self.assertEqual(error.exception.status_code, 422)

    def test_sql_filter_matches_the_python_formula_row_by_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            records = [
                availability_record("1001", available=900),
                availability_record("1002", available=899),
                availability_record("1003", available=9000),
                availability_record("1004", available=0),
                availability_record("1005", capacity=15000, dead_rest=14000, available=100),
                availability_record("1006", canonical_fuel="ДТ", capacity=30000, dead_rest=3000, available=1350),
            ]
            self._seed(db_path, records)

            with fuel_stock_connection(db_path) as conn:
                rows = conn.execute(
                    f"SELECT ksss, capacity_liters, dead_rest_liters, available_liters, {USABLE_PERCENT_SQL} AS pct "
                    "FROM fuel_stock_current"
                ).fetchall()

            self.assertEqual(len(rows), len(records))
            for row in rows:
                with self.subTest(ksss=row["ksss"]):
                    expected = calculated_usable_percent(
                        float(row["available_liters"]),
                        float(row["capacity_liters"]),
                        float(row["dead_rest_liters"]),
                    )[1]
                    self.assertAlmostEqual(float(row["pct"]), expected, places=9)

    def test_lists_only_fuels_present_in_the_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            self._seed(
                db_path,
                [
                    availability_record("1001", available=9000),
                    availability_record("1002", available=0),
                    availability_record("1003", canonical_fuel="ДТ", available=9000),
                ],
            )

            options = list_fuel_stock_options(5, db_path=db_path)

            self.assertEqual([item.canonicalFuel for item in options.fuels], ["АБ95", "ДТ"])
            self.assertEqual(options.fuels[0].stations, 2)
            self.assertEqual(options.fuels[0].availableStations, 1)
            self.assertEqual(options.fuels[1].availableStations, 1)

    def test_rejects_a_fuel_outside_the_canonical_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            self._seed(db_path, [availability_record("1001")])

            with self.assertRaises(FuelStockImportError) as error:
                list_stations_with_available_fuel(["АИ-95"], 5, db_path=db_path)

        self.assertEqual(error.exception.status_code, 422)

    def test_falls_back_to_the_configured_threshold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fuel.sqlite3"
            self._seed(db_path, [availability_record("1001", available=900)])

            result = list_stations_with_available_fuel(["АБ95"], None, db_path=db_path)

            self.assertEqual(result.minPercent, available_min_percent())
            self.assertEqual(result.ksss, ["1001"])


if __name__ == "__main__":
    unittest.main()
