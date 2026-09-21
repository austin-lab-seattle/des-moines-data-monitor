import json
import unittest
from unittest import mock

import lambda_api


class FakeExportS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://example.invalid/{Params['Key']}?expires={ExpiresIn}"


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.original_s3 = lambda_api.s3_client
        self.s3 = FakeExportS3()
        lambda_api.s3_client = self.s3

    def tearDown(self):
        lambda_api.s3_client = self.original_s3

    @staticmethod
    def smps_silver():
        return "\n".join([
            "Scan Number,DateTime Sample Start,Total Concentration (#/cm³)",
            "1,01/09/2026 12:00:00,100",
            "2,10/09/2026 12:00:00,200",
            "3,20/09/2026 12:00:00,300",
        ])

    def test_default_export_contains_only_latest_fourteen_days(self):
        with mock.patch.object(lambda_api, "get_silver_text", return_value=self.smps_silver()):
            result = lambda_api.get_observations_export({
                "queryStringParameters": {"instrument": "SMPS"}
            })

        payload = json.loads(result["body"])
        self.assertEqual(200, result["statusCode"])
        self.assertEqual(2, payload["rows"])
        self.assertEqual(14, payload["default_days"])
        self.assertEqual(31, payload["max_days"])
        self.assertEqual(1, len(self.s3.puts))
        body = self.s3.puts[0]["Body"].decode("utf-8")
        self.assertNotIn("01/09/2026", body)
        self.assertIn("10/09/2026", body)
        self.assertIn("20/09/2026", body)
        self.assertEqual("generated-export=true", self.s3.puts[0]["Tagging"])

    def test_export_rejects_window_over_thirty_one_days(self):
        with mock.patch.object(lambda_api, "get_silver_text", return_value=self.smps_silver()):
            result = lambda_api.get_observations_export({
                "queryStringParameters": {
                    "instrument": "SMPS",
                    "start": "2026-07-01T00:00:00-07:00",
                    "end": "2026-09-01T00:00:00-07:00",
                }
            })

        payload = json.loads(result["body"])
        self.assertEqual(400, result["statusCode"])
        self.assertIn("31 days", payload["error"])
        self.assertFalse(self.s3.puts)

    def test_export_rejects_too_many_rows_without_silent_truncation(self):
        with (
            mock.patch.object(lambda_api, "get_silver_text", return_value=self.smps_silver()),
            mock.patch.object(lambda_api, "EXPORT_MAX_ROWS", 1),
        ):
            result = lambda_api.get_observations_export({
                "queryStringParameters": {
                    "instrument": "SMPS",
                    "start": "2026-09-01T00:00:00-07:00",
                    "end": "2026-09-20T23:59:59-07:00",
                }
            })

        self.assertEqual(413, result["statusCode"])
        self.assertFalse(self.s3.puts)

    def test_pacific_wall_time_matches_explicit_utc_query(self):
        local = lambda_api.datetime_for_compare("2026-09-18 12:00:00")
        utc = lambda_api.datetime_for_compare("2026-09-18T19:00:00Z")
        self.assertEqual(local, utc)


if __name__ == "__main__":
    unittest.main()
