"""Hourly, per-instrument upload and observation freshness alerts.

Reading time comes from Silver rows, never the object's modification time.
Never-reporting instruments remain unarmed. Errors cannot clear an incident.
S3 state suppresses repeated alerts until the instrument genuinely recovers.
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import boto3

from lambda_api import datetime_for_instrument, record_timestamp, split_fields


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
if not math.isfinite(STALE_AFTER_HOURS) or STALE_AFTER_HOURS <= 0:
    raise ValueError("STALE_AFTER_HOURS must be a positive finite number")
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
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def newest_bronze_object(s3, instrument_id):
    newest = None
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{instrument_id}/bronze/"):
        for item in page.get("Contents", []):
            if item.get("Size", 0) <= 0 or item["Key"].endswith(("/", ".keep")):
                continue
            if newest is None or item["LastModified"] > newest["LastModified"]:
                newest = item
    return newest


def latest_reading(s3, instrument_id, previous, now):
    """Use the dashboard's timestamp parser and maximum valid observation time."""
    response = s3.get_object(Bucket=BUCKET, Key=f"{instrument_id}/silver/{instrument_id}_data.csv")
    body = response["Body"]
    etag = response.get("ETag")
    try:
        cached = parse_timestamp(previous.get("last_reading_at"))
        if etag and etag == previous.get("silver_etag") and cached and cached <= now + timedelta(minutes=5):
            return cached, etag
        lines = body.iter_lines() if hasattr(body, "iter_lines") else iter(body)
        columns = None
        latest = None
        future_rows = False
        for raw in lines:
            line = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
            if not line.strip():
                continue
            fields = split_fields(line)
            if columns is None:
                columns = fields
                continue
            if len(fields) != len(columns):
                continue
            moment = datetime_for_instrument(instrument_id, record_timestamp(instrument_id, columns, fields))
            if moment is None:
                continue
            moment = moment.replace(tzinfo=timezone.utc)
            if moment > now + timedelta(minutes=5):
                future_rows = True
                continue
            if latest is None or moment > latest:
                latest = moment
        if latest is None:
            raise ValueError("Silver contains no valid, non-future observation timestamp")
        # Recheck future-dated rows on later runs instead of caching them forever.
        return latest, None if future_rows else etag
    finally:
        body.close()


def load_state(s3):
    listing = s3.list_objects_v2(Bucket=BUCKET, Prefix=STATE_KEY, MaxKeys=1)
    if not any(item.get("Key") == STATE_KEY for item in listing.get("Contents", [])):
        return {}
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


def check_instrument(s3, instrument_id, previous, now):
    result = dict(previous)
    errors = {}
    latest_upload = parse_timestamp(previous.get("last_upload_at"))
    try:
        newest = newest_bronze_object(s3, instrument_id)
        if newest:
            latest_upload = newest["LastModified"]
            if latest_upload.tzinfo is None:
                latest_upload = latest_upload.replace(tzinfo=timezone.utc)
            if latest_upload > now + timedelta(minutes=5):
                raise ValueError("Upload timestamp is unexpectedly in the future")
            result["last_upload_key"] = newest["Key"]
    except Exception as exc:
        latest_upload = parse_timestamp(previous.get("last_upload_at"))
        errors["upload"] = str(exc)
    latest = parse_timestamp(previous.get("last_reading_at"))
    try:
        latest, etag = latest_reading(s3, instrument_id, previous, now)
        result["silver_etag"] = etag
    except Exception as exc:
        errors["reading"] = str(exc)
    armed = bool(previous.get("armed") or latest_upload or latest)
    reasons = []
    threshold = timedelta(hours=STALE_AFTER_HOURS)
    if latest_upload and now - latest_upload >= threshold:
        reasons.append("no-upload")
    if latest and now - latest >= threshold:
        reasons.append("stale-reading")
    # Once uploads begin, allow six hours for the first Silver observation.
    missing_since = parse_timestamp(previous.get("reading_missing_since"))
    if armed and latest is None:
        missing_since = missing_since or now
        if now - missing_since >= threshold:
            reasons.append("reading-unavailable")
    else:
        missing_since = None
    # A failed check cannot send a false recovery, even if the other channel is fresh.
    active = bool(reasons or (previous.get("alert_active") and errors))
    result.update(
        armed=armed, alert_active=active,
        status="stale" if active else "waiting-for-first-upload" if not armed else "unknown" if errors else "healthy",
        reasons=reasons, check_errors=errors,
        last_upload_at=latest_upload.isoformat() if latest_upload else None,
        last_reading_at=latest.isoformat() if latest else None,
        reading_missing_since=missing_since.isoformat() if missing_since else None,
        updated_at=now.isoformat(),
    )
    return result


def notification_text(now, instruments, alerts, recoveries):
    lines = [f"Des Moines instrument freshness check (UTC): {now.isoformat()}",
             f"Threshold: {STALE_AFTER_HOURS:g} hours. Each instrument is checked independently.", ""]
    for label, ids in [("ALERT", alerts), ("RECOVERED", recoveries)]:
        for instrument_id in ids:
            item = instruments[instrument_id]
            lines.extend([f"{label}: {instrument_id}",
                          f"Reasons: {', '.join(item['reasons']) or 'uploads and readings are fresh'}"])
            for name in ["upload", "reading"]:
                stamp = parse_timestamp(item[f"last_{name}_at"])
                age = f" ({(now - stamp).total_seconds() / 3600:.1f} hours old)" if stamp else ""
                lines.append(f"Last confirmed {name} (UTC): {stamp.isoformat() if stamp else 'not available'}{age}")
            if item.get("check_errors"):
                lines.append("Could not verify: " + ", ".join(item["check_errors"]))
            lines.append("")
    lines.extend(["For no-upload: check the field laptop, scheduled uploader, network, collector.log and credentials.",
                  "For stale-reading: check the instrument acquisition/export software and Silver rebuild.",
                  "A new upload or Silver rebuild alone does not prove new observations were collected.",
                  "This incident will not repeat hourly. A recovery email follows when both checks are fresh."])
    return "\n".join(lines)


def run_monitor(s3, ses, now=None, dry_run=False):
    now = now or utc_now()
    state = load_state(s3)
    previous = state.get("instruments", {})
    if state.get("armed") and not previous:
        # Carry over the legacy global checkpoint only for its actual instrument.
        legacy_instrument = (state.get("last_upload_key") or "").split("/")[0]
        if legacy_instrument in INSTRUMENT_IDS:
            previous = {legacy_instrument: {
                "armed": True, "last_upload_at": state.get("last_upload_at"),
                "last_upload_key": state.get("last_upload_key"),
                "alert_active": state.get("status") == "stale",
            }}
    with ThreadPoolExecutor(max_workers=5) as executor:
        checks = {instrument_id: executor.submit(check_instrument, s3, instrument_id,
                  previous.get(instrument_id, {}), now) for instrument_id in INSTRUMENT_IDS}
        instruments = {instrument_id: task.result() for instrument_id, task in checks.items()}
    alerts = [key for key, item in instruments.items()
              if item["alert_active"] and not previous.get(key, {}).get("alert_active")]
    recoveries = [key for key, item in instruments.items()
                  if not item["alert_active"] and previous.get(key, {}).get("alert_active")]
    subject = ("Des Moines instrument data alert: " + ", ".join(alerts)) if alerts else "Des Moines instrument data recovered: " + ", ".join(recoveries)
    text = notification_text(now, instruments, alerts, recoveries) if alerts or recoveries else None
    if text and not dry_run:
        send_email(ses, subject, text)
    status = "stale" if any(item["alert_active"] for item in instruments.values()) else "unknown" if any(item["status"] == "unknown" for item in instruments.values()) else "healthy" if any(item["armed"] for item in instruments.values()) else "waiting-for-first-upload"
    next_state = {"version": 2, "armed": any(item["armed"] for item in instruments.values()),
                  "status": status, "instruments": instruments, "updated_at": now.isoformat()}
    if not dry_run and next_state["armed"]:
        save_state(s3, next_state)
    return {**next_state, "dry_run": dry_run, "new_alerts": alerts, "recoveries": recoveries,
            "alert_sent": bool(alerts and not dry_run), "recovery_sent": bool(recoveries and not dry_run),
            "notification_preview": text if dry_run else None}


def lambda_handler(event, context):
    result = run_monitor(s3_client, ses_client, dry_run=event.get("dry_run") is True)
    print(json.dumps(result))
    return {"statusCode": 200, "body": json.dumps(result)}
