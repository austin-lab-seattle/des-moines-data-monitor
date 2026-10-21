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
        self.api.ENABLE_EMAIL_LINK_APPROVAL = False
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


class EmailLinkApprovalTests(unittest.TestCase):
    # Reuse only the offline setup/helpers, not the legacy notification tests.
    request = ApprovalFlowTests.request
    verify = ApprovalFlowTests.verify
    verified = ApprovalFlowTests.verified
    item = ApprovalFlowTests.item
    actor = ApprovalFlowTests.actor
    decide = ApprovalFlowTests.decide
    keys = ApprovalFlowTests.keys
    tearDown = ApprovalFlowTests.tearDown

    def setUp(self):
        ApprovalFlowTests.setUp(self)
        self.api.ENABLE_EMAIL_LINK_APPROVAL = True
        self.api.ACCESS_REQUEST_APPROVERS = ("elena@example.org", "yeon@example.org", "pavan@example.org")
        self.db.create_table(TableName=self.api.RATE_LIMITS_TABLE,
            KeySchema=[{"AttributeName": "counter_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "counter_id", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")

    def links(self):
        result = {}
        for message in self.mail.messages[1:]:
            text = message["Message"]["Body"]["Text"]["Data"]
            for decision in ("approve", "reject"):
                token = re.search(rf"{decision.title()}: https://[^\s]+\?token=(aqa_[A-Za-z0-9_-]+)", text)
                if token:
                    result[message["Destination"]["ToAddresses"][0], decision] = token.group(1)
        return result

    def link_event(self, token, method="GET", **fields):
        return {"rawPath": self.api.API_ROUTES["email_decision"],
            "queryStringParameters": {"token": token}, "body": urlencode({"token": token} | fields),
            "requestContext": {"http": {"method": method, "sourceIp": "192.0.2.1"}},
            "headers": {"host": "attacker.example.org", "x-forwarded-proto": "http"}}

    def open(self, token, method="GET", **fields):
        return self.api.lambda_handler(self.link_event(token, method, **fields), None)

    def test_separate_single_recipient_links_are_scoped_hashed_and_exclude_jeff(self):
        request_id = self.verified()
        links = self.links()
        self.assertEqual(len(links), 6)
        self.assertEqual(len(set(links.values())), 6)
        self.assertEqual(len(self.mail.messages), 4)
        self.assertNotIn("jeff@example.org", json.dumps(self.mail.messages))
        item = self.item(request_id)
        self.assertEqual(len(item["email_approval_tokens"]["M"]), 6)
        self.assertLessEqual(int(item["email_approval_expires"]["N"]), int(time.time()) + 86400)
        for (recipient, decision), token in links.items():
            self.assertNotIn(token, json.dumps(item))
            auth = item["email_approval_tokens"]["M"][self.api.key_hash(token)]["M"]
            self.assertEqual(auth["recipient"]["S"], recipient)
            self.assertEqual(auth["decision"]["S"], decision)
            message = next(m for m in self.mail.messages[1:] if m["Destination"]["ToAddresses"] == [recipient])
            self.assertIn("&lt;img", message["Message"]["Body"]["Html"]["Data"])
            self.assertIn("Do not forward", message["Message"]["Body"]["Text"]["Data"])
        self.assertEqual(self.keys(), [])

    def test_repeated_get_scanner_previews_do_not_consume_notify_audit_or_issue(self):
        request_id = self.verified()
        before = self.item(request_id)
        for token in self.links().values():
            for _ in range(2):
                result = self.open(token)
                self.assertEqual(result["statusCode"], 200, result)
                self.assertIn('method="post"', result["body"])
                self.assertIn("Confirm:", result["body"])
                self.assertIn("&lt;img", result["body"])
                self.assertNotIn("<img src=x>", result["body"])
                self.assertEqual(result["headers"]["Cache-Control"], "no-store")
                self.assertEqual(result["headers"]["Referrer-Policy"], "no-referrer")
                self.assertIn("frame-ancestors 'none'", result["headers"]["Content-Security-Policy"])
        self.assertEqual(self.item(request_id), before)
        self.assertEqual(self.keys(), [])
        self.assertEqual(len(self.mail.messages), 4)
        self.assertEqual(self.db.scan(TableName=self.api.ADMIN_AUDIT_TABLE)["Count"], 0)

    def test_approval_works_without_session_ignores_supplied_identity_and_consumes_all_links(self):
        request_id = self.verified()
        links = self.links()
        token = links["yeon@example.org", "approve"]
        result = self.open(token, "POST", email="attacker@example.org", request_id="another-request", decision="reject")
        self.assertEqual(result["statusCode"], 200, result)
        self.assertEqual(self.item(request_id)["status"]["S"], "ACTIVE")
        self.assertEqual(self.item(request_id)["approved_by"]["S"], "yeon@example.org")
        self.assertNotIn("email_approval_tokens", self.item(request_id))
        self.assertEqual(len(self.keys()), 1)
        self.assertEqual(self.mail.messages[-1]["Destination"]["ToAddresses"], ["researcher@example.org"])
        self.assertNotIn("aqk_", result["body"])
        audit = self.db.scan(TableName=self.api.ADMIN_AUDIT_TABLE)["Items"][0]
        self.assertEqual(audit["actor_subject"]["S"], "email-link:yeon@example.org")
        self.assertEqual(json.loads(audit["details"]["S"]), {"authorization_method": "email_link", "identity_verified": False})
        for sibling in links.values():
            self.assertEqual(self.open(sibling)["statusCode"], 400)
            self.assertEqual(self.open(sibling, "POST")["statusCode"], 400)
        self.assertEqual(len(self.keys()), 1)

    def test_reject_link_cannot_be_switched_to_approval(self):
        request_id = self.verified()
        token = self.links()["elena@example.org", "reject"]
        result = self.open(token, "POST", decision="approve")
        self.assertEqual(result["statusCode"], 200, result)
        self.assertEqual(self.item(request_id)["status"]["S"], "REJECTED")
        self.assertEqual(self.keys(), [])
        self.assertEqual(len(self.mail.messages), 4)

    def test_invalid_expired_disabled_removed_recipient_and_unverified_requests_fail_closed(self):
        request_id = self.verified()
        token = self.links()["elena@example.org", "approve"]
        self.assertEqual(self.open(token[:-1] + ("x" if token[-1] != "x" else "y"), "POST")["statusCode"], 400)
        self.assertEqual(self.open("aqa_invalid", "POST")["statusCode"], 400)
        self.assertEqual(self.open("", "POST")["statusCode"], 400)
        self.api.ENABLE_EMAIL_LINK_APPROVAL = False
        self.assertEqual(self.open(token, "POST")["statusCode"], 503)
        self.api.ENABLE_EMAIL_LINK_APPROVAL = True
        self.api.ACCESS_REQUEST_APPROVERS = ("yeon@example.org",)
        self.assertEqual(self.open(token, "POST")["statusCode"], 400)
        self.api.ACCESS_REQUEST_APPROVERS = ("elena@example.org",)
        self.db.update_item(TableName=self.api.ACCESS_REQUESTS_TABLE, Key={"request_id": {"S": request_id}},
            UpdateExpression="REMOVE verified_at")
        self.assertEqual(self.open(token, "POST")["statusCode"], 400)
        self.db.update_item(TableName=self.api.ACCESS_REQUESTS_TABLE, Key={"request_id": {"S": request_id}},
            UpdateExpression="SET verified_at = :now, email_approval_tokens.#hash.#expires = :past",
            ExpressionAttributeNames={"#hash": self.api.key_hash(token), "#expires": "expires"},
            ExpressionAttributeValues={":now": {"S": "CI-only"}, ":past": {"N": str(int(time.time()) - 1)}})
        self.assertEqual(self.open(token)["statusCode"], 400)
        self.assertEqual(self.open(token, "POST")["statusCode"], 400)
        self.assertEqual(self.keys(), [])

    def test_notification_partial_retry_preserves_links_already_received(self):
        request_id, verify_token = self.request()
        send = self.mail.send_email
        def partial(**message):
            if message["Destination"]["ToAddresses"] == ["yeon@example.org"]:
                raise RuntimeError("CI-only delivery failure")
            return send(**message)
        with patch.object(self.mail, "send_email", side_effect=partial):
            self.assertEqual(self.verify(verify_token)["statusCode"], 503)
        first = self.links()["elena@example.org", "approve"]
        self.assertEqual(self.open(first)["statusCode"], 200)
        self.assertEqual(self.verify(verify_token)["statusCode"], 200)
        self.assertEqual(self.links()["elena@example.org", "approve"], first)
        self.assertEqual(self.open(first, "POST")["statusCode"], 200)
        self.assertEqual(len(self.keys()), 1)

    def test_delivery_failure_consumes_links_and_only_console_can_retry(self):
        request_id = self.verified()
        token = self.links()["elena@example.org", "approve"]
        self.mail.fail = True
        self.assertEqual(self.open(token, "POST")["statusCode"], 503)
        self.assertEqual(self.keys()[0]["status"]["S"], "REVOKED")
        self.assertEqual(self.item(request_id)["status"]["S"], "PENDING_APPROVAL")
        self.mail.fail = False
        self.assertEqual(self.open(token, "POST")["statusCode"], 400)
        self.assertEqual(self.decide(request_id)["statusCode"], 200)
        self.assertEqual(sum(k["status"]["S"] == "ACTIVE" for k in self.keys()), 1)

    def test_token_revoked_between_read_and_transaction_cannot_insert_key(self):
        request_id = self.verified()
        token = self.links()["elena@example.org", "approve"]
        transaction = self.db.transact_write_items
        def revoke_then_transact(**kwargs):
            self.db.update_item(TableName=self.api.ACCESS_REQUESTS_TABLE, Key={"request_id": {"S": request_id}}, UpdateExpression="REMOVE email_approval_tokens")
            return transaction(**kwargs)
        with patch.object(self.db, "transact_write_items", side_effect=revoke_then_transact):
            self.assertEqual(self.open(token, "POST")["statusCode"], 409)
        self.assertEqual(self.keys(), [])
        self.assertEqual(len(self.mail.messages), 4)

    def test_console_decision_racing_email_cannot_issue_twice(self):
        request_id = self.verified()
        token = self.links()["elena@example.org", "approve"]
        transaction = self.db.transact_write_items
        raced = False
        def console_wins(**kwargs):
            nonlocal raced
            if not raced:
                raced = True
                self.assertEqual(self.decide(request_id)["statusCode"], 200)
            return transaction(**kwargs)
        with patch.object(self.db, "transact_write_items", side_effect=console_wins):
            self.assertEqual(self.open(token, "POST")["statusCode"], 409)
        self.assertEqual(len(self.keys()), 1)
        self.assertEqual(len(self.mail.messages), 5)

    def test_rate_or_audit_failure_cannot_issue_key_or_consume_link(self):
        request_id = self.verified()
        token = self.links()["elena@example.org", "approve"]
        with patch.object(self.api, "increment_rate_counter", return_value=self.api.response(429, {"error": "Rate limited"})):
            self.assertEqual(self.open(token, "POST")["statusCode"], 429)
        with patch.object(self.api, "begin_audit_event", return_value=None):
            self.assertEqual(self.open(token, "POST")["statusCode"], 503)
        self.assertIn("email_approval_tokens", self.item(request_id))
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.open(token)["statusCode"], 200)

    def test_links_never_leak_into_logs_or_queue_and_post_requires_body_token(self):
        request_id = self.verified()
        token = self.links()["elena@example.org", "approve"]
        event = self.link_event(token, "POST")
        event["body"] = ""
        self.assertEqual(self.api.lambda_handler(event, None)["statusCode"], 400)
        event["body"] = "x" * 5000
        self.assertEqual(self.api.lambda_handler(event, None)["statusCode"], 400)
        self.assertEqual(self.open(token, "HEAD")["statusCode"], 405)
        event = self.actor(request_id)
        event["rawPath"] = self.api.INTERNAL_API_ROUTES["access_requests"]
        event["requestContext"]["http"]["method"] = "GET"
        result = self.api.lambda_handler(event, None)
        self.assertNotIn("email_approval", result["body"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.open(token, "POST")
        self.assertNotIn("aqa_", output.getvalue())
        self.assertNotIn("aqk_", result["body"])
        self.assertEqual(result["statusCode"], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
