import hashlib
import json
import os
import re
import sys
import types
import unittest


class FakeDynamoDb:
    def __init__(self):
        self.requests = {}
        self.keys = {}
        self.counters = {}

    def query(self, **_kwargs):
        return {"Items": []}

    def put_item(self, TableName, Item, **_kwargs):
        if TableName == "AQApiAccessRequests":
            self.requests[Item["request_id"]["S"]] = Item
        else:
            self.keys[Item["key_id"]["S"]] = Item

    def get_item(self, TableName, Key, **_kwargs):
        if TableName == "AQApiKeys":
            item = self.keys.get(Key["key_id"]["S"])
            return {"Item": item} if item else {}
        item = self.requests.get(Key["request_id"]["S"])
        return {"Item": item} if item else {}

    def transact_write_items(self, TransactItems):
        for operation in TransactItems:
            if "Put" in operation:
                item = operation["Put"]["Item"]
                self.keys[item["key_id"]["S"]] = item
                continue
            update = operation["Update"]
            request = self.requests[update["Key"]["request_id"]["S"]]
            values = update["ExpressionAttributeValues"]
            request["status"] = values[":active"]
            request["verified_at"] = values[":verified_at"]
            request["key_id"] = values[":key_id"]
            request.pop("verification_token_hash", None)

    def update_item(self, TableName, Key, **_kwargs):
        if TableName == "AQApiRateLimits":
            counter_id = Key["counter_id"]["S"]
            self.counters[counter_id] = self.counters.get(counter_id, 0) + 1


class FakeSes:
    def __init__(self):
        self.messages = []

    def send_email(self, **message):
        self.messages.append(message)

class FakeS3:
    pass


fake_dynamodb = FakeDynamoDb()
fake_ses = FakeSes()


def fake_client(service, **_kwargs):
    return {
        "s3": FakeS3(),
        "dynamodb": fake_dynamodb,
        "ses": fake_ses,
    }[service]


os.environ["ENABLE_API_KEY_REGISTRATION"] = "1"
os.environ["PUBLIC_API_KEY_REQUIRED"] = "1"
os.environ["ACCESS_REQUEST_FROM_EMAIL"] = "api-access@example.org"
os.environ["API_KEY_HASH_PEPPER"] = "unit-test-only-pepper"
sys.modules.setdefault("boto3", types.SimpleNamespace(client=fake_client))

import lambda_api


class ApiKeyAccessTests(unittest.TestCase):
    def setUp(self):
        fake_dynamodb.requests.clear()
        fake_dynamodb.keys.clear()
        fake_dynamodb.counters.clear()
        fake_ses.messages.clear()
        lambda_api.ENABLE_API_KEY_REGISTRATION = True
        lambda_api.PUBLIC_API_KEY_REQUIRED = True
        lambda_api.ACCESS_REQUEST_FROM_EMAIL = "api-access@example.org"
        lambda_api.API_KEY_HASH_PEPPER = "unit-test-only-pepper"

    def test_existing_active_keys_keep_working_after_human_approval_is_introduced(self):
        raw_key = "aqk_0123456789abcdef_abcdefghijklmnopqrstuvwxyzABCDEF1234567890_-"
        key_id = lambda_api.API_KEY_PATTERN.fullmatch(raw_key).group(1)
        fake_dynamodb.keys[key_id] = {"key_id": {"S": key_id}, "key_hash": {"S": lambda_api.key_hash(raw_key)},
            "status": {"S": "ACTIVE"}, "scopes": {"SS": list(lambda_api.READ_SCOPES.values())}}
        allowed = lambda_api.require_public_api_access(
            {"headers": {"x-api-key": raw_key}},
            "read:summary",
        )
        self.assertIsNone(allowed)
        self.assertEqual(len(fake_dynamodb.counters), 2)
        missing = lambda_api.require_public_api_access({"headers": {}}, "read:summary")
        self.assertEqual(missing["statusCode"], 401)

        lambda_api.PUBLIC_API_KEY_REQUIRED = False
        anonymous = lambda_api.require_public_api_access({"headers": {}}, "read:summary")
        self.assertIsNone(anonymous)
        invalid = lambda_api.require_public_api_access(
            {"headers": {"x-api-key": "definitely-wrong"}},
            "read:summary",
        )
        self.assertEqual(invalid["statusCode"], 403)

    def test_key_hash_is_one_way(self):
        raw_key = "aqk_0123456789abcdef_abcdefghijklmnopqrstuvwxyzABCDEF1234567890_-"
        digest = lambda_api.key_hash(raw_key)
        self.assertEqual(len(digest), hashlib.sha256().digest_size * 2)
        self.assertNotEqual(digest, raw_key)


if __name__ == "__main__":
    unittest.main()
