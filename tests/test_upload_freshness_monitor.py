import importlib.util
import io
import hashlib
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


class FakeNoSuchKey(Exception):
    pass


class FakePaginator:
    def __init__(self, s3):
        self.s3 = s3

    def paginate(self, Bucket, Prefix):
        if Prefix in self.s3.errors:
            raise self.s3.errors[Prefix]
        return [{"Contents": [item for item in self.s3.objects if item["Key"].startswith(Prefix)]}]


class FakeS3:
    class exceptions:
        NoSuchKey = FakeNoSuchKey

    def __init__(self, objects=None, state=None, silver=None):
        self.objects = objects or []
        self.state = state
        self.silver = silver or {}
        self.errors = {}

    def get_paginator(self, name):
        return FakePaginator(self)

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        if self.state is None:
            return {}
        return {"Contents": [{"Key": Prefix}]}

    def get_object(self, Bucket, Key):
        if Key in self.errors:
            raise self.errors[Key]
        if "/silver/" in Key:
            if Key not in self.silver:
                raise FakeNoSuchKey("Silver not found")
            data = self.silver[Key].encode()
            return {"Body": io.BytesIO(data), "ETag": hashlib.sha256(data).hexdigest()}
        if self.state is None:
            raise FakeNoSuchKey()
        return {"Body": io.BytesIO(json.dumps(self.state).encode())}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.state = json.loads(Body.decode())


class FakeSES:
    def __init__(self):
        self.messages = []

    def send_email(self, **kwargs):
        self.messages.append(kwargs)


def load_module():
    fake_boto3 = types.SimpleNamespace(client=lambda *args, **kwargs: object())
    previous = sys.modules.get("boto3")
    sys.modules["boto3"] = fake_boto3
    try:
        path = Path(__file__).parents[1] / "lambda" / "upload_freshness_monitor.py"
        spec = importlib.util.spec_from_file_location("upload_freshness_monitor", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            del sys.modules["boto3"]
        else:
            sys.modules["boto3"] = previous


monitor = load_module()


class UploadFreshnessMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)
        monitor.FROM_EMAIL = "sender@example.edu"
        monitor.TO_EMAILS = ["primary@example.edu"]
        monitor.CC_EMAILS = ["copy-one@example.edu", "copy-two@example.edu"]
        monitor.STALE_AFTER_HOURS = 6

    def bronze(self, age_hours, key="NO2-CAPS/bronze/year=2026/month=09/batch.txt"):
        return {"Key": key, "Size": 42, "LastModified": self.now - timedelta(hours=age_hours)}

    def silver(self, age_hours=1, instrument="NO2-CAPS"):
        header = "Scan Number,DateTime Sample Start,Total Concentration (#/cm³)" if instrument == "SMPS" else "Concentration,Timestamp"
        stamp = (self.now - timedelta(hours=age_hours)).isoformat()
        row = f"1,{stamp},1000" if instrument == "SMPS" else f"10,{stamp}"
        return {f"{instrument}/silver/{instrument}_data.csv": f"{header}\n{row}\n"}

    def test_empty_bucket_remains_unarmed_and_sends_nothing(self):
        s3, ses = FakeS3(), FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertEqual("waiting-for-first-upload", result["status"])
        self.assertFalse(result["armed"])
        self.assertEqual([], ses.messages)
        self.assertIsNone(s3.state)

    def test_first_fresh_upload_arms_without_email(self):
        s3, ses = FakeS3([self.bronze(1)], silver=self.silver()), FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertEqual("healthy", result["status"])
        self.assertTrue(s3.state["armed"])
        self.assertEqual([], ses.messages)

    def test_stale_upload_sends_one_alert_not_hourly_spam(self):
        s3, ses = FakeS3([self.bronze(7)], silver=self.silver()), FakeSES()
        first = monitor.run_monitor(s3, ses, self.now)
        second = monitor.run_monitor(s3, ses, self.now + timedelta(hours=1))
        self.assertTrue(first["alert_sent"])
        self.assertFalse(second["alert_sent"])
        self.assertEqual(1, len(ses.messages))
        self.assertEqual(["primary@example.edu"], ses.messages[0]["Destination"]["ToAddresses"])
        self.assertEqual(
            ["copy-one@example.edu", "copy-two@example.edu"],
            ses.messages[0]["Destination"]["CcAddresses"],
        )

    def test_fresh_upload_after_stale_sends_one_recovery(self):
        state = {
            "armed": True,
            "status": "stale",
            "last_upload_at": (self.now - timedelta(hours=8)).isoformat(),
            "last_upload_key": "NO2-CAPS/bronze/old.txt",
        }
        s3, ses = FakeS3([self.bronze(0.25)], state, self.silver()), FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertTrue(result["recovery_sent"])
        self.assertEqual("healthy", s3.state["status"])
        self.assertEqual(1, len(ses.messages))

    def test_fresh_other_instruments_cannot_hide_stale_smps(self):
        objects = [self.bronze(.1), self.bronze(9, "SMPS/bronze/old.csv")]
        s3 = FakeS3(objects, silver={**self.silver(), **self.silver(9, "SMPS")})
        ses = FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertEqual(["SMPS"], result["new_alerts"])
        self.assertEqual(["no-upload", "stale-reading"], result["instruments"]["SMPS"]["reasons"])
        self.assertEqual("healthy", result["instruments"]["NO2-CAPS"]["status"])
        self.assertEqual(1, len(ses.messages))
        self.assertIn("SMPS", ses.messages[0]["Message"]["Subject"]["Data"])

    def test_fresh_upload_of_old_readings_still_alerts(self):
        s3, ses = FakeS3([self.bronze(.1)], silver=self.silver(8)), FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertEqual(["stale-reading"], result["instruments"]["NO2-CAPS"]["reasons"])
        self.assertTrue(result["alert_sent"])

    def test_exactly_six_hours_is_stale_and_no_hourly_repeat(self):
        s3, ses = FakeS3([self.bronze(.1)], silver=self.silver(6)), FakeSES()
        first = monitor.run_monitor(s3, ses, self.now)
        second = monitor.run_monitor(s3, ses, self.now + timedelta(hours=1))
        self.assertTrue(first["alert_sent"])
        self.assertFalse(second["alert_sent"])
        self.assertEqual(1, len(ses.messages))

    def test_upload_alone_does_not_clear_stale_reading_incident(self):
        s3, ses = FakeS3([self.bronze(8)], silver=self.silver(8)), FakeSES()
        monitor.run_monitor(s3, ses, self.now)
        s3.objects = [self.bronze(.1)]
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertFalse(result["recovery_sent"])
        self.assertEqual("stale", result["status"])
        s3.silver = self.silver(.1)
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertTrue(result["recovery_sent"])
        self.assertFalse(monitor.run_monitor(s3, ses, self.now)["recovery_sent"])
        self.assertEqual(2, len(ses.messages))

    def test_failed_check_does_not_send_false_recovery_or_block_other_instruments(self):
        s3, ses = FakeS3([self.bronze(8)], silver=self.silver(1)), FakeSES()
        monitor.run_monitor(s3, ses, self.now)
        s3.objects = [self.bronze(.1), self.bronze(9, "SMPS/bronze/old.csv")]
        s3.silver.update(self.silver(9, "SMPS"))
        s3.errors["NO2-CAPS/silver/NO2-CAPS_data.csv"] = PermissionError("denied")
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertFalse(result["recovery_sent"])
        self.assertEqual(["SMPS"], result["new_alerts"])
        self.assertTrue(result["instruments"]["NO2-CAPS"]["alert_active"])
        self.assertIn("reading", result["instruments"]["NO2-CAPS"]["check_errors"])

    def test_never_reporting_instrument_stays_unarmed(self):
        s3, ses = FakeS3([self.bronze(.1)], silver=self.silver()), FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertFalse(result["instruments"]["BC-MA200"]["armed"])
        self.assertEqual("waiting-for-first-upload", result["instruments"]["BC-MA200"]["status"])
        self.assertEqual([], result["new_alerts"])

    def test_started_uploads_without_any_silver_reading_alert_after_grace_period(self):
        s3, ses = FakeS3([self.bronze(.1)]), FakeSES()
        self.assertFalse(monitor.run_monitor(s3, ses, self.now)["alert_sent"])
        later = self.now + timedelta(hours=6)
        s3.objects = [{"Key": "NO2-CAPS/bronze/fresh.txt", "Size": 42, "LastModified": later}]
        result = monitor.run_monitor(s3, ses, later)
        self.assertEqual(["reading-unavailable"], result["instruments"]["NO2-CAPS"]["reasons"])
        self.assertTrue(result["alert_sent"])

    def test_dry_run_does_not_send_or_write_state(self):
        s3, ses = FakeS3([self.bronze(8)], silver=self.silver(8)), FakeSES()
        result = monitor.run_monitor(s3, ses, self.now, dry_run=True)
        self.assertEqual(["NO2-CAPS"], result["new_alerts"])
        self.assertFalse(result["alert_sent"])
        self.assertIn("stale-reading", result["notification_preview"])
        self.assertEqual([], ses.messages)
        self.assertIsNone(s3.state)

    def test_latest_reading_is_maximum_valid_timestamp_not_last_row_or_object_mtime(self):
        key = "SMPS/silver/SMPS_data.csv"
        s3 = FakeS3(silver={key: "Scan Number,DateTime Sample Start,Total Concentration (#/cm³)\n"
                    "1,17/09/2026 10:30:00,100\n2,17/09/2026 09:00:00,100\n"
                    "3,garbage,100\n4,17/09/2026 11:00:00,100,EXTRA\n"})
        stamp, _ = monitor.latest_reading(s3, "SMPS", {}, self.now)
        self.assertEqual("2026-09-17T17:30:00+00:00", stamp.isoformat())

    def test_future_and_invalid_timestamps_cannot_manufacture_freshness(self):
        key = "NO2-CAPS/silver/NO2-CAPS_data.csv"
        for stamp in ["2027-09-17T18:00:00Z", "invalid"]:
            s3 = FakeS3([self.bronze(.1)], silver={key: f"Concentration,Timestamp\n10,{stamp}\n"})
            result = monitor.run_monitor(s3, FakeSES(), self.now)
            self.assertEqual("unknown", result["status"])
            self.assertIsNone(result["instruments"]["NO2-CAPS"]["last_reading_at"])

    def test_ses_failure_does_not_record_an_alert_as_delivered(self):
        s3 = FakeS3([self.bronze(8)], silver=self.silver(8))
        ses = FakeSES()
        ses.send_email = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("SES unavailable"))
        with self.assertRaisesRegex(RuntimeError, "SES unavailable"):
            monitor.run_monitor(s3, ses, self.now)
        self.assertIsNone(s3.state)

    def test_multiple_stale_instruments_share_one_notification(self):
        s3 = FakeS3([self.bronze(8), self.bronze(9, "SMPS/bronze/old.csv")],
                    silver={**self.silver(8), **self.silver(9, "SMPS")})
        ses = FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertEqual(["NO2-CAPS", "SMPS"], result["new_alerts"])
        self.assertEqual(1, len(ses.messages))

    def test_alert_threshold_in_message_is_configured_not_hard_coded(self):
        monitor.STALE_AFTER_HOURS = 12
        s3, ses = FakeS3([self.bronze(13)], silver=self.silver(13)), FakeSES()
        monitor.run_monitor(s3, ses, self.now)
        self.assertIn("Threshold: 12 hours", ses.messages[0]["Message"]["Body"]["Text"]["Data"])

    def test_cached_source_keeps_actual_reading_time_not_rebuild_time(self):
        s3, ses = FakeS3([self.bronze(.1)], silver=self.silver(5.5)), FakeSES()
        monitor.run_monitor(s3, ses, self.now)
        with mock.patch.object(monitor, "datetime_for_instrument", side_effect=AssertionError("should reuse unchanged ETag")):
            result = monitor.run_monitor(s3, ses, self.now + timedelta(hours=1))
        self.assertTrue(result["alert_sent"])
        self.assertEqual(["stale-reading"], result["instruments"]["NO2-CAPS"]["reasons"])

    def test_upload_listing_error_cannot_clear_an_incident(self):
        s3, ses = FakeS3([self.bronze(8)], silver=self.silver()), FakeSES()
        monitor.run_monitor(s3, ses, self.now)
        s3.errors["NO2-CAPS/bronze/"] = PermissionError("denied")
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertFalse(result["recovery_sent"])
        self.assertIn("upload", result["instruments"]["NO2-CAPS"]["check_errors"])

    def test_all_five_sources_use_same_pacific_time_as_dashboard(self):
        cases = {
            "BC-MA200": "Date / time local,BC1\n2026/09/17 10:30:00,100",
            "CO2-LICOR": "System_Date_(Y-M-D),System_Time_(h:m:s),CO2_(umol_mol-1)\n2026-09-17,10:30:00,420",
            "NEPH-PM25": "Date_Time,PM2.5 (µg/m³)\n2026-09-17 10:30:00,5",
            "NO2-CAPS": "Concentration,Timestamp\n10,2026-09-17 10:30:00",
            "SMPS": "Scan Number,DateTime Sample Start,Total Concentration (#/cm³)\n1,17/09/2026 10:30:00,1000",
        }
        for instrument, csv in cases.items():
            s3 = FakeS3(silver={f"{instrument}/silver/{instrument}_data.csv": csv})
            stamp, _ = monitor.latest_reading(s3, instrument, {}, self.now)
            self.assertEqual("2026-09-17T17:30:00+00:00", stamp.isoformat(), instrument)


if __name__ == "__main__":
    unittest.main()
