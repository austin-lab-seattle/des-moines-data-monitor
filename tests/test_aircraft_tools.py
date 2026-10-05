import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "aircraft" / "build_opensky_query.py"
SPEC = importlib.util.spec_from_file_location("build_opensky_query", MODULE_PATH)
QUERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUERY)


class AircraftToolTests(unittest.TestCase):
    def test_query_is_bounded_by_time_partition_and_position(self):
        start = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
        sql = QUERY.build_query(47.422703, -122.297714, 10, start, end)
        self.assertIn("hour IN (", sql)
        self.assertIn("time BETWEEN 1784142000 AND 1784145600", sql)
        self.assertIn("lat BETWEEN", sql)
        self.assertIn("lon BETWEEN", sql)
        self.assertIn("geoaltitude", sql)
        self.assertIn("baroaltitude", sql)

    def test_hour_partitions_include_both_bounding_hours(self):
        start = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 2, 5, tzinfo=timezone.utc)
        self.assertEqual(3, len(QUERY.hour_partitions(start, end)))


if __name__ == "__main__":
    unittest.main()
