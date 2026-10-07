"""Independent numerical regression cases. These are synthetic, NOT field data."""
import json
import unittest
from unittest import mock

import lambda_api


class DataAccuracyTests(unittest.TestCase):
    def series(self, silver, instrument="NO2-CAPS", **params):
        with mock.patch.object(lambda_api, "get_silver_text", return_value=silver):
            result = lambda_api.get_series({"queryStringParameters": {
                "instrument": instrument, **params,
            }})
        return result["statusCode"], json.loads(result["body"])

    def test_hourly_mean_excludes_invalid_values_and_preserves_zero_and_negatives(self):
        # Independent arithmetic: (0 + -2 + 8) / 3 = 2, not 1.5 or NaN.
        silver = "\n".join([
            "HHMMSS,Concentration,Timestamp",
            "000000,0,2026-10-07 00:00:00",
            "000005,-2,2026-10-07 00:00:05",
            "000010,8,2026-10-07 00:00:10",
            "000015,NaN,2026-10-07 00:00:15",
            "000020,Infinity,2026-10-07 00:00:20",
            "000025,-Infinity,2026-10-07 00:00:25",
            "000030,,2026-10-07 00:00:30",
            "000035,error,2026-10-07 00:00:35",
            "000040,100,2026-10-07 00:00:40,EXTRA",
            "000045,100,invalid-date",
        ])
        status, payload = self.series(silver)
        self.assertEqual(200, status)
        self.assertEqual([{"t": "2026-10-07T07:00:00Z", "v": 2.0, "n": 3}], payload["series"])
        self.assertEqual(3, payload["plotted_rows"])
        self.assertEqual(5, payload["skipped_invalid_measurement"])
        self.assertEqual(1, payload["skipped_schema_mismatch"])
        self.assertEqual(1, payload["skipped_no_timestamp"])
        self.assertEqual("sample_mean", payload["aggregation"])
        json.dumps(payload, allow_nan=False)

    def test_all_five_instruments_have_independent_expected_means_and_timestamps(self):
        cases = [
            ("BC-MA200", "Date / time local,BC1", ["2026-10-07 00:00:00,0", "2026-10-07 00:00:05,120"], "BC1", 60),
            ("CO2-LICOR", "System_Date_(Y-M-D),System_Time_(h:m:s),CO2_(umol_mol-1)", ["2026-10-07,00:00:00,400", "2026-10-07,00:00:05,440"], "CO2_(umol_mol-1)", 420),
            ("NEPH-PM25", "Date_Time,PM2.5 (µg/m³)", ["2026-10-07 00:00:00,2.6", "2026-10-07 00:00:05,5.46"], "PM2.5 (µg/m³)", 4.03),
            ("NO2-CAPS", "HHMMSS,Concentration,Timestamp", ["000000,10.255,2026-10-07 00:00:00", "000005,11.745,2026-10-07 00:00:05"], "Concentration", 11),
            ("SMPS", "Scan Number,DateTime Sample Start,Total Concentration (#/cm³)", ["1,07/10/2026 00:00:00,1000", "2,07/10/2026 00:00:05,3000"], "Total Concentration (#/cm³)", 2000),
        ]
        for instrument, header, rows, column, expected in cases:
            with self.subTest(instrument=instrument):
                status, payload = self.series("\n".join([header, *rows]), instrument)
                self.assertEqual(200, status)
                self.assertEqual(column, payload["measurement"])
                self.assertEqual([{"t": "2026-10-07T07:00:00Z", "v": expected, "n": 2}], payload["series"])

    def test_hour_boundaries_and_missing_hours_do_not_invent_points(self):
        silver = "\n".join([
            "Concentration,Timestamp",
            "1,2026-10-07 00:59:59", "5,2026-10-07 01:00:00",
            "9,2026-10-07 03:00:00",
        ])
        _, payload = self.series(silver, start="2026-10-07T08:00:00Z", end="2026-10-07T10:00:00Z")
        self.assertEqual([
            {"t": "2026-10-07T08:00:00Z", "v": 5, "n": 1},
            {"t": "2026-10-07T10:00:00Z", "v": 9, "n": 1},
        ], payload["series"])

    def test_mean_of_large_finite_values_does_not_overflow_json(self):
        _, payload = self.series("Concentration,Timestamp\n1e308,2026-10-07 00:00:00\n1e308,2026-10-07 00:00:05")
        self.assertEqual(1e308, payload['series'][0]['v'])
        json.dumps(payload, allow_nan=False)

    def test_time_and_unknown_columns_are_never_silently_substituted(self):
        silver = "HHMMSS,Concentration,Timestamp\n000000,10,2026-10-07 00:00:00"
        for measurement in ["HHMMSS", "does-not-exist", "Timestamp"]:
            with self.subTest(measurement=measurement):
                status, _ = self.series(silver, measurement=measurement)
                self.assertEqual(400, status)
        status, payload = self.series("HHMMSS,Timestamp\n000000,2026-10-07 00:00:00")
        self.assertEqual(200, status)
        self.assertEqual([], payload["measurements"])
        self.assertEqual([], payload["series"])

    def test_sparse_column_is_not_lost_to_first_25_rows_heuristic(self):
        silver = "Concentration,Timestamp\n" + "\n".join([
            *(f",2026-10-07 00:00:{index:02}" for index in range(25)),
            "5,2026-10-07 00:01:00",
        ])
        _, payload = self.series(silver)
        self.assertEqual("Concentration", payload["measurement"])
        self.assertEqual(5, payload["series"][0]["v"])

    def test_invalid_time_ranges_are_rejected_not_ignored(self):
        silver = "Concentration,Timestamp\n10,2026-10-07 00:00:00"
        for params in [{"start": "garbage"}, {"end": "garbage"}, {"start": "2026-10-08", "end": "2026-10-07"}]:
            with self.subTest(params=params):
                self.assertEqual(400, self.series(silver, **params)[0])

    def test_latest_readings_are_sorted_by_time_without_changing_source_values(self):
        silver = "\n".join([
            "Concentration,Timestamp",
            "10.123456789,2026-10-07 00:00:10",
            "999,invalid-date", "444,2026-10-07 00:00:15,EXTRA",
            "20.222222222,2026-10-07 00:00:20",
            "0,2026-10-07 00:00:00",
        ])
        with mock.patch.object(lambda_api, "get_silver_text", return_value=silver), mock.patch.object(lambda_api, "read_review_objects", return_value=[]):
            columns, rows, cursor, _ = lambda_api.parse_silver_records("NO2-CAPS", limit=2, order="desc")
            _, next_rows, _, _ = lambda_api.parse_silver_records("NO2-CAPS", limit=2, cursor=cursor, order="desc")
        self.assertEqual(["Concentration", "Timestamp"], columns)
        self.assertEqual(["20.222222222", "10.123456789"], [row["values"]["Concentration"] for row in rows])
        self.assertEqual(["0"], [row["values"]["Concentration"] for row in next_rows])
        self.assertEqual("2026-10-07T07:00:20+00:00", rows[0]["timestamp_iso"])
        self.assertEqual(3, rows[0]["source_index"])

    def test_observation_filters_and_count_preview_fail_closed(self):
        silver = "Concentration,Timestamp\n10,2026-10-07 00:00:00"
        with mock.patch.object(lambda_api, 'get_silver_text', return_value=silver):
            for query in [{'start': 'invalid'}, {'order': 'random'}, {'count_only': 'true', 'end': 'invalid'}]:
                result = lambda_api.get_silver_records({'queryStringParameters': {'instrument': 'NO2-CAPS', **query}})
                self.assertEqual(400, result['statusCode'])

    def test_pacific_winter_summer_and_explicit_fall_back_offsets(self):
        cases = [
            ("2026-01-15 00:00:00", "2026-01-15T08:00:00Z"),
            ("2026-07-15 00:00:00", "2026-07-15T07:00:00Z"),
            ("2026-11-01T01:30:00-07:00", "2026-11-01T08:30:00Z"),
            ("2026-11-01T01:30:00-08:00", "2026-11-01T09:30:00Z"),
        ]
        for stamp, expected in cases:
            _, payload = self.series(f"Concentration,Timestamp\n5,{stamp}", bucket_minutes="1")
            self.assertEqual(expected, payload["series"][0]["t"])


if __name__ == "__main__":
    unittest.main()
