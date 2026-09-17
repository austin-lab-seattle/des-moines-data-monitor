"""Hourly monitor for stalled Des Moines Bronze uploads.

The monitor arms only after the first real Bronze object appears. Once armed,
it sends one email when the newest upload is older than the configured
threshold and one recovery email when a fresh upload arrives. State is kept in
S3 so repeated hourly invocations do not spam the research team.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import boto3


BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
INSTRUMENT_IDS = [
    value.strip()
    for value in os.environ.get(
        "INSTRUMENT_IDS", "BC-MA200,CO2-LICOR,NEPH-PM25,NO2-CAPS,SMPS"
    ).split(",")
    if value.strip()
]
STATE_KEY = os.environ.get(
    "UPLOAD_MONITOR_STATE_KEY", "_monitor/upload_freshness_state.json"
)
STALE_AFTER_HOURS = float(os.environ.get("STALE_AFTER_HOURS", "6"))
FROM_EMAIL = os.environ.get("ALERT_FROM_EMAIL", "")
TO_EMAILS = [
    value.strip()
    for value in os.environ.get("ALERT_TO_EMAILS", "").split(",")
    if value.strip()
]
CC_EMAILS = [
    value.strip()
    for value in os.environ.get("ALERT_CC_EMAILS", "").split(",")
    if value.strip()
]

s3_client = boto3.client("s3")
ses_client = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def utc_now():
    return datetime.now(timezone.utc)


def parse_timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def newest_bronze_object(s3):
    newest = None
    for instrument_id in INSTRUMENT_IDS:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=BUCKET, Prefix=f"{instrument_id}/bronze/"
        ):
            for item in page.get("Contents", []):
                if item.get("Size", 0) <= 0 or item["Key"].endswith("/"):
                    continue
                if newest is None or item["LastModified"] > newest["LastModified"]:
                    newest = item
    return newest


def load_state(s3):
    try:
        response = s3.get_object(Bucket=BUCKET, Key=STATE_KEY)
        return json.loads(response["Body"].read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        return {}
    except Exception as exc:
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if error_code in {"NoSuchKey", "404"}:
            return {}
        raise


def save_state(s3, state):
    s3.put_object(
        Bucket=BUCKET,
        Key=STATE_KEY,
        Body=json.dumps(state, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def send_email(ses, subject, text):
    if not FROM_EMAIL or not TO_EMAILS:
        raise RuntimeError("ALERT_FROM_EMAIL and ALERT_TO_EMAILS are required")
    destination = {"ToAddresses": TO_EMAILS}
    if CC_EMAILS:
        destination["CcAddresses"] = CC_EMAILS
    ses.send_email(
        Source=FROM_EMAIL,
        Destination=destination,
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": text, "Charset": "UTF-8"}},
        },
    )


def stale_message(now, latest_at, latest_key):
    age_hours = (now - latest_at).total_seconds() / 3600
    return (
        "The Des Moines field-data uploader has not written a new Bronze object "
        f"for {age_hours:.1f} hours.\n\n"
        f"Alert threshold: {STALE_AFTER_HOURS:g} hours\n"
        f"Last upload (UTC): {latest_at.isoformat()}\n"
        f"Last object: s3://{BUCKET}/{latest_key}\n\n"
        "Please check the field laptop, Windows scheduled task, network connection, "
        "collector.log, and AWS credentials. This alert will not repeat hourly."
    )


def recovery_message(now, latest_at, latest_key):
    return (
        "Des Moines Bronze uploads have resumed.\n\n"
        f"Recovery detected (UTC): {now.isoformat()}\n"
        f"Newest upload (UTC): {latest_at.isoformat()}\n"
        f"Newest object: s3://{BUCKET}/{latest_key}\n\n"
        "The stale-upload alert is now cleared."
    )


def run_monitor(s3, ses, now=None):
    now = now or utc_now()
    state = load_state(s3)
    newest = newest_bronze_object(s3)

    if newest:
        latest_at = newest["LastModified"]
        if latest_at.tzinfo is None:
            latest_at = latest_at.replace(tzinfo=timezone.utc)
        latest_key = newest["Key"]
    else:
        latest_at = parse_timestamp(state.get("last_upload_at"))
        latest_key = state.get("last_upload_key")

    if not state.get("armed") and not newest:
        return {"armed": False, "status": "waiting-for-first-upload"}

    previous_status = state.get("status")
    stale = latest_at is not None and now - latest_at > timedelta(
        hours=STALE_AFTER_HOURS
    )
    alert_sent = False
    recovery_sent = False

    if stale and previous_status != "stale":
        send_email(
            ses,
            "Des Moines data upload alert: no new data for 6 hours",
            stale_message(now, latest_at, latest_key),
        )
        alert_sent = True
    elif not stale and previous_status == "stale":
        send_email(
            ses,
            "Des Moines data upload recovered",
            recovery_message(now, latest_at, latest_key),
        )
        recovery_sent = True

    next_state = {
        **state,
        "armed": True,
        "status": "stale" if stale else "healthy",
        "last_upload_at": latest_at.isoformat() if latest_at else None,
        "last_upload_key": latest_key,
        "updated_at": now.isoformat(),
    }
    if alert_sent:
        next_state["stale_alert_sent_at"] = now.isoformat()
    if recovery_sent:
        next_state["recovery_sent_at"] = now.isoformat()
    save_state(s3, next_state)

    return {
        "armed": True,
        "status": next_state["status"],
        "last_upload_at": next_state["last_upload_at"],
        "alert_sent": alert_sent,
        "recovery_sent": recovery_sent,
    }


def lambda_handler(event, context):
    result = run_monitor(s3_client, ses_client)
    print(json.dumps(result))
    return {"statusCode": 200, "body": json.dumps(result)}
