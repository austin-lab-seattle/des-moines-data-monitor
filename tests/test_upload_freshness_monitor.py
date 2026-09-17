import importlib.util
import io
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class FakeNoSuchKey(Exception):
    pass


class FakePaginator:
    def __init__(self, s3):
        self.s3 = s3

    def paginate(self, Bucket, Prefix):
        return [{"Contents": [item for item in self.s3.objects if item["Key"].startswith(Prefix)]}]


class FakeS3:
    class exceptions:
        NoSuchKey = FakeNoSuchKey

    def __init__(self, objects=None, state=None):
        self.objects = objects or []
        self.state = state

    def get_paginator(self, name):
        return FakePaginator(self)

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        if self.state is None:
            return {}
        return {"Contents": [{"Key": Prefix}]}

    def get_object(self, Bucket, Key):
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

    def bronze(self, age_hours, key="NO2-CAPS/bronze/year=2026/month=09/batch.txt"):
        return {"Key": key, "Size": 42, "LastModified": self.now - timedelta(hours=age_hours)}

    def test_empty_bucket_remains_unarmed_and_sends_nothing(self):
        s3, ses = FakeS3(), FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertEqual("waiting-for-first-upload", result["status"])
        self.assertFalse(result["armed"])
        self.assertEqual([], ses.messages)
        self.assertIsNone(s3.state)

    def test_first_fresh_upload_arms_without_email(self):
        s3, ses = FakeS3([self.bronze(1)]), FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertEqual("healthy", result["status"])
        self.assertTrue(s3.state["armed"])
        self.assertEqual([], ses.messages)

    def test_stale_upload_sends_one_alert_not_hourly_spam(self):
        s3, ses = FakeS3([self.bronze(7)]), FakeSES()
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
        s3, ses = FakeS3([self.bronze(0.25)], state), FakeSES()
        result = monitor.run_monitor(s3, ses, self.now)
        self.assertTrue(result["recovery_sent"])
        self.assertEqual("healthy", s3.state["status"])
        self.assertEqual(1, len(ses.messages))


if __name__ == "__main__":
    unittest.main()
