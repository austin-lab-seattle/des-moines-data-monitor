import json
import unittest

import lambda_api


def event(path, method="GET", groups=None, body=None):
    claims = {
        "sub": "uw-subject-123",
        "email": "reviewer@uw.edu",
    }
    if groups is not None:
        claims["cognito:groups"] = groups
    value = {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims}},
        },
    }
    if body is not None:
        value["body"] = json.dumps(body)
    return value


class FakeDynamoDb:
    def __init__(self):
        self.audit = {}

    def put_item(self, TableName, Item, **_kwargs):
        assert TableName == "AQAdminAudit"
        self.audit[Item["audit_id"]["S"]] = Item

    def update_item(self, TableName, Key, ExpressionAttributeValues, **_kwargs):
        assert TableName == "AQAdminAudit"
        item = self.audit[Key["audit_id"]["S"]]
        item["result"] = ExpressionAttributeValues[":result"]
        item["completed_at"] = ExpressionAttributeValues[":completed_at"]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **_kwargs):
        self.objects[(Bucket, Key)] = json.loads(Body)


class TeamAccessTests(unittest.TestCase):
    def setUp(self):
        self.original_ddb = lambda_api.dynamodb_client
        self.original_s3 = lambda_api.s3_client
        self.ddb = FakeDynamoDb()
        self.s3 = FakeS3()
        lambda_api.dynamodb_client = self.ddb
        lambda_api.s3_client = self.s3

    def tearDown(self):
        lambda_api.dynamodb_client = self.original_ddb
        lambda_api.s3_client = self.original_s3

    def test_team_identity_requires_verified_gateway_claims(self):
        missing = lambda_api.lambda_handler({
            "rawPath": "/air-quality/internal/v1/api-users",
            "requestContext": {"http": {"method": "GET"}},
        }, None)
        self.assertEqual(missing["statusCode"], 401)

        denied = lambda_api.lambda_handler(
            event("/air-quality/internal/v1/api-users", groups=["Reviewer"]),
            None,
        )
        self.assertEqual(denied["statusCode"], 403)

    def test_group_claim_string_is_normalized(self):
        identity = lambda_api.team_identity(event("/", groups="[Admin,Reviewer]"))
        self.assertEqual(identity["roles"], {"Admin", "Reviewer"})

    def test_reviewer_flag_keeps_source_immutable_and_records_actor(self):
        result = lambda_api.lambda_handler(
            event(
                "/air-quality/internal/v1/flags",
                method="POST",
                groups=["Reviewer"],
                body={
                    "instrument_id": "NO2-CAPS",
                    "scope": "selected_rows",
                    "row_key": "row-123",
                    "timestamp": "2026/03/03 00:00:10.000",
                    "reason": "Instrument diagnostic indicates an invalid reading",
                    "notes": "Reviewed against the field log.",
                },
            ),
            None,
        )
        self.assertEqual(result["statusCode"], 201)
        saved = next(iter(self.s3.objects.values()))
        self.assertEqual(saved["created_by"], "reviewer@uw.edu")
        self.assertEqual(saved["row_key"], "row-123")
        self.assertNotIn("corrected_values", saved)
        audit = next(iter(self.ddb.audit.values()))
        self.assertEqual(audit["action"]["S"], "CREATE_FLAG")
        self.assertEqual(audit["result"]["S"], "SUCCEEDED")

    def test_flag_requires_reason(self):
        result = lambda_api.write_review_item(
            event("/", method="POST", groups=["Reviewer"], body={
                "instrument_id": "NO2-CAPS",
                "scope": "selected_rows",
                "row_key": "row-123",
            }),
            "flags",
            {"email": "reviewer@uw.edu", "subject": "uw-subject-123", "roles": {"Reviewer"}},
        )
        self.assertEqual(result["statusCode"], 400)
        self.assertFalse(self.s3.objects)
        self.assertFalse(self.ddb.audit)


if __name__ == "__main__":
    unittest.main()
