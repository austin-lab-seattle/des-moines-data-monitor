import csv
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

cost_explorer_client = boto3.client("ce", region_name="us-east-1")
s3_client = boto3.client("s3")

BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
INSTRUMENT_IDS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]

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

    return {
        "statusCode": 200,
        "headers": {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
            "Content-Type": "application/json",
        },
        "body": json.dumps(payload),
    }
