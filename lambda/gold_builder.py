"""Gold builder Lambda (SMPS).

Reads the consolidated SMPS silver file and produces an hourly summary, which is
the analysis-ready "gold" product. For each clock hour it computes:

* number of scans in the hour,
* mean total particle concentration (#/cm3),
* mean median and mode diameter (nm),
* the ultrafine fraction (share of particles below 100 nm), which is the
  health-relevant metric for occupational exposure.

Output: {SMPS}/gold/SMPS_hourly_summary.csv (one row per hour).

Gold is instrument- and purpose-specific, so this is intentionally SMPS-only for
now. The exact metrics can be tuned with the science team. Scheduled daily.
"""

import csv
import io
import json
import os
from collections import defaultdict
from datetime import datetime

import boto3

s3_client = boto3.client("s3")
BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
INSTRUMENT_ID = "SMPS"
UFP_CUTOFF_NM = 100.0


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_column(header, needle):
    needle = needle.lower()
    for i, name in enumerate(header):
        if needle in name.strip().lower():
            return i
    return None


def lambda_handler(event, context):
    key = f"{INSTRUMENT_ID}/silver/{INSTRUMENT_ID}_data.csv"
    try:
        text = s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
    except Exception as exc:
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}

    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return {"statusCode": 200, "body": json.dumps({"hours": 0, "note": "no data rows"})}

    header = rows[0]
    dt_idx = _find_column(header, "DateTime Sample Start")
    tc_idx = _find_column(header, "Total Concentration")
    med_idx = _find_column(header, "Median (nm)")
    mode_idx = _find_column(header, "Mode (nm)")
    # Size-distribution columns are the ones whose header is a bare diameter number.
    size_bins = [(i, _num(name)) for i, name in enumerate(header) if _num(name) is not None]

    if dt_idx is None or tc_idx is None:
        return {"statusCode": 500, "body": json.dumps({"error": "expected SMPS columns not found"})}

    agg = defaultdict(lambda: {"n": 0, "tc": 0.0, "med": 0.0, "mode": 0.0, "ufp": 0.0})

    for row in rows[1:]:
        if len(row) <= tc_idx:
            continue
        try:
            dt = datetime.strptime(row[dt_idx].strip(), "%d/%m/%Y %H:%M:%S")
        except (ValueError, IndexError):
            continue
        hour = dt.strftime("%Y-%m-%d %H:00")

        bucket_row = agg[hour]
        bucket_row["n"] += 1

        tc = _num(row[tc_idx])
        if tc is not None:
            bucket_row["tc"] += tc
        if med_idx is not None and med_idx < len(row) and _num(row[med_idx]) is not None:
            bucket_row["med"] += _num(row[med_idx])
        if mode_idx is not None and mode_idx < len(row) and _num(row[mode_idx]) is not None:
            bucket_row["mode"] += _num(row[mode_idx])

        total = 0.0
        below = 0.0
        for i, diameter in size_bins:
            if i < len(row):
                value = _num(row[i])
                if value is not None:
                    total += value
                    if diameter < UFP_CUTOFF_NM:
                        below += value
        bucket_row["ufp"] += (below / total) if total > 0 else 0.0

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "hour_local", "n_scans", "mean_total_conc_per_cm3",
        "mean_median_nm", "mean_mode_nm", "mean_ufp_fraction_lt100nm",
    ])
    total_scans = 0
    for hour in sorted(agg):
        a = agg[hour]
        n = a["n"]
        total_scans += n
        writer.writerow([
            hour, n,
            round(a["tc"] / n, 1),
            round(a["med"] / n, 2),
            round(a["mode"] / n, 2),
            round(a["ufp"] / n, 4),
        ])

    body = out.getvalue()
    s3_client.put_object(
        Bucket=BUCKET,
        Key=f"{INSTRUMENT_ID}/gold/{INSTRUMENT_ID}_hourly_summary.csv",
        Body=body.encode("utf-8"),
        ContentType="text/csv; charset=utf-8",
    )

    return {
        "statusCode": 200,
        "body": json.dumps({"instrument": INSTRUMENT_ID, "hours": len(agg), "scans": total_scans}),
    }
