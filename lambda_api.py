import csv
import hashlib
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

cost_explorer_client = boto3.client("ce", region_name="us-east-1")
s3_client = boto3.client("s3")

BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
INSTRUMENT_IDS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]
REVIEW_API_KEY = os.environ.get("REVIEW_API_KEY")

# The cost tile changes slowly and the Cost Explorer call is slow; cache an hour.
COST_TTL_SECONDS = 3600
_cost_cache = {"value": None, "ts": 0.0}

# Row/size counts are recomputed live from S3, but cached briefly so a burst of
# refreshes does not each trigger a full scan. New data only lands every few
# minutes, so a short cache still feels live on the dashboard.
INVENTORY_TTL_SECONDS = 30
_inventory_cache = {"data": None, "ts": 0.0}

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
        "Access-Control-Allow-Headers": "content-type,x-api-key,authorization",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": "" if payload is None else json.dumps(payload),
    }


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
    path = event.get("rawPath") or http_context.get("path") or event.get("path") or "/metrics"
    return method.upper(), path.rstrip("/") or "/"


def query_params(event):
    return event.get("queryStringParameters") or {}


def normalized_headers(event):
    return {key.lower(): value for key, value in (event.get("headers") or {}).items()}


def require_review_auth(event):
    if not REVIEW_API_KEY:
        return False, response(
            503,
            {
                "error": "Review writes are not configured",
                "message": "Set REVIEW_API_KEY on the API Lambda before enabling write routes.",
            },
        )
    headers = normalized_headers(event)
    params = query_params(event)
    provided = (
        headers.get("x-api-key")
        or headers.get("authorization", "").removeprefix("Bearer ").strip()
        or params.get("api_key")
        or params.get("review_key")
    )
    if provided != REVIEW_API_KEY:
        return False, response(403, {"error": "Forbidden"})
    return True, None


# --- Row detection: which lines are real data rows vs headers and comments ---

def clean_field(field):
    return field.strip().lstrip("\ufeff").strip('"').strip()


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


def parse_datetime_value(value):
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

    formats = [
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def datetime_for_compare(value):
    parsed = parse_datetime_value(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


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
        timestamp_compare = datetime_for_compare(timestamp_raw)
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
            "timestamp_iso": parse_datetime_value(timestamp_raw).isoformat() if parse_datetime_value(timestamp_raw) else None,
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


def get_silver_records(event):
    params = query_params(event)
    instrument_id = params.get("instrument") or params.get("instrument_id")
    if not is_valid_instrument(instrument_id):
        return response(400, {"error": "Invalid or missing instrument"})

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
        return response(404, {"error": "Silver records not found", "instrument_id": instrument_id})
    except Exception as exc:
        print(f"Silver records error: {exc}")
        return response(500, {"error": "Could not read silver records"})

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


def write_review_item(event, review_type):
    authorized, auth_response = require_review_auth(event)
    if not authorized:
        return auth_response

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
    s3_client.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(item, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    item["_s3_key"] = key
    return response(201, item)


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


def get_month_to_date_cost():
    now = time.time()
    if _cost_cache["value"] is not None and now - _cost_cache["ts"] < COST_TTL_SECONDS:
        return _cost_cache["value"]
    try:
        now_utc = datetime.now(timezone.utc)
        start_of_month = now_utc.replace(day=1).strftime("%Y-%m-%d")
        end_date = now_utc.strftime("%Y-%m-%d")
        if start_of_month == end_date:
            value = 0.00
        else:
            response = cost_explorer_client.get_cost_and_usage(
                TimePeriod={"Start": start_of_month, "End": end_date},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            amount = response["ResultsByTime"][0]["Total"]["UnblendedCost"]["Amount"]
            value = round(float(amount), 2)
    except Exception as exc:
        print(f"Cost Explorer error: {exc}")
        value = "N/A"
    _cost_cache["value"] = value
    _cost_cache["ts"] = now
    return value


def lambda_handler(event, context):
    method, path = get_route(event)
    if method == "OPTIONS":
        return response(204)

    if path.endswith("/silver-records") and method == "GET":
        return get_silver_records(event)

    if path.endswith("/record-flags"):
        if method == "GET":
            return get_review_items(event, "flags")
        if method == "POST":
            return write_review_item(event, "flags")

    if path.endswith("/record-corrections"):
        if method == "GET":
            return get_review_items(event, "corrections")
        if method == "POST":
            return write_review_item(event, "corrections")

    if not path.endswith("/metrics"):
        return response(404, {"error": "Route not found"})

    inventory = get_inventory()
    month_to_date_cost = get_month_to_date_cost()

    payload = {
        "refreshTime": inventory["refreshTime"],
        "systemStatus": inventory["systemStatus"],
        "kpis": {
            "mtdCost": month_to_date_cost,
            "costScope": "AWS account MTD",
            "lastUpdatedInstrument": inventory["lastUpdatedInstrument"],
            "siteName": "Des Moines",
        },
        "instruments": inventory["instruments"],
    }

    return response(200, payload)
