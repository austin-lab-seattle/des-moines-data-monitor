import json
import os
import boto3
from datetime import datetime, timedelta, timezone

cloudwatch_client = boto3.client("cloudwatch")
cost_explorer_client = boto3.client("ce", region_name="us-east-1")
s3_client = boto3.client("s3")

BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
CW_NAMESPACE = "AirQuality/Pipeline"
INSTRUMENT_IDS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]


def iter_s3_objects(prefix):
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for object_summary in page.get("Contents", []):
            key = object_summary["Key"]
            if key.endswith(".keep") or key.endswith("/"):
                continue
            yield object_summary


def get_latest_cloudwatch_metric(metric_name, dimensions=None, hours=168):
    """Fetch the latest datapoint for a CloudWatch metric over the past week."""
    try:
        request = {
            "Namespace": CW_NAMESPACE,
            "MetricName": metric_name,
            "StartTime": datetime.now(timezone.utc) - timedelta(hours=hours),
            "EndTime": datetime.now(timezone.utc),
            "Period": 3600,
            "Statistics": ["Maximum"],
        }
        if dimensions:
            request["Dimensions"] = dimensions
        datapoints = cloudwatch_client.get_metric_statistics(**request).get("Datapoints", [])
        if not datapoints:
            return None
        return sorted(datapoints, key=lambda point: point["Timestamp"])[-1]
    except Exception as exc:
        print(f"Error fetching CloudWatch metric {metric_name}: {exc}")
        return None


def get_s3_last_modified(instrument_id):
    """
    Scan the instrument's bronze prefix in S3 and return the exact
    LastModified timestamp of the most recently uploaded file.
    """
    try:
        prefix = f"{instrument_id}/bronze/"
        latest = None
        for object_summary in iter_s3_objects(prefix):
            if latest is None or object_summary["LastModified"] > latest:
                latest = object_summary["LastModified"]
        return latest  # timezone-aware datetime or None
    except Exception as exc:
        print(f"S3 scan error for {instrument_id}: {exc}")
        return None


def get_month_to_date_cost():
    try:
        now = datetime.now(timezone.utc)
        start_of_month = now.replace(day=1).strftime("%Y-%m-%d")
        end_date = now.strftime("%Y-%m-%d")
        if start_of_month == end_date:
            return 0.00
        response = cost_explorer_client.get_cost_and_usage(
            TimePeriod={"Start": start_of_month, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
        cost = response["ResultsByTime"][0]["Total"]["UnblendedCost"]["Amount"]
        return round(float(cost), 2)
    except Exception as exc:
        print(f"Cost Explorer error: {exc}")
        return "N/A"


def lambda_handler(event, context):
    month_to_date_cost = get_month_to_date_cost()

    instruments = []
    latest_global_update = None
    latest_global_instrument = "NONE"
    any_data = False

    for instrument_id in INSTRUMENT_IDS:
        dimensions = [{"Name": "Instrument", "Value": instrument_id}]

        size_datapoint = get_latest_cloudwatch_metric("BronzeSize", dimensions)
        rows_datapoint = get_latest_cloudwatch_metric("BronzeRows", dimensions)

        bronze_size = int(size_datapoint.get("Maximum", 0)) if size_datapoint else 0
        bronze_rows = int(rows_datapoint.get("Maximum", 0)) if rows_datapoint else 0

        last_modified = get_s3_last_modified(instrument_id)
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
