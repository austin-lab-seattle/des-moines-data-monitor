import json
import unittest
from unittest import mock

import lambda_api


class SeriesTests(unittest.TestCase):
    def test_smps_defaults_to_total_concentration_and_applies_time_filter(self):
        silver = "\n".join([
            "Scan Number,DateTime Sample Start,Total Concentration (#/cm³),Detector Status",
            "1,6/10/2025 16:25:06,5497.34,1",
            "2,6/10/2025 17:25:06,4858.09,1",
        ])
        event = {
            "queryStringParameters": {
                "instrument": "SMPS",
                "start": "2025-10-06T17:00:00",
            }
        }

        with mock.patch.object(lambda_api, "get_silver_text", return_value=silver):
            result = lambda_api.get_series(event)

        payload = json.loads(result["body"])
        self.assertEqual(200, result["statusCode"])
        self.assertEqual("Total Concentration (#/cm³)", payload["measurement"])
        self.assertEqual(1, payload["plotted_rows"])
        self.assertEqual(4858.09, payload["series"][0]["v"])

    def test_smps_ambiguous_csv_date_is_day_first(self):
        parsed = lambda_api.datetime_for_instrument("SMPS", "11/08/2026 10:40:50")
        self.assertEqual((2026, 8, 11, 17, 40, 50), (
            parsed.year, parsed.month, parsed.day,
            parsed.hour, parsed.minute, parsed.second,
        ))

    def test_neph_reports_pm25_by_default_and_hides_raw_bscat(self):
        silver = "\n".join([
            "Date_Time,Raw Scat coefficient (10^-6 m^-1),BScat (10^-4 m^-1),PM2.5 (µg/m³)",
            "2026-09-17 13:13:50,8.174,0.08174,4.937764",
        ])

        with mock.patch.object(lambda_api, "get_silver_text", return_value=silver):
            result = lambda_api.get_series({
                "queryStringParameters": {"instrument": "NEPH-PM25"}
            })

        payload = json.loads(result["body"])
        self.assertEqual("PM2.5 (µg/m³)", payload["measurement"])
        self.assertNotIn(
            "Raw Scat coefficient (10^-6 m^-1)", payload["measurements"]
        )
        self.assertIn("BScat (10^-4 m^-1)", payload["measurements"])
        self.assertNotIn("Major State", payload["measurements"])
        self.assertNotIn("PC_minus_instrument_s", payload["measurements"])


if __name__ == "__main__":
    unittest.main()
