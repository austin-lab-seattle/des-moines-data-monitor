import csv
import json
import os
import re
import time
from datetime import datetime, timezone

import boto3

cost_explorer_client = boto3.client("ce", region_name="us-east-1")
s3_client = boto3.client("s3")

BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
INSTRUMENT_IDS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]

# Cost Explorer is slow and the value moves slowly, so cache it in the warm
# container. Every Refresh still recounts the rows live; only the cost tile is cached.
COST_TTL_SECONDS = 3600
_cost_cache = {"value": None, "ts": 0.0}


def iter_s3_objects(prefix):
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for object_summary in page.get("Contents", []):
            key = object_summary["Key"]
            if key.endswith(".keep") or key.endswith("/"):
                continue
            yield object_summary


# --- Row detection: which lines are real data rows vs headers and comments ---

def split_fields(line):
    if "\t" in line:
        return [field.strip().strip('"') for field in line.split("\t")]
    try:
        return [field.strip().strip('"') for field in next(csv.reader([line]))]
    except csv.Error:
        return []


def is_float(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def is_data_row(instrument_id, line):
    stripped = line.strip().lstrip("﻿")
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
        return (
            len(fields) >= 3
            and re.match(r"^\d{4}-\d{2}-\d{2}$", first)
            and re.match(r"^\d{2}:\d{2}:\d{2}$", second)
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


def count_data_rows(instrument_id, s3_key):
    obj = s3_client.get_object(Bucket=BUCKET, Key=s3_key)
    raw = obj["Body"].read().decode("utf-8", errors="replace")
    return sum(1 for line in raw.splitlines() if is_data_row(instrument_id, line))


def scan_instrument(instrument_id):
    """One live pass over an instrument's bronze prefix.

    Returns (row_count, total_bytes, latest_modified). This runs on every API
    call, so the dashboard's row count and size are always current when the user
    hits Refresh, with no dependence on the hourly collector or CloudWatch.
    """
    rows = 0
    size = 0
    latest = None
    for object_summary in iter_s3_objects(f"{instrument_id}/bronze/"):
        size += object_summary["Size"]
        if latest is None or object_summary["LastModified"] > latest:
            latest = object_summary["LastModified"]
        try:
            rows += count_data_rows(instrument_id, object_summary["Key"])
        except Exception as exc:
            print(f"Could not count rows in {object_summary['Key']}: {exc}")
    return rows, size, latest


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
    month_to_date_cost = get_month_to_date_cost()

    instruments = []
    latest_global_update = None
    latest_global_instrument = "NONE"
    any_data = False

    for instrument_id in INSTRUMENT_IDS:
        bronze_rows, bronze_size, last_modified = scan_instrument(instrument_id)
        last_update_iso = last_modified.isoformat() if last_modified else None

        if last_modified:
            any_data = True
            if latest_global_update is None or last_modified > latest_global_update:
                latest_global_update = last_modified
                latest_global_instrument = instrument_id

        instruments.append({
            "id": instrument_id,
            "name": instrument_id.replace("-", " "),
            "bronzeSize": bronze_size,
            "bronzeRows": bronze_rows,
            "lastUpdate": last_update_iso,
        })

    refresh_time_iso = latest_global_update.isoformat() if latest_global_update else None
    system_status = "ONLINE" if any_data else "DEGRADED"

    payload = {
        "refreshTime": refresh_time_iso,
        "systemStatus": system_status,
        "kpis": {
            "mtdCost": month_to_date_cost,
            "costScope": "AWS account MTD",
            "lastUpdatedInstrument": latest_global_instrument,
            "siteName": "Des Moines",
        },
        "instruments": instruments,
    }

    return {
        "statusCode": 200,
        "headers": {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
            "Content-Type": "application/json",
        },
        "body": json.dumps(payload),
    }
