"""Silver builder Lambda.

Consolidates each instrument's many raw bronze batch files into a single clean
file in the silver layer. For each instrument it:

* merges every bronze object (in chronological order);
* keeps only real data rows (the same `is_data_row` logic the dashboard uses, so
  headers, comment lines, and the instrument metadata preamble are filtered out);
* drops duplicate rows (the at-least-once duplicates and any re-uploads);
* writes two objects:
    {instrument}/silver/{instrument}_data.csv      column header + unique rows
    {instrument}/silver/{instrument}_metadata.txt  preamble/header + provenance

It does a full rebuild each run, so it is safe to invoke repeatedly (idempotent).
Invoke on demand from the Lambda console or the CLI:

    aws lambda invoke --function-name aq-silver-builder /dev/stdout
"""

import csv
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

s3_client = boto3.client("s3")

BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
INSTRUMENT_IDS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]
MAX_WORKERS = 16


def list_bronze_keys(instrument_id):
    paginator = s3_client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{instrument_id}/bronze/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".keep") or key.endswith("/"):
                continue
            keys.append(key)
    return sorted(keys)


def download_text(key):
    body = s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return key, body.decode("utf-8", errors="replace")


# --- Row detection: mirrors lambda_api.py ---------------------------------

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


def consolidate(instrument_id, ordered_texts):
    """Return (header, data_rows, metadata_lines, total_rows, duplicates)."""
    header = None
    data_rows = []
    seen_rows = set()
    metadata = []
    seen_meta = set()
    total = 0
    duplicates = 0

    for _key, text in ordered_texts:
        last_nondata = None
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            if is_data_row(instrument_id, line):
                if header is None and last_nondata is not None:
                    header = last_nondata
                total += 1
                if line in seen_rows:
                    duplicates += 1
                else:
                    seen_rows.add(line)
                    data_rows.append(line)
            elif line.strip():
                last_nondata = line
                if line not in seen_meta:
                    seen_meta.add(line)
                    metadata.append(line)

    return header, data_rows, metadata, total, duplicates


def write_silver(instrument_id, header, data_rows, metadata, stats):
    data_body = ""
    if header:
        data_body += header + "\n"
    data_body += "\n".join(data_rows) + ("\n" if data_rows else "")

    meta_lines = [
        f"# Silver metadata for {instrument_id}",
        f"# generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"# bronze_objects: {stats['objects']}",
        f"# data_rows_total: {stats['total']}",
        f"# data_rows_unique: {stats['unique']}",
        f"# duplicates_removed: {stats['duplicates']}",
        "# --- header / preamble lines from bronze ---",
    ] + metadata
    meta_body = "\n".join(meta_lines) + "\n"

    s3_client.put_object(
        Bucket=BUCKET,
        Key=f"{instrument_id}/silver/{instrument_id}_data.csv",
        Body=data_body.encode("utf-8"),
        ContentType="text/csv; charset=utf-8",
    )
    s3_client.put_object(
        Bucket=BUCKET,
        Key=f"{instrument_id}/silver/{instrument_id}_metadata.txt",
        Body=meta_body.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )


def build_instrument(instrument_id):
    keys = list_bronze_keys(instrument_id)
    if not keys:
        return {"objects": 0, "rows": 0, "unique": 0, "duplicates": 0, "written": False}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        texts = list(pool.map(download_text, keys))
    texts.sort(key=lambda kt: kt[0])

    header, data_rows, metadata, total, duplicates = consolidate(instrument_id, texts)
    stats = {
        "objects": len(keys),
        "total": total,
        "unique": len(data_rows),
        "duplicates": duplicates,
    }
    write_silver(instrument_id, header, data_rows, metadata, stats)
    return {
        "objects": len(keys),
        "rows": total,
        "unique": len(data_rows),
        "duplicates": duplicates,
        "written": True,
    }


def lambda_handler(event, context):
    results = {}
    for instrument_id in INSTRUMENT_IDS:
        try:
            results[instrument_id] = build_instrument(instrument_id)
        except Exception as exc:
            results[instrument_id] = {"error": str(exc)}

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Silver build complete",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "instruments": results,
        }),
    }
