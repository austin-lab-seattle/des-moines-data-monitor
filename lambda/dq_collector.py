import csv
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")
cloudwatch_client = boto3.client("cloudwatch")

BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
INSTRUMENTS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]


def iter_s3_objects(prefix):
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for object_summary in page.get("Contents", []):
            key = object_summary["Key"]
            if key.endswith(".keep") or key.endswith("/"):
                continue
            yield object_summary


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
    """
    Download a sensor file from S3 and count actual data rows.
    """
    obj = s3_client.get_object(Bucket=BUCKET, Key=s3_key)
    raw = obj["Body"].read().decode("utf-8", errors="replace")
    return sum(1 for line in raw.splitlines() if is_data_row(instrument_id, line))


def lambda_handler(event, context):
    logger.info("Starting scheduled DQ Collector run")
    start_time = time.time()

    try:
        now = datetime.now(timezone.utc)
        metric_data = []

        for instrument_id in INSTRUMENTS:
            for tier in ["bronze", "silver", "gold"]:
                prefix = f"{instrument_id}/{tier}/"
                file_count = 0
                total_size = 0
                latest = None
                objects = list(iter_s3_objects(prefix))

                for object_summary in objects:
                    file_count += 1
                    total_size += object_summary["Size"]
                    if latest is None or object_summary["LastModified"] > latest:
                        latest = object_summary["LastModified"]

                metric_data.append({
                    "MetricName": f"{tier.capitalize()}Files",
                    "Dimensions": [{"Name": "Instrument", "Value": instrument_id}],
                    "Value": file_count,
                    "Unit": "Count",
                })
                metric_data.append({
                    "MetricName": f"{tier.capitalize()}Size",
                    "Dimensions": [{"Name": "Instrument", "Value": instrument_id}],
                    "Value": total_size,
                    "Unit": "Bytes",
                })

                if tier == "bronze" and latest:
                    freshness_hours = (now - latest).total_seconds() / 3600
                    metric_data.append({
                        "MetricName": "Freshness",
                        "Dimensions": [{"Name": "Instrument", "Value": instrument_id}],
                        "Value": round(freshness_hours, 2),
                        "Unit": "None",
                    })

                if tier == "bronze":
                    total_rows = 0
                    for object_summary in objects:
                        key = object_summary["Key"]
                        try:
                            total_rows += count_data_rows(instrument_id, key)
                        except Exception as exc:
                            logger.warning(f"Could not count rows in {key}: {exc}")
                    metric_data.append({
                        "MetricName": "BronzeRows",
                        "Dimensions": [{"Name": "Instrument", "Value": instrument_id}],
                        "Value": total_rows,
                        "Unit": "Count",
                    })

        end_time = time.time()
        duration_ms = (end_time - start_time) * 1000

        metric_data.extend([
            {
                "MetricName": "LambdaDuration",
                "Value": duration_ms,
                "Unit": "Milliseconds",
            },
            {
                "MetricName": "LambdaSuccess",
                "Value": 1,
                "Unit": "Count",
            },
        ])

        for start_index in range(0, len(metric_data), 20):
            batch = metric_data[start_index:start_index + 20]
            cloudwatch_client.put_metric_data(
                Namespace="AirQuality/Pipeline",
                MetricData=batch,
            )

        logger.info(f"Published {len(metric_data)} metrics to CloudWatch")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "DQ Collector run complete",
                "metrics_published": len(metric_data),
            }),
        }

    except Exception as exc:
        logger.error(f"Error in DQ Collector: {exc}")
        cloudwatch_client.put_metric_data(
            Namespace="AirQuality/Pipeline",
            MetricData=[{
                "MetricName": "LambdaSuccess",
                "Value": 0,
                "Unit": "Count",
            }],
        )
        raise
