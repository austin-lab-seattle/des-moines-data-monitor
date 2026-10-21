"""Offline DynamoDB integration checks, isolated from legacy fake-boto tests.

No real credentials, AWS endpoints, emails or production records are used.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_DEFAULT_REGION="us-west-2", AWS_EC2_METADATA_DISABLED="true")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boto3
from moto import mock_aws


class Mailbox:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send_email(self, **kwargs):
        if self.fail:
            raise RuntimeError("CI-only mail failure")
        self.messages.append(kwargs)


class ApprovalFlowTests(unittest.TestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        import lambda_api
        self.api = lambda_api
        self.api.dynamodb_client = boto3.client("dynamodb", region_name="us-west-2")
        self.db = self.api.dynamodb_client
        self.mail = Mailbox()
        self.api.ses_client = self.mail
        self.api.ENABLE_API_KEY_REGISTRATION = True
        self.api.API_KEY_HASH_PEPPER = "CI-only-pepper-not-a-real-secret"
        self.api.ACCESS_REQUEST_FROM_EMAIL = "sender@example.org"
        self.api.ACCESS_REQUEST_APPROVERS = ("elena@example.org", "jeff@example.org", "yeon@example.org", "pavan@example.org")
        self.api.TEAM_CONSOLE_URL = "https://dashboard.example.org/team"
        self.api.PUBLIC_API_KEY_REQUIRED = True
        for name, primary in [(self.api.ACCESS_REQUESTS_TABLE, "request_id"), (self.api.API_KEYS_TABLE, "key_id"), (self.api.ADMIN_AUDIT_TABLE, "audit_id")]:
            args = dict(TableName=name, KeySchema=[{"AttributeName": primary, "KeyType": "HASH"}], AttributeDefinitions=[{"AttributeName": primary, "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
            if primary == "request_id":
                args["AttributeDefinitions"] += [{"AttributeName": "email", "AttributeType": "S"}, {"AttributeName": "status", "AttributeType": "S"}, {"AttributeName": "created_at_epoch", "AttributeType": "N"}]
                args["GlobalSecondaryIndexes"] = [{"IndexName": f"{field}-created-at-index", "KeySchema": [{"AttributeName": field, "KeyType": "HASH"}, {"AttributeName": "created_at_epoch", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}} for field in ("email", "status")]
            self.db.create_table(**args)

    def tearDown(self):
        self.mock.stop()

    def request(self):
        result = self.api.create_access_request({"headers": {"host": "api.example.org"}, "body": json.dumps({"name": "CI Researcher <img src=x>", "email": "researcher@example.org", "organization": "CI University", "use_case": "CI-only environmental analysis"})})
        self.assertEqual(result["statusCode"], 202, result)
        item = self.db.scan(TableName=self.api.ACCESS_REQUESTS_TABLE)["Items"][0]
        text = self.mail.messages[0]["Message"]["Body"]["Text"]["Data"]
        token = re.search(r"token=(aqv_[A-Za-z0-9_-]+)", text).group(1)
        return item["request_id"]["S"], token

    def verify(self, token):
        return self.api.verify_access_request({"body": urlencode({"token": token})})

    def verified(self):
        request_id, token = self.request()
        result = self.verify(token)
        self.assertEqual(result["statusCode"], 200, result)
        return request_id

    def item(self, request_id):
        return self.db.get_item(TableName=self.api.ACCESS_REQUESTS_TABLE, Key={"request_id": {"S": request_id}}, ConsistentRead=True)["Item"]

    def actor(self, request_id=None, decision="approve", email="elena@example.org", role="ApiApprover", verified="true"):
        event = {"rawPath": self.api.INTERNAL_API_ROUTES["access_decision"], "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {"sub": "ci-subject", "email": email, "email_verified": verified, "cognito:groups": [role]}}}}, "body": json.dumps({"request_id": request_id, "decision": decision})}
        return event

    def decide(self, request_id, **kwargs):
        return self.api.lambda_handler(self.actor(request_id, **kwargs), None)

    def keys(self):
        return self.db.scan(TableName=self.api.API_KEYS_TABLE)["Items"]

    def test_verified_request_notifies_exactly_four_approvers_but_issues_no_key(self):
        request_id = self.verified()
        self.assertEqual(self.item(request_id)["status"]["S"], "PENDING_APPROVAL")
        self.assertEqual(self.keys(), [])
        notification = self.mail.messages[1]
        self.assertEqual(notification["Destination"]["ToAddresses"], list(self.api.ACCESS_REQUEST_APPROVERS))
        body = notification["Message"]["Body"]
        self.assertIn(f"https://dashboard.example.org/team?request_id={request_id}", body["Text"]["Data"])
        self.assertIn("&lt;img", body["Html"]["Data"])
        self.assertNotIn("<img src=x>", body["Html"]["Data"])
        self.assertNotIn("aqk_", json.dumps(notification))

    def test_get_preview_does_not_verify_notify_or_issue(self):
        request_id, token = self.request()
        result = self.api.lambda_handler({"rawPath": self.api.API_ROUTES["verify_access"], "queryStringParameters": {"token": token}, "requestContext": {"http": {"method": "GET"}}}, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertIn('method="post"', result["body"])
        self.assertEqual(self.item(request_id)["status"]["S"], "PENDING_VERIFICATION")
        self.assertEqual(len(self.mail.messages), 1)
        self.assertEqual(self.keys(), [])

    def test_verified_approver_can_issue_once_only_to_verified_requester(self):
        request_id = self.verified()
        event = self.actor(request_id)
        payload = json.loads(event["body"])
        payload["email"] = "attacker@example.org"
        event["body"] = json.dumps(payload)
        result = self.api.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200, result)
        self.assertEqual(len(self.keys()), 1)
        key = self.keys()[0]
        self.assertEqual(key["status"]["S"], "ACTIVE")
        delivered = self.mail.messages[-1]
        self.assertEqual(delivered["Destination"]["ToAddresses"], ["researcher@example.org"])
        text = delivered["Message"]["Body"]["Text"]["Data"]
        raw_key = re.search(r"API key: (aqk_[A-Za-z0-9_-]+)", text).group(1)
        self.assertEqual(key["key_hash"]["S"], self.api.key_hash(raw_key))
        self.assertNotIn(raw_key, json.dumps(result))
        self.assertEqual(self.item(request_id)["approved_by"]["S"], "elena@example.org")
        self.assertEqual(self.decide(request_id)["statusCode"], 409)
        self.assertEqual(len(self.keys()), 1)
        self.assertEqual(len(self.mail.messages), 3)
        audit = self.db.scan(TableName=self.api.ADMIN_AUDIT_TABLE)["Items"][0]
        self.assertEqual(audit["result"]["S"], "SUCCEEDED")

    def test_all_four_configured_approvers_are_allowed(self):
        request_id = self.verified()
        for email in self.api.ACCESS_REQUEST_APPROVERS:
            identity, denied = self.api.require_access_approver(self.actor(request_id, email=email))
            self.assertIsNone(denied)
            self.assertEqual(identity["email"], email)

    def test_anonymous_wrong_role_unverified_email_and_unlisted_admin_are_denied(self):
        request_id = self.verified()
        for kwargs in ({"role": "Viewer"}, {"role": "Reviewer"}, {"verified": "false"}, {"verified": ""}, {"role": "Admin", "email": "outsider@example.org"}):
            self.assertEqual(self.decide(request_id, **kwargs)["statusCode"], 403)
        event = self.actor(request_id)
        event["requestContext"].pop("authorizer")
        self.assertEqual(self.api.lambda_handler(event, None)["statusCode"], 401)
        self.assertEqual(self.keys(), [])

    def test_api_approver_does_not_gain_user_management_or_record_write(self):
        for path, method in [(self.api.INTERNAL_API_ROUTES["api_users"], "GET"), (self.api.INTERNAL_API_ROUTES["api_users_revoke"], "POST"), (self.api.INTERNAL_API_ROUTES["flags"], "POST")]:
            event = self.actor()
            event["rawPath"] = path
            event["requestContext"]["http"]["method"] = method
            self.assertEqual(self.api.lambda_handler(event, None)["statusCode"], 403)

    def test_gateway_space_flattened_groups_can_review_with_existing_security_gates(self):
        for groups in ("[Admin ApiApprover]", "[ApiApprover Viewer]"):
            event = self.actor()
            event["rawPath"] = self.api.INTERNAL_API_ROUTES["access_requests"]
            event["requestContext"]["http"]["method"] = "GET"
            claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
            claims["cognito:groups"] = groups
            self.assertEqual(self.api.lambda_handler(event, None)["statusCode"], 200)
            claims["email_verified"] = "false"
            self.assertEqual(self.api.lambda_handler(event, None)["statusCode"], 403)
            claims["email_verified"] = "true"
            claims["email"] = "unlisted@example.org"
            self.assertEqual(self.api.lambda_handler(event, None)["statusCode"], 403)

    def test_pending_verification_cannot_be_approved(self):
        request_id, _ = self.request()
        self.assertEqual(self.decide(request_id)["statusCode"], 409)
        self.assertEqual(self.keys(), [])

    def test_expired_request_is_denied_even_before_table_ttl_cleanup(self):
        request_id = self.verified()
        self.db.update_item(TableName=self.api.ACCESS_REQUESTS_TABLE, Key={"request_id": {"S": request_id}}, UpdateExpression="SET approval_expires_epoch = :expired", ExpressionAttributeValues={":expired": {"N": str(int(time.time()) - 1)}})
        self.assertEqual(self.decide(request_id)["statusCode"], 410)
        self.assertEqual(self.keys(), [])

    def test_rejection_never_issues_a_key_and_cannot_be_replayed_as_approval(self):
        request_id = self.verified()
        self.assertEqual(self.decide(request_id, decision="reject")["statusCode"], 200)
        self.assertEqual(self.item(request_id)["status"]["S"], "REJECTED")
        self.assertEqual(self.decide(request_id)["statusCode"], 409)
        self.assertEqual(self.keys(), [])

    def test_delivery_failure_revokes_unusable_key_and_allows_explicit_retry(self):
        request_id = self.verified()
        self.mail.fail = True
        self.assertEqual(self.decide(request_id)["statusCode"], 503)
        self.assertEqual(self.keys()[0]["status"]["S"], "REVOKED")
        self.assertEqual(self.item(request_id)["status"]["S"], "PENDING_APPROVAL")
        self.mail.fail = False
        self.assertEqual(self.decide(request_id)["statusCode"], 200)
        self.assertEqual(sum(key["status"]["S"] == "ACTIVE" for key in self.keys()), 1)

    def test_notification_failure_can_retry_without_creating_any_key(self):
        request_id, token = self.request()
        self.mail.fail = True
        self.assertEqual(self.verify(token)["statusCode"], 503)
        self.assertEqual(self.item(request_id)["status"]["S"], "PENDING_APPROVAL")
        self.mail.fail = False
        self.assertEqual(self.verify(token)["statusCode"], 200)
        self.assertEqual(len(self.mail.messages), 2)
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.verify(token)["statusCode"], 400)

    def test_audit_failure_fails_closed_before_key_or_mail(self):
        request_id = self.verified()
        with patch.object(self.api, "begin_audit_event", return_value=None):
            self.assertEqual(self.decide(request_id)["statusCode"], 503)
        self.assertEqual(self.keys(), [])
        self.assertEqual(len(self.mail.messages), 2)

    def test_internal_list_redacts_hashes_and_get_decision_never_approves(self):
        request_id = self.verified()
        event = self.actor(request_id)
        event["requestContext"]["http"]["method"] = "GET"
        self.assertEqual(self.api.lambda_handler(event, None)["statusCode"], 405)
        event["rawPath"] = self.api.INTERNAL_API_ROUTES["access_requests"]
        event["queryStringParameters"] = {"request_id": request_id}
        result = self.api.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200, result)
        self.assertIn("CI-only environmental analysis", result["body"])
        self.assertNotIn("token_hash", result["body"])
        self.assertNotIn("key_hash", result["body"])
        self.assertEqual(self.keys(), [])

    def test_absent_approver_configuration_cannot_fall_back_to_auto_issuance(self):
        self.api.ACCESS_REQUEST_APPROVERS = ()
        self.assertFalse(self.api.access_registration_ready())
        self.assertEqual(self.api.create_access_request({"body": "{}"})["statusCode"], 503)

    def test_secrets_are_not_logged_or_returned(self):
        request_id = self.verified()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.decide(request_id)
        self.assertEqual(result["statusCode"], 200)
        self.assertNotIn("aqk_", output.getvalue())
        self.assertNotIn("aqv_", output.getvalue())
        self.assertNotIn("aqk_", result["body"])

    def test_competing_approvals_are_atomic_and_cannot_create_a_second_key(self):
        request_id = self.verified()
        real_transaction = self.db.transact_write_items
        def race(**kwargs):
            # Another approver has claimed the request between our read and
            # transaction. The losing transaction must not insert its key.
            self.db.update_item(TableName=self.api.ACCESS_REQUESTS_TABLE, Key={"request_id": {"S": request_id}},
                UpdateExpression="SET #status = :other", ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":other": {"S": "APPROVING"}})
            return real_transaction(**kwargs)
        with patch.object(self.db, "transact_write_items", side_effect=race):
            self.assertEqual(self.decide(request_id)["statusCode"], 409)
        self.assertEqual(self.keys(), [])
        self.assertEqual(len(self.mail.messages), 2)

    def test_pending_delivery_key_is_unusable_until_email_delivery_is_accepted(self):
        request_id = self.verified()
        real_send = self.mail.send_email
        def check_before_send(**message):
            key = self.keys()[0]
            self.assertEqual(key["status"]["S"], "PENDING_DELIVERY")
            raw = re.search(r"API key: (aqk_[A-Za-z0-9_-]+)", message["Message"]["Body"]["Text"]["Data"]).group(1)
            self.assertEqual(self.api.require_public_api_access({"headers": {"x-api-key": raw}}, "read:summary")["statusCode"], 403)
            return real_send(**message)
        with patch.object(self.mail, "send_email", side_effect=check_before_send):
            self.assertEqual(self.decide(request_id)["statusCode"], 200)

    def test_broken_verification_expired_token_and_invalid_post_do_not_notify(self):
        request_id, token = self.request()
        self.db.update_item(TableName=self.api.ACCESS_REQUESTS_TABLE, Key={"request_id": {"S": request_id}},
            UpdateExpression="SET verification_expires_epoch = :expired", ExpressionAttributeValues={":expired": {"N": "1"}})
        self.assertEqual(self.verify(token)["statusCode"], 400)
        self.assertEqual(self.verify("aqv_invalid")["statusCode"], 400)
        self.assertEqual(self.api.create_access_request({"body": "[]"})["statusCode"], 400)
        self.assertEqual(len(self.mail.messages), 1)
        self.assertEqual(self.keys(), [])

    def test_pre_release_verification_link_can_enter_manual_review_without_auto_issuance(self):
        request_id, token = self.request()
        self.db.update_item(TableName=self.api.ACCESS_REQUESTS_TABLE, Key={"request_id": {"S": request_id}}, UpdateExpression="REMOVE approval_expires_epoch")
        self.assertEqual(self.verify(token)["statusCode"], 200)
        self.assertGreater(int(self.item(request_id)["approval_expires_epoch"]["N"]), time.time())
        self.assertEqual(self.keys(), [])

    def test_email_links_ignore_untrusted_host_and_proxy_headers(self):
        message = self.api.verification_email({"email": {"S": "researcher@example.org"}}, "aqv_ci_only",
            {"headers": {"host": "attacker.example.org", "x-forwarded-proto": "http"}})
        text = message["Message"]["Body"]["Text"]["Data"]
        self.assertIn("https://dashboard.example.org/air-quality/v1/access-requests/verify", text)
        self.assertNotIn("attacker.example.org", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
