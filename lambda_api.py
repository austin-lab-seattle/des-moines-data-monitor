import csv
import hmac
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3

s3_client = boto3.client("s3")
dynamodb_client = boto3.client("dynamodb")
ses_client = boto3.client("ses")

BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
INSTRUMENT_IDS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]
ENABLE_COST_KPI = os.environ.get("ENABLE_COST_KPI") == "1"
ENABLE_API_KEY_REGISTRATION = os.environ.get("ENABLE_API_KEY_REGISTRATION") == "1"
PUBLIC_API_KEY_REQUIRED = os.environ.get("PUBLIC_API_KEY_REQUIRED") == "1"
ACCESS_REQUESTS_TABLE = os.environ.get("ACCESS_REQUESTS_TABLE", "AQApiAccessRequests")
API_KEYS_TABLE = os.environ.get("API_KEYS_TABLE", "AQApiKeys")
RATE_LIMITS_TABLE = os.environ.get("RATE_LIMITS_TABLE", "AQApiRateLimits")
ADMIN_AUDIT_TABLE = os.environ.get("ADMIN_AUDIT_TABLE", "AQAdminAudit")
API_KEY_HASH_PEPPER = os.environ.get("API_KEY_HASH_PEPPER")
ACCESS_REQUEST_FROM_EMAIL = os.environ.get("ACCESS_REQUEST_FROM_EMAIL")
API_ROUTES = {
    "summary": "/air-quality/v1/summary",
    "timeseries": "/air-quality/v1/timeseries",
    "observations": "/air-quality/v1/observations",
    "observations_export": "/air-quality/v1/observations/export",
    "access_requests": "/air-quality/v1/access-requests",
    "verify_access": "/air-quality/v1/access-requests/verify",
}
KEYED_API_ROUTES = {
    "summary": "/air-quality/v1/keyed/summary",
    "timeseries": "/air-quality/v1/keyed/timeseries",
    "observations": "/air-quality/v1/keyed/observations",
    "observations_export": "/air-quality/v1/keyed/observations/export",
}
INTERNAL_API_ROUTES = {
    "api_users": "/air-quality/internal/v1/api-users",
    "api_users_revoke": "/air-quality/internal/v1/api-users/revoke",
    "observations": "/air-quality/internal/v1/observations",
    "flags": "/air-quality/internal/v1/flags",
    "corrections": "/air-quality/internal/v1/corrections",
    "audit": "/air-quality/internal/v1/audit",
}
LEGACY_API_ROUTES = {
    "summary": "/metrics",
    "timeseries": "/series",
    "observations": "/silver-records",
    "observations_export": "/silver-download",
}

# Row/size counts are recomputed live from S3, but cached briefly so a burst of
# refreshes does not each trigger a full scan. New data only lands every few
# minutes, so a short cache still feels live on the dashboard.
INVENTORY_TTL_SECONDS = 30
COST_TTL_SECONDS = 3600
_inventory_cache = {"data": None, "ts": 0.0}
_cost_cache = {"data": None, "ts": 0.0}

# Counting many small batch objects is dominated by per-object request latency,
# so fan the downloads out across threads.
MAX_WORKERS = 24


def iter_s3_objects(prefix):
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for object_summary in page.get("Contents", []):
            key = object_summary["Key"]
            if key.endswith(".keep") or key.endswith("/"):
                continue
            yield object_summary


def response(status_code, payload=None, extra_headers=None):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "content-type,authorization",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": "" if payload is None else json.dumps(payload),
    }


def access_response(status_code, payload=None):
    """Response for the one public POST route used to request API access."""
    return response(
        status_code,
        payload,
        {"Access-Control-Allow-Methods": "GET,POST,OPTIONS"},
    )


def request_base_url(event):
    headers = normalized_headers(event)
    host = headers.get("host")
    if not host:
        return ""
    scheme = headers.get("x-forwarded-proto") or "https"
    return f"{scheme}://{host}"


def redirect_response(event, canonical_path):
    query = event.get("rawQueryString") or ""
    location = f"{request_base_url(event)}{canonical_path}"
    if query:
        location = f"{location}?{query}"
    return response(
        308,
        {"message": "Endpoint moved", "location": location},
        {"Location": location},
    )


def parse_body(event):
    raw_body = event.get("body") or "{}"
    try:
        return json.loads(raw_body)
    except json.JSONDecodeError:
        return None


def get_route(event):
    request_context = event.get("requestContext", {})
    http_context = request_context.get("http", {})
    method = http_context.get("method") or event.get("httpMethod") or "GET"
    path = event.get("rawPath") or http_context.get("path") or event.get("path") or API_ROUTES["summary"]
    return method.upper(), path.rstrip("/") or "/"


def route_matches(path, *routes):
    return any(path == route or path.endswith(route) for route in routes)


def legacy_route_target(path):
    for name, legacy_path in LEGACY_API_ROUTES.items():
        if route_matches(path, legacy_path):
            return API_ROUTES[name]
    return None


def query_params(event):
    return event.get("queryStringParameters") or {}


def normalized_headers(event):
    return {key.lower(): value for key, value in (event.get("headers") or {}).items()}


TEAM_ROLES = {"Admin", "Reviewer", "AccessManager", "Viewer"}


def _claim_groups(value):
    """Normalize API Gateway's Cognito groups claim into a set."""
    if isinstance(value, list):
        return {str(group) for group in value}
    if not value:
        return set()
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return {str(group) for group in parsed}
        except json.JSONDecodeError:
            text = text.strip("[]")
    return {group.strip().strip("'\"") for group in text.split(",") if group.strip()}


def team_identity(event):
    """Trust claims only after an API Gateway JWT authorizer has verified them."""
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    subject = str(claims.get("sub") or "").strip()
    email = normalize_email(claims.get("email"))
    groups = _claim_groups(claims.get("cognito:groups") or claims.get("groups"))
    roles = groups & TEAM_ROLES
    if not subject or not email:
        return None
    return {"subject": subject, "email": email, "roles": roles}


def require_team_role(event, allowed_roles):
    identity = team_identity(event)
    if not identity:
        return None, response(401, {"error": "Team sign-in required"})
    if not identity["roles"].intersection(set(allowed_roles)):
        return None, response(403, {"error": "Your team role does not allow this action"})
    return identity, None


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
API_KEY_PATTERN = re.compile(r"^aqk_([a-z0-9]{16})_([A-Za-z0-9_-]{32,})$")
VERIFICATION_TOKEN_PATTERN = re.compile(r"^aqv_([0-9a-f-]{36})_([A-Za-z0-9_-]{32,})$")
ACCESS_REQUEST_WINDOW_SECONDS = 24 * 60 * 60
ACCESS_REQUEST_RETENTION_SECONDS = 90 * 24 * 60 * 60
VERIFICATION_WINDOW_SECONDS = 30 * 60
PER_MINUTE_LIMIT = int(os.environ.get("API_RATE_LIMIT_PER_MINUTE", "30"))
PER_DAY_LIMIT = int(os.environ.get("API_RATE_LIMIT_PER_DAY", "5000"))
EXPORTS_PER_DAY_LIMIT = int(os.environ.get("API_EXPORT_LIMIT_PER_DAY", "20"))
READ_SCOPES = {
    "summary": "read:summary",
    "timeseries": "read:timeseries",
    "observations": "read:observations",
    "observations_export": "read:export",
}


def ddb_string(item, name, default=None):
    value = (item or {}).get(name)
    if not value:
        return default
    return value.get("S", default)


def ddb_number(item, name, default=None):
    value = (item or {}).get(name)
    if not value:
        return default
    try:
        return int(value.get("N"))
    except (TypeError, ValueError):
        return default


def ddb_string_list(item, name):
    return (item or {}).get(name, {}).get("SS", [])


def normalize_email(value):
    email = str(value or "").strip().lower()
    return email if EMAIL_PATTERN.fullmatch(email) else None


def limit_text(value, maximum):
    return str(value or "").strip()[:maximum]


def access_registration_ready():
    return bool(ENABLE_API_KEY_REGISTRATION and API_KEY_HASH_PEPPER and ACCESS_REQUEST_FROM_EMAIL)


def key_hash(raw_key):
    if not API_KEY_HASH_PEPPER:
        return None
    return hmac.new(
        API_KEY_HASH_PEPPER.encode("utf-8"),
        raw_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verification_email(request, raw_token, event):
    recipient = ddb_string(request, "email")
    name = ddb_string(request, "name") or "researcher"
    verification_url = f"{request_base_url(event)}{API_ROUTES['verify_access']}?token={raw_token}"
    text = (
        f"Hello {name},\n\n"
        "Verify your email address to receive a personal read-only Des Moines Air Quality API key.\n\n"
        f"Verify email: {verification_url}\n\n"
        "This link expires in 30 minutes and can be used once. If you did not request an API key, ignore this message.\n"
    )
    return {
        "Source": ACCESS_REQUEST_FROM_EMAIL,
        "Destination": {"ToAddresses": [recipient]},
        "Message": {
            "Subject": {"Data": "Verify your Des Moines Air Quality API request", "Charset": "UTF-8"},
            "Body": {"Text": {"Data": text, "Charset": "UTF-8"}},
        },
    }


def key_delivery_email(request, raw_key, key_id):
    recipient = ddb_string(request, "email")
    name = ddb_string(request, "name") or "researcher"
    text = (
        f"Hello {name},\n\nYour email has been verified. Here is your personal read-only Des Moines Air Quality API key.\n\n"
        f"API key: {raw_key}\nKey ID: {key_id}\n\n"
        "Send the key only in the x-api-key request header. Store it in a secret manager or environment variable. "
        "Do not put it in a URL, repository, notebook, screenshot, or shared document.\n\n"
        "This key cannot change or flag observations.\n"
    )
    return {
        "Source": ACCESS_REQUEST_FROM_EMAIL,
        "Destination": {"ToAddresses": [recipient]},
        "Message": {
            "Subject": {"Data": "Your Des Moines Air Quality API key", "Charset": "UTF-8"},
            "Body": {"Text": {"Data": text, "Charset": "UTF-8"}},
        },
    }


def find_recent_access_request(email):
    result = dynamodb_client.query(
        TableName=ACCESS_REQUESTS_TABLE,
        IndexName="email-created-at-index",
        KeyConditionExpression="email = :email",
        ExpressionAttributeValues={":email": {"S": email}},
        ScanIndexForward=False,
        Limit=1,
    )
    items = result.get("Items") or []
    return items[0] if items else None


def create_access_request(event):
    """Email a short-lived ownership check before issuing an API key."""
    if not access_registration_ready():
        return access_response(503, {"error": "API access registration is not available"})

    payload = parse_body(event)
    if payload is None:
        return access_response(400, {"error": "Invalid JSON body"})

    email = normalize_email(payload.get("email"))
    name = limit_text(payload.get("name"), 120)
    organization = limit_text(payload.get("organization"), 160)
    use_case = limit_text(payload.get("use_case"), 1000)
    if not email or not name or not use_case:
        return access_response(400, {
            "error": "Name, email, and intended use are required",
        })

    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())
    try:
        recent_request = find_recent_access_request(email)
        recent_created = ddb_number(recent_request, "created_at_epoch", 0) or 0
        recent_status = ddb_string(recent_request, "status")
        recent_verification_expiry = (
            ddb_number(recent_request, "verification_expires_epoch", 0) or 0
        )
        if recent_request and recent_status == "ACTIVE":
            return access_response(202, {
                "message": (
                    "A personal API key has already been issued to this address. "
                    "Contact the project team if it must be revoked or replaced."
                ),
            })
        if (
            recent_request
            and now_epoch - recent_created < ACCESS_REQUEST_WINDOW_SECONDS
            and recent_status == "PENDING_VERIFICATION"
            and recent_verification_expiry > now_epoch
        ):
            return access_response(202, {
                "message": "Check your email for the verification link.",
            })
    except Exception as exc:
        print(f"Could not check API access request state: {exc}")
        return access_response(503, {"error": "API access registration is temporarily unavailable"})

    request_id = str(uuid.uuid4())
    raw_token = f"aqv_{request_id}_{secrets.token_urlsafe(32)}"
    item = {
        "request_id": {"S": request_id},
        "email": {"S": email},
        "name": {"S": name},
        "organization": {"S": organization},
        "use_case": {"S": use_case},
        "status": {"S": "PENDING_VERIFICATION"},
        "verification_token_hash": {"S": key_hash(raw_token)},
        "verification_expires_epoch": {"N": str(now_epoch + VERIFICATION_WINDOW_SECONDS)},
        "created_at": {"S": now.isoformat()},
        "created_at_epoch": {"N": str(now_epoch)},
        "ttl": {"N": str(now_epoch + ACCESS_REQUEST_RETENTION_SECONDS)},
    }
    try:
        dynamodb_client.put_item(
            TableName=ACCESS_REQUESTS_TABLE,
            Item=item,
            ConditionExpression="attribute_not_exists(request_id)",
        )
        ses_client.send_email(**verification_email(item, raw_token, event))
    except Exception as exc:
        print(f"Could not create API access request: {exc}")
        return access_response(503, {"error": "API access registration is temporarily unavailable"})

    return access_response(202, {
        "message": "Check your email and open the verification link within 30 minutes.",
    })


def verify_access_request(event):
    if not access_registration_ready():
        return access_response(503, {"error": "API access registration is not available"})
    raw_token = str(query_params(event).get("token") or "").strip()
    match = VERIFICATION_TOKEN_PATTERN.fullmatch(raw_token)
    if not match:
        return access_response(400, {"error": "Invalid verification link"})

    request_id = match.group(1)
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())
    try:
        result = dynamodb_client.get_item(
            TableName=ACCESS_REQUESTS_TABLE,
            Key={"request_id": {"S": request_id}},
            ConsistentRead=True,
        )
        request = result.get("Item")
        expected_hash = ddb_string(request, "verification_token_hash")
        if (
            not request
            or ddb_string(request, "status") != "PENDING_VERIFICATION"
            or not expected_hash
            or now_epoch > (ddb_number(request, "verification_expires_epoch", 0) or 0)
            or not hmac.compare_digest(key_hash(raw_token), expected_hash)
        ):
            return access_response(400, {"error": "Verification link is invalid or expired"})

        key_id = secrets.token_hex(8)
        raw_key = f"aqk_{key_id}_{secrets.token_urlsafe(32)}"
        key_item = {
            "key_id": {"S": key_id},
            "key_hash": {"S": key_hash(raw_key)},
            "status": {"S": "ACTIVE"},
            "request_id": {"S": request_id},
            "email": {"S": ddb_string(request, "email")},
            "scopes": {"SS": list(READ_SCOPES.values())},
            "created_at": {"S": now.isoformat()},
            "created_at_epoch": {"N": str(now_epoch)},
        }
        dynamodb_client.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": API_KEYS_TABLE,
                "Item": key_item,
                "ConditionExpression": "attribute_not_exists(key_id)",
            }},
            {"Update": {
                "TableName": ACCESS_REQUESTS_TABLE,
                "Key": {"request_id": {"S": request_id}},
                "UpdateExpression": (
                    "SET #status = :active, verified_at = :verified_at, key_id = :key_id "
                    "REMOVE verification_token_hash"
                ),
                "ConditionExpression": "#status = :pending",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":active": {"S": "ACTIVE"},
                    ":pending": {"S": "PENDING_VERIFICATION"},
                    ":verified_at": {"S": now.isoformat()},
                    ":key_id": {"S": key_id},
                },
            }},
        ])
        try:
            ses_client.send_email(**key_delivery_email(request, raw_key, key_id))
        except Exception:
            failed_at = datetime.now(timezone.utc).isoformat()
            dynamodb_client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": API_KEYS_TABLE,
                    "Key": {"key_id": {"S": key_id}},
                    "UpdateExpression": "SET #status = :revoked, revoked_at = :failed_at",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":revoked": {"S": "REVOKED"},
                        ":failed_at": {"S": failed_at},
                    },
                }},
                {"Update": {
                    "TableName": ACCESS_REQUESTS_TABLE,
                    "Key": {"request_id": {"S": request_id}},
                    "UpdateExpression": "SET #status = :failed, delivery_failed_at = :failed_at",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":failed": {"S": "DELIVERY_FAILED"},
                        ":failed_at": {"S": failed_at},
                    },
                }},
            ])
            raise
    except Exception as exc:
        print(f"Could not verify API access request: {exc}")
        return access_response(503, {"error": "API key delivery is temporarily unavailable"})

    return access_response(200, {
        "message": "Email verified. Your personal API key has been emailed to you."
    })


def public_api_key(event):
    headers = normalized_headers(event)
    key = headers.get("x-api-key", "")
    authorization = headers.get("authorization", "")
    if not key and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    return str(key).strip()


def increment_rate_counter(counter_id, limit, ttl, retry_after):
    try:
        dynamodb_client.update_item(
            TableName=RATE_LIMITS_TABLE,
            Key={"counter_id": {"S": counter_id}},
            UpdateExpression="SET #ttl = :ttl ADD request_count :one",
            ConditionExpression="attribute_not_exists(request_count) OR request_count < :limit",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":ttl": {"N": str(ttl)},
                ":one": {"N": "1"},
                ":limit": {"N": str(limit)},
            },
        )
        return None
    except Exception as exc:
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            return response(
                429,
                {"error": "API rate limit exceeded"},
                {"Retry-After": str(retry_after)},
            )
        print(f"Could not update API rate counter: {exc}")
        return response(503, {"error": "API rate limiting is temporarily unavailable"})


def enforce_rate_limit(key_id, required_scope):
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())
    minute_bucket = now_epoch // 60
    result = increment_rate_counter(
        f"{key_id}:minute:{minute_bucket}",
        PER_MINUTE_LIMIT,
        now_epoch + 120,
        max(1, 60 - (now_epoch % 60)),
    )
    if result:
        return result

    day_bucket = now.strftime("%Y-%m-%d")
    day_limit = EXPORTS_PER_DAY_LIMIT if required_scope == READ_SCOPES["observations_export"] else PER_DAY_LIMIT
    counter_type = "exports" if required_scope == READ_SCOPES["observations_export"] else "requests"
    result = increment_rate_counter(
        f"{key_id}:{counter_type}:{day_bucket}",
        day_limit,
        now_epoch + (2 * 24 * 60 * 60),
        24 * 60 * 60,
    )
    if result:
        return result
    try:
        dynamodb_client.update_item(
            TableName=API_KEYS_TABLE,
            Key={"key_id": {"S": key_id}},
            UpdateExpression="SET last_used_at = :now, last_used_at_epoch = :epoch",
            ExpressionAttributeValues={
                ":now": {"S": now.isoformat()},
                ":epoch": {"N": str(now_epoch)},
            },
        )
    except Exception as exc:
        # Authentication and rate limiting succeeded; usage metadata is useful
        # for operators but must not make an otherwise valid research request fail.
        print(f"Could not update API key last-used metadata: {exc}")
    return None


def require_public_api_access(event, required_scope, force=False):
    """Validate an issued API key when key enforcement is explicitly enabled."""
    supplied_key = public_api_key(event)
    if not PUBLIC_API_KEY_REQUIRED and not force and not supplied_key:
        return None
    if not API_KEY_HASH_PEPPER:
        print("Public API key enforcement enabled without API_KEY_HASH_PEPPER")
        return response(503, {"error": "API key authentication is not configured"})

    if not supplied_key:
        return response(401, {"error": "API key required"})
    match = API_KEY_PATTERN.fullmatch(supplied_key)
    if not match:
        return response(403, {"error": "Invalid API key"})

    key_id = match.group(1)
    try:
        result = dynamodb_client.get_item(
            TableName=API_KEYS_TABLE,
            Key={"key_id": {"S": key_id}},
            ConsistentRead=True,
        )
        item = result.get("Item")
    except Exception as exc:
        print(f"Could not validate public API key: {exc}")
        return response(503, {"error": "API key authentication is temporarily unavailable"})

    expected_hash = ddb_string(item, "key_hash")
    if (
        not item
        or ddb_string(item, "status") != "ACTIVE"
        or not expected_hash
        or not hmac.compare_digest(key_hash(supplied_key), expected_hash)
        or required_scope not in ddb_string_list(item, "scopes")
    ):
        return response(403, {"error": "Invalid API key"})
    return enforce_rate_limit(key_id, required_scope)


# --- Row detection: which lines are real data rows vs headers and comments ---

def clean_field(field):
    return field.strip().lstrip("\ufeff").strip('"').strip().replace("cm�", "cm³")


def split_fields(line):
    if "\t" in line:
        return [clean_field(field) for field in line.split("\t")]
    try:
        return [clean_field(field) for field in next(csv.reader([line]))]
    except csv.Error:
        return []


def is_float(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def is_data_row(instrument_id, line):
    stripped = line.strip().lstrip("\ufeff")
    if not stripped or stripped.startswith(('%', '#')):
        return False

    fields = split_fields(stripped)
    if not fields or not any(fields):
        return False

    first = fields[0]
    second = fields[1] if len(fields) > 1 else ""

    if instrument_id == "BC-MA200":
        return len(fields) > 10 and first.upper().startswith("MA") and second.isdigit()

    if instrument_id == "CO2-LICOR":
        # The Li-Cor does not zero-pad hours/minutes (e.g. 18:0:00), so accept
        # 1- or 2-digit time components.
        return (
            len(fields) >= 3
            and re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", first)
            and re.match(r"^\d{1,2}:\d{1,2}:\d{1,2}$", second)
        )

    if instrument_id == "NEPH-PM25":
        return (
            len(fields) >= 3
            and re.match(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$", first)
            and is_float(second)
        )

    if instrument_id == "NO2-CAPS":
        return len(fields) >= 10 and re.match(r"^\d{6}$", first) and is_float(fields[3])

    if instrument_id == "SMPS":
        return (
            len(fields) > 40
            and first.isdigit()
            and re.match(r"^\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2}$", second)
        )

    return False


def parse_datetime_value(value, prefer_month_first=False):
    if value is None:
        return None
    cleaned = str(value).strip().strip('"')
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        pass

    base_formats = [
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
    ]
    slash_formats = [
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S",
    ] if prefer_month_first else [
        "%d/%m/%Y %H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ]
    formats = base_formats + slash_formats
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def datetime_for_compare(value, prefer_month_first=False):
    parsed = parse_datetime_value(value, prefer_month_first=prefer_month_first)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def instrument_prefers_month_first(instrument_id):
    return instrument_id == "SMPS"


def datetime_for_instrument(instrument_id, value):
    return datetime_for_compare(
        value,
        prefer_month_first=instrument_prefers_month_first(instrument_id),
    )


def column_index(columns):
    return {column.strip().lower(): index for index, column in enumerate(columns)}


def first_existing(index, names):
    for name in names:
        if name.lower() in index:
            return index[name.lower()]
    return None


def value_at(fields, index):
    if index is None or index >= len(fields):
        return None
    return fields[index]


def record_timestamp(instrument_id, columns, fields):
    index = column_index(columns)
    if instrument_id == "BC-MA200":
        direct = value_at(fields, first_existing(index, ["Date / time local"]))
        if direct:
            return direct
        date_value = value_at(fields, first_existing(index, ["Date local (yyyy/MM/dd)"]))
        time_value = value_at(fields, first_existing(index, ["Time local (hh:mm:ss)"]))
        return f"{date_value} {time_value}" if date_value and time_value else None

    if instrument_id == "CO2-LICOR":
        date_value = value_at(fields, first_existing(index, ["System_Date_(Y-M-D)"]))
        time_value = value_at(fields, first_existing(index, ["System_Time_(h:m:s)"]))
        return f"{date_value} {time_value}" if date_value and time_value else None

    if instrument_id == "NEPH-PM25":
        return value_at(fields, first_existing(index, ["Date_Time"]))

    if instrument_id == "NO2-CAPS":
        return value_at(fields, first_existing(index, ["Timestamp"]))

    if instrument_id == "SMPS":
        return value_at(fields, first_existing(index, ["DateTime Sample Start"]))

    return None


def make_row_key(instrument_id, timestamp_raw, raw_line):
    raw = f"{instrument_id}|{timestamp_raw or ''}|{raw_line}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_valid_instrument(instrument_id):
    return instrument_id in INSTRUMENT_IDS


def count_data_rows(instrument_id, s3_key):
    obj = s3_client.get_object(Bucket=BUCKET, Key=s3_key)
    raw = obj["Body"].read().decode("utf-8", errors="replace")
    return sum(1 for line in raw.splitlines() if is_data_row(instrument_id, line))


def _safe_count(task):
    instrument_id, key = task
    try:
        return instrument_id, count_data_rows(instrument_id, key)
    except Exception as exc:
        print(f"Could not count rows in {key}: {exc}")
        return instrument_id, 0


def get_silver_rows(instrument_id):
    """Read the consolidated row count from the silver metadata sidecar (cheap)."""
    try:
        body = s3_client.get_object(
            Bucket=BUCKET,
            Key=f"{instrument_id}/silver/{instrument_id}_metadata.txt",
        )["Body"].read().decode("utf-8", errors="replace")
        for line in body.splitlines():
            if line.startswith("# data_rows_unique:"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        return None
    return None


def get_silver_text(instrument_id):
    key = f"{instrument_id}/silver/{instrument_id}_data.csv"
    return s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8", errors="replace")


def read_review_objects(instrument_id, review_type):
    items = []
    prefix = f"{instrument_id}/{review_type}/"
    for object_summary in iter_s3_objects(prefix):
        try:
            body = s3_client.get_object(Bucket=BUCKET, Key=object_summary["Key"])["Body"].read()
            item = json.loads(body.decode("utf-8"))
            item["_s3_key"] = object_summary["Key"]
            items.append(item)
        except Exception as exc:
            print(f"Could not read review object {object_summary['Key']}: {exc}")
    return items


def row_matches_time_range(row_time, start_time, end_time):
    if row_time is None:
        return False
    if start_time is not None and row_time < start_time:
        return False
    if end_time is not None and row_time > end_time:
        return False
    return True


def review_item_applies_to_row(item, row):
    if item.get("status", "active") != "active":
        return False

    row_keys = set(item.get("row_keys") or [])
    if item.get("row_key"):
        row_keys.add(item["row_key"])
    if row["row_key"] in row_keys:
        return True

    if item.get("scope") == "time_range":
        start_time = datetime_for_compare(item.get("start_time") or item.get("start"))
        end_time = datetime_for_compare(item.get("end_time") or item.get("end"))
        return row_matches_time_range(row["timestamp_compare"], start_time, end_time)

    return False


def parse_silver_records(instrument_id, start_raw=None, end_raw=None, limit=100, cursor=0, order="asc"):
    text = get_silver_text(instrument_id)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return [], [], None, 0

    columns = split_fields(lines[0])
    start_time = datetime_for_compare(start_raw)
    end_time = datetime_for_compare(end_raw)
    rows = []
    skipped = 0
    limit = min(max(int(limit or 100), 1), 500)
    cursor = max(int(cursor or 0), 0)

    flags = read_review_objects(instrument_id, "flags")
    corrections = read_review_objects(instrument_id, "corrections")
    source_rows = list(enumerate(lines[1:]))
    if order == "desc":
        source_rows.reverse()

    for source_index, raw_line in source_rows:
        fields = split_fields(raw_line)
        timestamp_raw = record_timestamp(instrument_id, columns, fields)
        timestamp_compare = datetime_for_instrument(instrument_id, timestamp_raw)
        if (start_time or end_time) and not row_matches_time_range(timestamp_compare, start_time, end_time):
            continue
        if skipped < cursor:
            skipped += 1
            continue

        values = {
            columns[index] if index < len(columns) else f"column_{index + 1}": value
            for index, value in enumerate(fields)
        }
        row = {
            "row_key": make_row_key(instrument_id, timestamp_raw, raw_line),
            "source_index": source_index,
            "timestamp": timestamp_raw,
            "timestamp_iso": timestamp_compare.isoformat() if timestamp_compare else None,
            "timestamp_compare": timestamp_compare,
            "values": values,
            "raw": raw_line,
        }
        row_flags = [item for item in flags if review_item_applies_to_row(item, row)]
        row_corrections = [item for item in corrections if review_item_applies_to_row(item, row)]
        row["flags"] = row_flags
        row["corrections"] = row_corrections
        row["status"] = "corrected" if row_corrections else "flagged" if row_flags else "normal"
        del row["timestamp_compare"]
        rows.append(row)

        if len(rows) >= limit:
            break

    next_cursor = cursor + len(rows) if len(rows) == limit else None
    return columns, rows, next_cursor, len(flags) + len(corrections)


def count_silver_records(instrument_id, start_raw=None, end_raw=None):
    """Count data rows in a time window without the heavier flag/correction join."""
    text = get_silver_text(instrument_id)
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return 0
    start_time = datetime_for_compare(start_raw)
    end_time = datetime_for_compare(end_raw)
    if not (start_time or end_time):
        return len(lines) - 1
    columns = split_fields(lines[0])
    count = 0
    for raw_line in lines[1:]:
        fields = split_fields(raw_line)
        moment = datetime_for_instrument(instrument_id, record_timestamp(instrument_id, columns, fields))
        if row_matches_time_range(moment, start_time, end_time):
            count += 1
    return count


def get_silver_records(event):
    params = query_params(event)
    instrument_id = params.get("instrument") or params.get("instrument_id")
    if not is_valid_instrument(instrument_id):
        return response(400, {"error": "Invalid or missing instrument"})

    # The Flag Range dialog previews how many records a window covers before
    # committing, so answer that cheaply without building full row objects.
    if str(params.get("count_only", "")).lower() in ("1", "true", "yes"):
        try:
            count = count_silver_records(instrument_id, params.get("start"), params.get("end"))
        except s3_client.exceptions.NoSuchKey:
            return response(404, {"error": "Records not found", "instrument_id": instrument_id})
        except Exception as exc:
            print(f"Silver count error: {exc}")
            return response(500, {"error": "Could not count records"})
        return response(200, {"instrument_id": instrument_id, "count": count})

    try:
        columns, rows, next_cursor, review_count = parse_silver_records(
            instrument_id,
            params.get("start"),
            params.get("end"),
            params.get("limit", 100),
            params.get("cursor", 0),
            params.get("order", "asc"),
        )
    except s3_client.exceptions.NoSuchKey:
        return response(404, {"error": "Records not found", "instrument_id": instrument_id})
    except Exception as exc:
        print(f"Silver records error: {exc}")
        return response(500, {"error": "Could not read records"})

    return response(200, {
        "instrument_id": instrument_id,
        "columns": columns,
        "rows": rows,
        "next_cursor": next_cursor,
        "review_object_count": review_count,
    })


def get_review_items(event, review_type):
    params = query_params(event)
    instrument_id = params.get("instrument") or params.get("instrument_id")
    if not is_valid_instrument(instrument_id):
        return response(400, {"error": "Invalid or missing instrument"})
    items = read_review_objects(instrument_id, review_type)
    return response(200, {"instrument_id": instrument_id, review_type: items})


def write_review_item(event, review_type, identity):
    payload = parse_body(event)
    if payload is None:
        return response(400, {"error": "Invalid JSON body"})

    instrument_id = payload.get("instrument_id") or payload.get("instrument")
    if not is_valid_instrument(instrument_id):
        return response(400, {"error": "Invalid or missing instrument_id"})

    now = datetime.now(timezone.utc)
    review_id = f"{review_type[:-1]}_{now.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:10]}"
    item = {
        "id": review_id,
        "type": review_type[:-1],
        "instrument_id": instrument_id,
        "status": payload.get("status", "active"),
        "scope": payload.get("scope", "selected_rows"),
        "reason": payload.get("reason", ""),
        "notes": payload.get("notes", ""),
        "created_at": now.isoformat(),
        "created_by": identity["email"],
        "created_by_subject": identity["subject"],
    }

    for key in [
        "row_key",
        "row_keys",
        "timestamp",
        "start_time",
        "end_time",
        "original_values",
        "corrected_values",
    ]:
        if key in payload:
            item[key] = payload[key]

    if review_type == "flags":
        if not limit_text(item.get("reason"), 500):
            return response(400, {"error": "A flag reason is required"})
        if item["scope"] == "time_range" and (not item.get("start_time") or not item.get("end_time")):
            return response(400, {"error": "Time range flags require start_time and end_time"})
        if item["scope"] != "time_range" and not (item.get("row_key") or item.get("row_keys")):
            return response(400, {"error": "Selected row flags require row_key or row_keys"})

    if review_type == "corrections":
        if not item.get("row_key") or not item.get("corrected_values"):
            return response(400, {"error": "Corrections require row_key and corrected_values"})

    key = (
        f"{instrument_id}/{review_type}"
        f"/year={now.strftime('%Y')}"
        f"/month={now.strftime('%m')}"
        f"/{review_id}.json"
    )
    audit_id = begin_audit_event(
        identity,
        f"CREATE_{review_type[:-1].upper()}",
        review_id,
        {"instrument_id": instrument_id, "scope": item["scope"]},
    )
    if not audit_id:
        return response(503, {"error": "The audit log is unavailable; no change was made"})
    try:
        s3_client.put_object(
            Bucket=BUCKET,
            Key=key,
            Body=json.dumps(item, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:
        finish_audit_event(audit_id, "FAILED", str(exc))
        print(f"Could not write review item: {exc}")
        return response(500, {"error": "Could not save the review annotation"})
    finish_audit_event(audit_id, "SUCCEEDED")
    item["_s3_key"] = key
    item["audit_id"] = audit_id
    return response(201, item)


def begin_audit_event(identity, action, target_id, details=None):
    now = datetime.now(timezone.utc)
    audit_id = str(uuid.uuid4())
    item = {
        "audit_id": {"S": audit_id},
        "audit_scope": {"S": "ADMIN"},
        "created_at": {"S": now.isoformat()},
        "created_at_epoch": {"N": str(int(now.timestamp()))},
        "actor_email": {"S": identity["email"]},
        "actor_subject": {"S": identity["subject"]},
        "action": {"S": action},
        "target_id": {"S": str(target_id)},
        "result": {"S": "PENDING"},
    }
    if details:
        item["details"] = {"S": json.dumps(details, separators=(",", ":"), sort_keys=True)}
    try:
        dynamodb_client.put_item(
            TableName=ADMIN_AUDIT_TABLE,
            Item=item,
            ConditionExpression="attribute_not_exists(audit_id)",
        )
        return audit_id
    except Exception as exc:
        print(f"Could not begin admin audit event: {exc}")
        return None


def finish_audit_event(audit_id, result, error=None):
    now = datetime.now(timezone.utc).isoformat()
    expression = "SET #result = :result, completed_at = :completed_at"
    values = {
        ":result": {"S": result},
        ":completed_at": {"S": now},
    }
    if error:
        expression += ", error_message = :error"
        values[":error"] = {"S": limit_text(error, 500)}
    try:
        dynamodb_client.update_item(
            TableName=ADMIN_AUDIT_TABLE,
            Key={"audit_id": {"S": audit_id}},
            UpdateExpression=expression,
            ExpressionAttributeNames={"#result": "result"},
            ExpressionAttributeValues=values,
        )
        return True
    except Exception as exc:
        print(f"Could not finish admin audit event {audit_id}: {exc}")
        return False


def query_by_status(table_name, status, limit=100):
    result = dynamodb_client.query(
        TableName=table_name,
        IndexName="status-created-at-index",
        KeyConditionExpression="#status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": {"S": status}},
        ScanIndexForward=False,
        Limit=limit,
    )
    return result.get("Items") or []


def rate_counter_value(key_id, counter_type, day_bucket):
    result = dynamodb_client.get_item(
        TableName=RATE_LIMITS_TABLE,
        Key={"counter_id": {"S": f"{key_id}:{counter_type}:{day_bucket}"}},
        ConsistentRead=False,
    )
    return ddb_number(result.get("Item"), "request_count", 0) or 0


def get_team_api_users(event, identity):
    try:
        items = query_by_status(API_KEYS_TABLE, "ACTIVE") + query_by_status(API_KEYS_TABLE, "REVOKED")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        keys = []
        for item in sorted(items, key=lambda value: ddb_number(value, "created_at_epoch", 0), reverse=True):
            key_id = ddb_string(item, "key_id", "")
            request_id = ddb_string(item, "request_id", "")
            organization = ""
            if request_id:
                request = dynamodb_client.get_item(
                    TableName=ACCESS_REQUESTS_TABLE,
                    Key={"request_id": {"S": request_id}},
                ).get("Item")
                organization = ddb_string(request, "organization", "")
            keys.append({
                "key_id": key_id,
                "email": ddb_string(item, "email", ""),
                "organization": organization,
                "status": ddb_string(item, "status", "UNKNOWN"),
                "scopes": ddb_string_list(item, "scopes"),
                "created_at": ddb_string(item, "created_at"),
                "last_used_at": ddb_string(item, "last_used_at"),
                "requests_today": rate_counter_value(key_id, "requests", today),
                "exports_today": rate_counter_value(key_id, "exports", today),
            })
        return response(200, {
            "keys": keys,
            "limits": {
                "requests_per_minute": PER_MINUTE_LIMIT,
                "requests_per_day": PER_DAY_LIMIT,
                "exports_per_day": EXPORTS_PER_DAY_LIMIT,
            },
            "viewer": {"email": identity["email"], "roles": sorted(identity["roles"])},
        })
    except Exception as exc:
        print(f"Could not list API users: {exc}")
        return response(500, {"error": "Could not load API users"})


def revoke_team_api_key(event, identity):
    payload = parse_body(event)
    if payload is None:
        return response(400, {"error": "Invalid JSON body"})
    key_id = str(payload.get("key_id") or "").strip().lower()
    reason = limit_text(payload.get("reason"), 500)
    if not re.fullmatch(r"[a-f0-9]{16}", key_id) or not reason:
        return response(400, {"error": "A valid key ID and revocation reason are required"})
    try:
        key_item = dynamodb_client.get_item(
            TableName=API_KEYS_TABLE,
            Key={"key_id": {"S": key_id}},
            ConsistentRead=True,
        ).get("Item")
        if not key_item:
            return response(404, {"error": "API key not found"})
        if ddb_string(key_item, "status") != "ACTIVE":
            return response(409, {"error": "API key is not active"})
        audit_id = begin_audit_event(identity, "REVOKE_API_KEY", key_id, {"reason": reason})
        if not audit_id:
            return response(503, {"error": "The audit log is unavailable; no change was made"})
        now = datetime.now(timezone.utc).isoformat()
        dynamodb_client.update_item(
            TableName=API_KEYS_TABLE,
            Key={"key_id": {"S": key_id}},
            UpdateExpression=(
                "SET #status = :revoked, revoked_at = :now, "
                "revocation_reason = :reason, revoked_by = :actor"
            ),
            ConditionExpression="#status = :active",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":active": {"S": "ACTIVE"},
                ":revoked": {"S": "REVOKED"},
                ":now": {"S": now},
                ":reason": {"S": reason},
                ":actor": {"S": identity["email"]},
            },
        )
        request_id = ddb_string(key_item, "request_id")
        if request_id:
            dynamodb_client.update_item(
                TableName=ACCESS_REQUESTS_TABLE,
                Key={"request_id": {"S": request_id}},
                UpdateExpression="SET #status = :revoked, revoked_at = :now",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":revoked": {"S": "REVOKED"}, ":now": {"S": now}},
            )
        finish_audit_event(audit_id, "SUCCEEDED")
        return response(200, {"message": "API key revoked", "key_id": key_id, "audit_id": audit_id})
    except Exception as exc:
        if "audit_id" in locals() and audit_id:
            finish_audit_event(audit_id, "FAILED", str(exc))
        print(f"Could not revoke API key: {exc}")
        return response(500, {"error": "Could not revoke API key"})


def get_admin_audit(event, identity):
    try:
        result = dynamodb_client.query(
            TableName=ADMIN_AUDIT_TABLE,
            IndexName="scope-created-at-index",
            KeyConditionExpression="audit_scope = :scope",
            ExpressionAttributeValues={":scope": {"S": "ADMIN"}},
            ScanIndexForward=False,
            Limit=100,
        )
        events = []
        for item in result.get("Items") or []:
            events.append({
                "audit_id": ddb_string(item, "audit_id"),
                "created_at": ddb_string(item, "created_at"),
                "completed_at": ddb_string(item, "completed_at"),
                "actor_email": ddb_string(item, "actor_email"),
                "action": ddb_string(item, "action"),
                "target_id": ddb_string(item, "target_id"),
                "result": ddb_string(item, "result"),
            })
        return response(200, {
            "events": events,
            "viewer": {"email": identity["email"], "roles": sorted(identity["roles"])},
        })
    except Exception as exc:
        print(f"Could not read admin audit log: {exc}")
        return response(500, {"error": "Could not load the audit log"})


SERIES_DEFAULT_MEASUREMENT = {
    "SMPS": "Total Concentration",
    "NEPH-PM25": "Scat coefficient",
    "CO2-LICOR": "CO2",
    "NO2-CAPS": "NO2",
    "BC-MA200": "BC",
}


def numeric_columns(columns, data_lines, sample=25):
    counts = [0] * len(columns)
    seen = 0
    for raw_line in data_lines:
        fields = split_fields(raw_line)
        if len(fields) != len(columns):
            continue
        for index in range(min(len(columns), len(fields))):
            if is_float(fields[index]):
                counts[index] += 1
        seen += 1
        if seen >= sample:
            break
    if not seen:
        return []
    threshold = seen * 0.6
    return [columns[index] for index in range(len(columns)) if counts[index] >= threshold]


# Instrument-setting and diagnostic columns are numeric but not science
# measurements, and the SMPS export carries ~100 particle-size bins whose headers
# are bare diameters. Both only clutter the chart's measurement picker, so filter
# them out and leave the meaningful quantities (concentrations, sizes, coefficients).
MEASUREMENT_BLOCKLIST = (
    "flow", "voltage", "temp", "pressure", "humidity", "viscosity", "free path",
    "dma", "ramping", "transit", "adjustment", "dilution", "density", "sheath",
    "impactor", "size", "scan", "polarity", "direction", "neutralizer",
    "classifier", "detector", "communication", "status", "reserved", "d50",
    "inlet", "counting", "channel", "retrace",
)


def is_measurement_column(name):
    """True for real measurements; False for size-bin diameters and diagnostics."""
    if is_float(name):  # a bare-number header is a particle-size bin, not a measurement
        return False
    lowered = name.lower()
    return not any(term in lowered for term in MEASUREMENT_BLOCKLIST)


def pick_default_measurement(instrument_id, options):
    hint = SERIES_DEFAULT_MEASUREMENT.get(instrument_id, "")
    if hint:
        for column in options:
            if hint.lower() in column.lower():
                return column
    return options[0] if options else None


def get_series(event):
    """Hourly mean of one measurement over time, computed on demand from silver."""
    params = query_params(event)
    instrument_id = params.get("instrument") or params.get("instrument_id")
    if not is_valid_instrument(instrument_id):
        return response(400, {"error": "Invalid or missing instrument"})

    try:
        text = get_silver_text(instrument_id)
    except s3_client.exceptions.NoSuchKey:
        return response(404, {"error": "Silver data not found", "instrument_id": instrument_id})
    except Exception as exc:
        print(f"Series error: {exc}")
        return response(500, {"error": "Could not read silver data"})

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return response(200, {"instrument_id": instrument_id, "measurement": None, "measurements": [], "series": []})

    columns = split_fields(lines[0])
    data_lines = lines[1:]
    numeric = numeric_columns(columns, data_lines)
    # The picker shows only meaningful measurements, but any numeric column can
    # still be requested directly by name via ?measurement=.
    options = [column for column in numeric if is_measurement_column(column)] or numeric
    measurement = params.get("measurement")
    if measurement not in numeric:
        measurement = pick_default_measurement(instrument_id, options)
    if measurement is None:
        return response(200, {"instrument_id": instrument_id, "measurement": None, "measurements": options, "series": []})

    col_index = columns.index(measurement)
    start_time = datetime_for_compare(params.get("start"))
    end_time = datetime_for_compare(params.get("end"))

    buckets = {}
    skipped_schema_mismatch = 0
    skipped_invalid_measurement = 0
    skipped_no_timestamp = 0
    expected_fields = len(columns)
    for raw_line in data_lines:
        fields = split_fields(raw_line)
        if len(fields) != expected_fields:
            skipped_schema_mismatch += 1
            continue
        if col_index >= len(fields) or not is_float(fields[col_index]):
            skipped_invalid_measurement += 1
            continue
        moment = datetime_for_instrument(instrument_id, record_timestamp(instrument_id, columns, fields))
        if moment is None or not row_matches_time_range(moment, start_time, end_time):
            if moment is None:
                skipped_no_timestamp += 1
            continue
        hour = moment.replace(minute=0, second=0, microsecond=0).isoformat()
        agg = buckets.setdefault(hour, [0.0, 0])
        agg[0] += float(fields[col_index])
        agg[1] += 1

    series = [{"t": hour, "v": round(total / count, 3)} for hour, (total, count) in sorted(buckets.items())]
    plotted_rows = sum(count for _total, count in buckets.values())
    return response(200, {
        "instrument_id": instrument_id,
        "measurement": measurement,
        "measurements": options,
        "series": series,
        "source_rows": len(data_lines),
        "plotted_rows": plotted_rows,
        "skipped_schema_mismatch": skipped_schema_mismatch,
        "skipped_invalid_measurement": skipped_invalid_measurement,
        "skipped_no_timestamp": skipped_no_timestamp,
    })


def get_observations_export(event):
    """Hand back a short-lived presigned URL for the full cleaned observation CSV.

    The consolidated file can be tens of MB, past the Lambda response limit, so
    the client reads it straight from S3 instead of through the API.
    """
    params = query_params(event)
    instrument_id = params.get("instrument") or params.get("instrument_id")
    if not is_valid_instrument(instrument_id):
        return response(400, {"error": "Invalid or missing instrument"})

    key = f"{instrument_id}/silver/{instrument_id}_data.csv"
    try:
        head = s3_client.head_object(Bucket=BUCKET, Key=key)
    except Exception:
        return response(404, {"error": "Silver data not found", "instrument_id": instrument_id})

    filename = f"{instrument_id}_observations.csv"
    url = s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
            "ResponseContentType": "text/csv",
        },
        ExpiresIn=300,
    )
    return response(200, {
        "instrument_id": instrument_id,
        "filename": filename,
        "bytes": head.get("ContentLength"),
        "url": url,
    })


def compute_inventory():
    """List every bronze object, count rows in parallel, aggregate per instrument."""
    per_instrument_objects = {
        instrument_id: list(iter_s3_objects(f"{instrument_id}/bronze/"))
        for instrument_id in INSTRUMENT_IDS
    }
    tasks = [
        (instrument_id, obj["Key"])
        for instrument_id, objects in per_instrument_objects.items()
        for obj in objects
    ]

    counts = {instrument_id: 0 for instrument_id in INSTRUMENT_IDS}
    if tasks:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for instrument_id, row_count in pool.map(_safe_count, tasks):
                counts[instrument_id] += row_count

    instruments = []
    latest_global_update = None
    latest_global_instrument = "NONE"
    any_data = False

    for instrument_id in INSTRUMENT_IDS:
        objects = per_instrument_objects[instrument_id]
        bronze_size = sum(obj["Size"] for obj in objects)
        last_modified = max((obj["LastModified"] for obj in objects), default=None)

        if last_modified:
            any_data = True
            if latest_global_update is None or last_modified > latest_global_update:
                latest_global_update = last_modified
                latest_global_instrument = instrument_id

        instruments.append({
            "id": instrument_id,
            "name": instrument_id.replace("-", " "),
            "bronzeSize": bronze_size,
            "bronzeRows": counts[instrument_id],
            "silverRows": get_silver_rows(instrument_id),
            "lastUpdate": last_modified.isoformat() if last_modified else None,
        })

    return {
        "instruments": instruments,
        "refreshTime": latest_global_update.isoformat() if latest_global_update else None,
        "systemStatus": "ONLINE" if any_data else "DEGRADED",
        "lastUpdatedInstrument": latest_global_instrument,
    }


def get_inventory():
    now = time.time()
    if _inventory_cache["data"] is not None and now - _inventory_cache["ts"] < INVENTORY_TTL_SECONDS:
        return _inventory_cache["data"]
    inventory = compute_inventory()
    _inventory_cache["data"] = inventory
    _inventory_cache["ts"] = now
    return inventory


def get_mtd_cost():
    if not ENABLE_COST_KPI:
        return None

    now = time.time()
    if _cost_cache["data"] is not None and now - _cost_cache["ts"] < COST_TTL_SECONDS:
        return _cost_cache["data"]

    today = datetime.now(timezone.utc).date()
    start_date = today.replace(day=1).isoformat()
    end_date = (today + timedelta(days=1)).isoformat()
    client = boto3.client("ce", region_name="us-east-1")
    result = client.get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
    )
    total = result.get("ResultsByTime", [{}])[0].get("Total", {}).get("UnblendedCost", {})
    data = {
        "amount": round(float(total.get("Amount", 0)), 2),
        "currency": total.get("Unit", "USD"),
        "periodStart": start_date,
        "periodEnd": end_date,
    }
    _cost_cache["data"] = data
    _cost_cache["ts"] = now
    return data


def lambda_handler(event, context):
    method, path = get_route(event)
    if method == "OPTIONS":
        if route_matches(path, API_ROUTES["access_requests"]):
            return access_response(204)
        return response(204)

    if route_matches(path, INTERNAL_API_ROUTES["api_users"]) and method == "GET":
        identity, auth_response = require_team_role(event, {"Admin", "AccessManager"})
        return auth_response or get_team_api_users(event, identity)

    if route_matches(path, INTERNAL_API_ROUTES["api_users_revoke"]) and method == "POST":
        identity, auth_response = require_team_role(event, {"Admin", "AccessManager"})
        return auth_response or revoke_team_api_key(event, identity)

    if route_matches(path, INTERNAL_API_ROUTES["observations"]) and method == "GET":
        identity, auth_response = require_team_role(event, {"Admin", "Reviewer", "Viewer"})
        if auth_response:
            return auth_response
        result = get_silver_records(event)
        if result.get("statusCode") == 200:
            payload = json.loads(result["body"])
            payload["viewer"] = {"email": identity["email"], "roles": sorted(identity["roles"])}
            result["body"] = json.dumps(payload)
        return result

    if route_matches(path, INTERNAL_API_ROUTES["flags"]) and method == "POST":
        identity, auth_response = require_team_role(event, {"Admin", "Reviewer"})
        return auth_response or write_review_item(event, "flags", identity)

    if route_matches(path, INTERNAL_API_ROUTES["corrections"]) and method == "POST":
        identity, auth_response = require_team_role(event, {"Admin", "Reviewer"})
        return auth_response or write_review_item(event, "corrections", identity)

    if route_matches(path, INTERNAL_API_ROUTES["audit"]) and method == "GET":
        identity, auth_response = require_team_role(event, {"Admin", "Reviewer", "AccessManager"})
        return auth_response or get_admin_audit(event, identity)

    if method == "GET":
        canonical_path = legacy_route_target(path)
        if canonical_path:
            return redirect_response(event, canonical_path)

    if route_matches(path, API_ROUTES["access_requests"]) and method == "POST":
        return create_access_request(event)

    if route_matches(path, API_ROUTES["verify_access"]) and method == "GET":
        return verify_access_request(event)

    if route_matches(path, KEYED_API_ROUTES["timeseries"]) and method == "GET":
        auth_response = require_public_api_access(event, READ_SCOPES["timeseries"], force=True)
        return auth_response or get_series(event)

    if route_matches(path, KEYED_API_ROUTES["observations_export"]) and method == "GET":
        auth_response = require_public_api_access(event, READ_SCOPES["observations_export"], force=True)
        return auth_response or get_observations_export(event)

    if route_matches(path, KEYED_API_ROUTES["observations"]) and method == "GET":
        auth_response = require_public_api_access(event, READ_SCOPES["observations"], force=True)
        return auth_response or get_silver_records(event)

    if route_matches(path, KEYED_API_ROUTES["summary"]) and method == "GET":
        auth_response = require_public_api_access(event, READ_SCOPES["summary"], force=True)
        if auth_response:
            return auth_response
        inventory = get_inventory()
        return response(200, {
            "refreshTime": inventory["refreshTime"],
            "systemStatus": inventory["systemStatus"],
            "kpis": {
                "lastUpdatedInstrument": inventory["lastUpdatedInstrument"],
                "siteName": "Des Moines",
            },
            "instruments": inventory["instruments"],
        })

    if route_matches(path, API_ROUTES["timeseries"]) and method == "GET":
        auth_response = require_public_api_access(event, READ_SCOPES["timeseries"])
        if auth_response:
            return auth_response
        return get_series(event)

    if route_matches(path, API_ROUTES["observations_export"]) and method == "GET":
        auth_response = require_public_api_access(event, READ_SCOPES["observations_export"])
        if auth_response:
            return auth_response
        return get_observations_export(event)

    if route_matches(path, API_ROUTES["observations"]) and method == "GET":
        auth_response = require_public_api_access(event, READ_SCOPES["observations"])
        if auth_response:
            return auth_response
        return get_silver_records(event)

    if not route_matches(path, API_ROUTES["summary"]):
        return response(404, {"error": "Route not found"})

    auth_response = require_public_api_access(event, READ_SCOPES["summary"])
    if auth_response:
        return auth_response

    inventory = get_inventory()
    mtd_cost = None
    try:
        mtd_cost = get_mtd_cost()
    except Exception as exc:
        print(f"Cost Explorer error: {exc}")

    payload = {
        "refreshTime": inventory["refreshTime"],
        "systemStatus": inventory["systemStatus"],
        "kpis": {
            "lastUpdatedInstrument": inventory["lastUpdatedInstrument"],
            "siteName": "Des Moines",
        },
        "instruments": inventory["instruments"],
    }
    if mtd_cost is not None:
        payload["kpis"]["mtdCost"] = mtd_cost["amount"]
        payload["kpis"]["costCurrency"] = mtd_cost["currency"]
        payload["kpis"]["costPeriodStart"] = mtd_cost["periodStart"]
        payload["kpis"]["costPeriodEnd"] = mtd_cost["periodEnd"]

    return response(200, payload)
