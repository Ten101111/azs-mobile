import unittest

from backend.main import ALLOWED_STATION_STATUSES, visible_station_payload, stations_share_region


class SimilarStationTests(unittest.TestCase):
    def test_same_region_is_compared_case_insensitively(self):
        self.assertTrue(
            stations_share_region(
                {"subject": "Москва"},
                {"subject": " мОСКВА "},
            )
        )

    def test_missing_region_does_not_create_a_priority(self):
        self.assertFalse(stations_share_region({"subject": ""}, {"subject": "Москва"}))

    def test_station_status_whitelist_contains_only_operational_scope(self):
        self.assertEqual(
            ALLOWED_STATION_STATUSES,
            {
                "Действующая",
                "CODO",
                "Реконструкция",
                "Консервация",
                "Арендованные",
                "Временная приостановка работы",
            },
        )

    def test_visible_station_payload_excludes_other_statuses(self):
        from unittest.mock import patch

        payload = {
            "meta": {"count": 3},
            "stations": [
                {"ksss": "1", "status": "Действующая"},
                {"ksss": "2", "status": "Продана"},
                {"ksss": "3", "status": "Консервация"},
            ],
        }
        with patch("backend.main.load_station_payload", return_value=payload):
            visible = visible_station_payload()

        self.assertEqual(visible["meta"]["count"], 2)
        self.assertEqual([item["ksss"] for item in visible["stations"]], ["1", "3"])


if __name__ == "__main__":
    unittest.main()
