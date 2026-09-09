import hashlib
import json
import os
import re
import sys
import types
import unittest
from urllib.parse import parse_qs, urlparse


class FakeDynamoDb:
    def __init__(self):
        self.requests = {}
        self.keys = {}

    def query(self, **_kwargs):
        return {"Items": []}

    def put_item(self, TableName, Item, **_kwargs):
        if TableName == "AQApiAccessRequests":
            self.requests[Item["request_id"]["S"]] = Item
        else:
            self.keys[Item["key_id"]["S"]] = Item

    def delete_item(self, TableName, Key):
        if TableName == "AQApiAccessRequests":
            self.requests.pop(Key["request_id"]["S"], None)

    def update_item(self, TableName, Key, ExpressionAttributeValues, **_kwargs):
        request = self.requests[Key["request_id"]["S"]]
        supplied_hash = ExpressionAttributeValues[":token_hash"]["S"]
        self.assert_token_matches(request, supplied_hash)
        request["status"] = ExpressionAttributeValues[":approved"]
        request["verified_at"] = ExpressionAttributeValues[":verified_at"]
        request.pop("verification_token_hash", None)
        return {}

    def get_item(self, TableName, Key, **_kwargs):
        if TableName == "AQApiKeys":
            item = self.keys.get(Key["key_id"]["S"])
            return {"Item": item} if item else {}
        item = self.requests.get(Key["request_id"]["S"])
        return {"Item": item} if item else {}

    @staticmethod
    def assert_token_matches(request, supplied_hash):
        if request["verification_token_hash"]["S"] != supplied_hash:
            raise ValueError("verification token mismatch")


class FakeSes:
    def __init__(self):
        self.messages = []

    def send_email(self, **kwargs):
        self.messages.append(kwargs)


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
        fake_ses.messages.clear()
        lambda_api.ENABLE_API_KEY_REGISTRATION = True
        lambda_api.PUBLIC_API_KEY_REQUIRED = True
        lambda_api.ACCESS_REQUEST_FROM_EMAIL = "api-access@example.org"
        lambda_api.API_KEY_HASH_PEPPER = "unit-test-only-pepper"

    def test_request_verification_and_hashed_key_validation(self):
        request_event = {
            "headers": {"host": "api.example.org", "x-forwarded-proto": "https"},
            "body": json.dumps({
                "name": "Example Researcher",
                "email": "researcher@example.org",
                "organization": "Example University",
                "use_case": "Environmental health analysis",
            }),
        }
        created = lambda_api.create_access_request(request_event)
        self.assertEqual(created["statusCode"], 202)
        self.assertEqual(len(fake_ses.messages), 1)

        request = next(iter(fake_dynamodb.requests.values()))
        self.assertIn("verification_token_hash", request)
        self.assertNotIn("verification_token", request)
        email_text = fake_ses.messages[0]["Message"]["Body"]["Text"]["Data"]
        verification_url = re.search(r"https://\S+", email_text).group(0)
        query = parse_qs(urlparse(verification_url).query)
        verified = lambda_api.verify_access_request({
            "queryStringParameters": {
                "request_id": query["request_id"][0],
                "token": query["token"][0],
            }
        })
        self.assertEqual(verified["statusCode"], 200)
        self.assertEqual(request["status"]["S"], "PENDING_APPROVAL")
        self.assertNotIn("verification_token_hash", request)

        raw_key = "aqk_0123456789abcdef_abcdefghijklmnopqrstuvwxyzABCDEF1234567890_-"
        fake_dynamodb.keys["0123456789abcdef"] = {
            "key_id": {"S": "0123456789abcdef"},
            "status": {"S": "ACTIVE"},
            "key_hash": {"S": lambda_api.key_hash(raw_key)},
            "scopes": {"SS": ["read:summary"]},
        }
        allowed = lambda_api.require_public_api_access(
            {"headers": {"x-api-key": raw_key}},
            "read:summary",
        )
        self.assertIsNone(allowed)
        missing = lambda_api.require_public_api_access({"headers": {}}, "read:summary")
        self.assertEqual(missing["statusCode"], 401)

    def test_key_hash_is_one_way(self):
        raw_key = "aqk_0123456789abcdef_abcdefghijklmnopqrstuvwxyzABCDEF1234567890_-"
        digest = lambda_api.key_hash(raw_key)
        self.assertEqual(len(digest), hashlib.sha256().digest_size * 2)
        self.assertNotEqual(digest, raw_key)


if __name__ == "__main__":
    unittest.main()
