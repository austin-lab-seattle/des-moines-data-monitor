#!/usr/bin/env python3
"""Private operator commands for API access requests and issued API keys.

This script is deliberately not exposed through the public dashboard or API.
It uses the operator's AWS identity to review access requests, issue a read-only
key once, email it automatically, and revoke a key when needed.

Examples:
    python3 scripts/manage_api_keys.py list-pending
    python3 scripts/manage_api_keys.py approve --request-id <request-id>
    python3 scripts/manage_api_keys.py list-active
    python3 scripts/manage_api_keys.py revoke --key-id <key-id>

Set API_KEY_HASH_PEPPER and ACCESS_REQUEST_FROM_EMAIL in the operator's secure
shell or secret manager before issuing a key. Approved keys are emailed only to
the address on the approved request. Never put secrets in source code,
docs, or a committed .env file.
"""

import argparse
import hashlib
import hmac
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone
import re

DEFAULT_REGION = "us-west-2"
ACCESS_REQUESTS_TABLE = os.environ.get("ACCESS_REQUESTS_TABLE", "AQApiAccessRequests")
API_KEYS_TABLE = os.environ.get("API_KEYS_TABLE", "AQApiKeys")
API_KEY_HASH_PEPPER = os.environ.get("API_KEY_HASH_PEPPER")
ACCESS_REQUEST_FROM_EMAIL = os.environ.get("ACCESS_REQUEST_FROM_EMAIL")
READ_SCOPES = ["read:summary", "read:timeseries", "read:observations", "read:export"]
KEY_ID_PATTERN = re.compile(r"^[a-f0-9]{16}$")


def clients():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required. Install dependencies with: python3 -m pip install -r requirements.txt") from exc
    region = os.environ.get("DEPLOY_AWS_REGION", DEFAULT_REGION)
    session = boto3.Session(region_name=region)
    return session.client("dynamodb"), session.client("ses")


def string_value(item, field, default=""):
    return (item.get(field) or {}).get("S", default)


def number_value(item, field, default=0):
    try:
        return int((item.get(field) or {}).get("N", default))
    except (TypeError, ValueError):
        return default


def require_pepper():
    if not API_KEY_HASH_PEPPER:
        raise RuntimeError(
            "API_KEY_HASH_PEPPER is required to issue keys. Set it only in a secure shell or secret manager."
        )


def require_email_sender():
    if not ACCESS_REQUEST_FROM_EMAIL:
        raise RuntimeError(
            "ACCESS_REQUEST_FROM_EMAIL is required to deliver approved keys. "
            "Use an SES-verified sender address."
        )


def hash_key(raw_key):
    return hmac.new(
        API_KEY_HASH_PEPPER.encode("utf-8"),
        raw_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def api_key_id(value):
    key_id = str(value).strip().lower()
    if not KEY_ID_PATTERN.fullmatch(key_id):
        raise argparse.ArgumentTypeError("Key ID must be a 16-character lowercase hexadecimal value.")
    return key_id


def list_requests(ddb):
    result = ddb.query(
        TableName=ACCESS_REQUESTS_TABLE,
        IndexName="status-created-at-index",
        KeyConditionExpression="#status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": {"S": "PENDING_APPROVAL"}},
        ScanIndexForward=True,
    )
    items = result.get("Items", [])
    if not items:
        print("No access requests are waiting for approval.")
        return

    print("Access requests waiting for approval:\n")
    for item in items:
        print(f"Request:      {string_value(item, 'request_id')}")
        print(f"Email:        {string_value(item, 'email')}")
        print(f"Name:         {string_value(item, 'name')}")
        print(f"Organization: {string_value(item, 'organization') or 'Not provided'}")
        print(f"Intended use: {string_value(item, 'use_case')}")
        print(f"Submitted:    {string_value(item, 'created_at')}")
        print()


def key_delivery_email(request, raw_key, key_id):
    recipient = string_value(request, "email")
    name = string_value(request, "name") or "researcher"
    scopes = ", ".join(READ_SCOPES)
    text = (
        f"Hello {name},\n\n"
        "Your Des Moines Air Quality API request has been approved.\n\n"
        f"API key: {raw_key}\n"
        f"Key ID: {key_id}\n"
        f"Scopes: {scopes}\n\n"
        "Send the key in the x-api-key request header. Do not place it in a URL, "
        "repository, notebook, screenshot, or shared document. Store it in a "
        "secret manager or environment variable. This key is read-only and cannot "
        "change or flag observations.\n\n"
        "If you did not request this access, contact the project team so the key can "
        "be revoked.\n"
    )
    return {
        "Source": ACCESS_REQUEST_FROM_EMAIL,
        "Destination": {"ToAddresses": [recipient]},
        "Message": {
            "Subject": {"Data": "Your Des Moines Air Quality API key", "Charset": "UTF-8"},
            "Body": {"Text": {"Data": text, "Charset": "UTF-8"}},
        },
    }


def roll_back_failed_delivery(ddb, request_id, key_id):
    now = datetime.now(timezone.utc).isoformat()
    ddb.transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": API_KEYS_TABLE,
                    "Key": {"key_id": {"S": key_id}},
                    "UpdateExpression": (
                        "SET #status = :revoked, revoked_at = :revoked_at, "
                        "revocation_reason = :reason"
                    ),
                    "ConditionExpression": "#status = :active",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":revoked": {"S": "REVOKED"},
                        ":active": {"S": "ACTIVE"},
                        ":revoked_at": {"S": now},
                        ":reason": {"S": "EMAIL_DELIVERY_FAILED"},
                    },
                }
            },
            {
                "Update": {
                    "TableName": ACCESS_REQUESTS_TABLE,
                    "Key": {"request_id": {"S": request_id}},
                    "UpdateExpression": "SET #status = :pending REMOVE approved_at, key_id",
                    "ConditionExpression": "#status = :approved AND key_id = :key_id",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":pending": {"S": "PENDING_APPROVAL"},
                        ":approved": {"S": "APPROVED"},
                        ":key_id": {"S": key_id},
                    },
                }
            },
        ]
    )


def approve_request(ddb, ses, request_id):
    require_pepper()
    require_email_sender()
    result = ddb.get_item(
        TableName=ACCESS_REQUESTS_TABLE,
        Key={"request_id": {"S": request_id}},
        ConsistentRead=True,
    )
    request = result.get("Item")
    if not request:
        raise RuntimeError("Access request was not found.")
    if string_value(request, "status") != "PENDING_APPROVAL":
        raise RuntimeError("Only requests that are waiting for approval can receive a key.")

    key_id = secrets.token_hex(8)
    raw_key = f"aqk_{key_id}_{secrets.token_urlsafe(32)}"
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())
    key_item = {
        "key_id": {"S": key_id},
        "key_hash": {"S": hash_key(raw_key)},
        "status": {"S": "ACTIVE"},
        "request_id": {"S": request_id},
        "email": {"S": string_value(request, "email")},
        "scopes": {"SS": READ_SCOPES},
        "created_at": {"S": now.isoformat()},
        "created_at_epoch": {"N": str(now_epoch)},
    }
    ddb.transact_write_items(
        TransactItems=[
            {
                "Put": {
                    "TableName": API_KEYS_TABLE,
                    "Item": key_item,
                    "ConditionExpression": "attribute_not_exists(key_id)",
                }
            },
            {
                "Update": {
                    "TableName": ACCESS_REQUESTS_TABLE,
                    "Key": {"request_id": {"S": request_id}},
                    "UpdateExpression": "SET #status = :approved, approved_at = :approved_at, key_id = :key_id",
                    "ConditionExpression": "#status = :pending",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":approved": {"S": "APPROVED"},
                        ":pending": {"S": "PENDING_APPROVAL"},
                        ":approved_at": {"S": now.isoformat()},
                        ":key_id": {"S": key_id},
                    },
                }
            },
        ]
    )

    try:
        ses.send_email(**key_delivery_email(request, raw_key, key_id))
    except Exception as email_error:
        try:
            roll_back_failed_delivery(ddb, request_id, key_id)
        except Exception as rollback_error:
            raise RuntimeError(
                f"Key email failed and automatic revocation also failed for key ID {key_id}. "
                "Revoke this key immediately before retrying. "
                f"Email error: {email_error}; rollback error: {rollback_error}"
            ) from email_error
        raise RuntimeError(
            "Key email failed. The new key was revoked and the request was returned "
            "to PENDING_APPROVAL; fix SES delivery and approve it again."
        ) from email_error

    print(f"Read-only API key emailed to the approved request address for {request_id}.")
    print(f"Key ID: {key_id}")
    print("Scopes: " + ", ".join(READ_SCOPES))


def list_active_keys(ddb):
    result = ddb.query(
        TableName=API_KEYS_TABLE,
        IndexName="status-created-at-index",
        KeyConditionExpression="#status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": {"S": "ACTIVE"}},
        ScanIndexForward=False,
    )
    items = result.get("Items", [])
    if not items:
        print("No active API keys.")
        return

    print("Active API keys:\n")
    for item in items:
        print(
            f"{string_value(item, 'key_id')}  "
            f"{string_value(item, 'email')}  "
            f"issued {string_value(item, 'created_at')}"
        )


def revoke_key(ddb, key_id):
    now = datetime.now(timezone.utc).isoformat()
    result = ddb.update_item(
        TableName=API_KEYS_TABLE,
        Key={"key_id": {"S": key_id}},
        UpdateExpression="SET #status = :revoked, revoked_at = :revoked_at",
        ConditionExpression="attribute_exists(key_id) AND #status = :active",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":revoked": {"S": "REVOKED"},
            ":active": {"S": "ACTIVE"},
            ":revoked_at": {"S": now},
        },
        ReturnValues="ALL_NEW",
    )
    print(f"Key {string_value(result.get('Attributes', {}), 'key_id', key_id)} revoked.")


def build_parser():
    parser = argparse.ArgumentParser(description="Manage private API access requests and keys.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("list-pending", help="List requests waiting for approval.")
    subcommands.add_parser("list-active", help="List active API key IDs and their owners.")

    approve = subcommands.add_parser(
        "approve",
        help="Approve a request and email a one-time API key to its submitted address.",
    )
    approve.add_argument("--request-id", required=True, type=uuid.UUID)

    revoke = subcommands.add_parser("revoke", help="Revoke an active API key immediately.")
    revoke.add_argument("--key-id", required=True, type=api_key_id)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    ddb, ses = clients()
    try:
        if args.command == "list-pending":
            list_requests(ddb)
        elif args.command == "approve":
            approve_request(ddb, ses, str(args.request_id))
        elif args.command == "list-active":
            list_active_keys(ddb)
        elif args.command == "revoke":
            revoke_key(ddb, args.key_id)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
