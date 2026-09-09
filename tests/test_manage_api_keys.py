import contextlib
import io
import unittest

from scripts import manage_api_keys


class FakeDynamoDb:
    def __init__(self):
        self.requests = {
            "request-1": {
                "request_id": {"S": "request-1"},
                "email": {"S": "researcher@example.org"},
                "name": {"S": "Example Researcher"},
                "status": {"S": "PENDING_APPROVAL"},
            }
        }
        self.keys = {}

    def get_item(self, TableName, Key, **_kwargs):
        if TableName == manage_api_keys.ACCESS_REQUESTS_TABLE:
            return {"Item": self.requests.get(Key["request_id"]["S"])}
        return {"Item": self.keys.get(Key["key_id"]["S"])}

    def transact_write_items(self, TransactItems):
        for operation in TransactItems:
            if "Put" in operation:
                item = operation["Put"]["Item"]
                self.keys[item["key_id"]["S"]] = item
                continue

            update = operation["Update"]
            values = update["ExpressionAttributeValues"]
            if update["TableName"] == manage_api_keys.API_KEYS_TABLE:
                key = self.keys[update["Key"]["key_id"]["S"]]
                key["status"] = values[":revoked"]
                key["revoked_at"] = values[":revoked_at"]
                key["revocation_reason"] = values[":reason"]
            elif ":approved_at" in values:
                request = self.requests[update["Key"]["request_id"]["S"]]
                request["status"] = values[":approved"]
                request["approved_at"] = values[":approved_at"]
                request["key_id"] = values[":key_id"]
            else:
                request = self.requests[update["Key"]["request_id"]["S"]]
                request["status"] = values[":pending"]
                request.pop("approved_at", None)
                request.pop("key_id", None)


class FakeSes:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    def send_email(self, **message):
        if self.fail:
            raise RuntimeError("simulated SES failure")
        self.messages.append(message)


class ManageApiKeysTests(unittest.TestCase):
    def setUp(self):
        manage_api_keys.API_KEY_HASH_PEPPER = "unit-test-only-pepper"
        manage_api_keys.ACCESS_REQUEST_FROM_EMAIL = "api-access@example.org"

    def test_approval_emails_key_to_verified_address_without_printing_it(self):
        ddb = FakeDynamoDb()
        ses = FakeSes()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            manage_api_keys.approve_request(ddb, ses, "request-1")

        self.assertEqual(len(ses.messages), 1)
        message = ses.messages[0]
        self.assertEqual(message["Destination"]["ToAddresses"], ["researcher@example.org"])
        email_text = message["Message"]["Body"]["Text"]["Data"]
        key_id, key = next(iter(ddb.keys.items()))
        self.assertNotIn(key["key_hash"]["S"], email_text)
        self.assertIn(f"aqk_{key_id}_", email_text)
        self.assertNotIn(f"aqk_{key_id}_", output.getvalue())
        self.assertEqual(ddb.requests["request-1"]["status"]["S"], "APPROVED")

    def test_email_failure_revokes_key_and_returns_request_to_queue(self):
        ddb = FakeDynamoDb()
        ses = FakeSes(fail=True)

        with self.assertRaisesRegex(RuntimeError, "returned to PENDING_APPROVAL"):
            manage_api_keys.approve_request(ddb, ses, "request-1")

        key = next(iter(ddb.keys.values()))
        self.assertEqual(key["status"]["S"], "REVOKED")
        self.assertEqual(key["revocation_reason"]["S"], "EMAIL_DELIVERY_FAILED")
        self.assertEqual(ddb.requests["request-1"]["status"]["S"], "PENDING_APPROVAL")
        self.assertNotIn("key_id", ddb.requests["request-1"])


if __name__ == "__main__":
    unittest.main()
