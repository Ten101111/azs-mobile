import unittest

from backend.main import stations_share_region


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


if __name__ == "__main__":
    unittest.main()
