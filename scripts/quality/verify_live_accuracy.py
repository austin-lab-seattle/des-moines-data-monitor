"""Read-only reconciliation of live API readings/means against an S3 snapshot.

No fixtures, uploads, Lambda invocations, or production writes. Requires AWS
CLI read access. Decimal arithmetic is independent of the API's float mean.
"""
import argparse
import csv
import json
import math
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

MEASUREMENTS = {
    "BC-MA200": "BC1", "CO2-LICOR": "CO2_(umol_mol-1)",
    "NEPH-PM25": "PM2.5 (µg/m³)", "NO2-CAPS": "Concentration",
    "SMPS": "Total Concentration (#/cm³)",
}


def aws_environment(credentials_file):
    environment = dict(os.environ)
    if credentials_file:
        credentials, _ = json.JSONDecoder().raw_decode(Path(credentials_file).read_text())
        environment.update(AWS_ACCESS_KEY_ID=credentials["aws_access_key_id"], AWS_SECRET_ACCESS_KEY=credentials["aws_secret_access_key"])
        if credentials.get("aws_session_token"):
            environment["AWS_SESSION_TOKEN"] = credentials["aws_session_token"]
        else:
            environment.pop("AWS_SESSION_TOKEN", None)
    return environment


def aws_json(arguments, environment):
    return json.loads(subprocess.check_output(["aws", *arguments, "--region", "us-west-2", "--output", "json"], env=environment))


def fetch_api(base, resource, **query):
    started = time.monotonic()
    try:
        with urlopen(f"{base.rstrip('/')}/air-quality/v1/{resource}?{urlencode(query)}", timeout=40) as response:
            return response.status, json.load(response), round(time.monotonic() - started, 2)
    except HTTPError as exc:
        return exc.code, json.load(exc), round(time.monotonic() - started, 2)


def source_timestamp(instrument, values):
    if instrument == "SMPS":
        raw = values.get("DateTime Sample Start", "")
    elif instrument == "CO2-LICOR":
        raw = f"{values.get('System_Date_(Y-M-D)', '')} {values.get('System_Time_(h:m:s)', '')}"
    elif instrument == "BC-MA200":
        raw = values.get("Date / time local") or f"{values.get('Date local (yyyy/MM/dd)', '')} {values.get('Time local (hh:mm:ss)', '')}"
    else:
        raw = values.get("Timestamp" if instrument == "NO2-CAPS" else "Date_Time", "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ["%d/%m/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"]:
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("America/Los_Angeles"))).astimezone(timezone.utc)


def verify_instrument(base, bucket, instrument, environment):
    key = f"{instrument}/silver/{instrument}_data.csv"
    column = MEASUREMENTS[instrument]
    for _attempt in range(3):
        head_command = ["s3api", "head-object", "--bucket", bucket, "--key", key]
        with tempfile.TemporaryDirectory(prefix="des-moines-accuracy-") as temporary:
            path = Path(temporary) / "source.csv"
            try:
                metadata = aws_json(["s3api", "get-object", "--bucket", bucket, "--key", key, str(path)], environment)
            except subprocess.CalledProcessError:
                # Do not call an inaccessible source verified. A 404 API with
                # an independently confirmed missing S3 object is honest no-data.
                probe = subprocess.run(["aws", *head_command, "--region", "us-west-2"], env=environment, capture_output=True, text=True)
                if "404" not in probe.stderr and "Not Found" not in probe.stderr and "NoSuchKey" not in probe.stderr:
                    raise RuntimeError(f"Cannot verify the {instrument} source")
                status, _, _ = fetch_api(base, "observations", instrument=instrument, limit=8, order="desc")
                assert status == 404, (instrument, status)
                return {"instrument": instrument, "result": "no source data; API correctly returns 404"}
            with path.open(newline="", encoding="utf-8-sig") as source:
                reader = csv.reader(source)
                columns = next(reader)
                records = []
                for index, fields in enumerate(reader):
                    if len(fields) != len(columns):
                        continue
                    values = {name: field.strip() for name, field in zip(columns, fields)}
                    stamp = source_timestamp(instrument, values)
                    if stamp is not None:
                        records.append((stamp, index, values))
            assert column in columns, (instrument, "unrecognized source column")
            records.sort(key=lambda item: (item[0], item[1]), reverse=True)
            status, observations, observation_seconds = fetch_api(base, "observations", instrument=instrument, limit=8, order="desc")
            assert status == 200, (instrument, "observations", status)
            # Reconcile a complete recent source hour, never an arbitrary last-N
            # subset that could produce a different (and misleading) mean.
            hour = records[0][0].replace(minute=0, second=0, microsecond=0)
            from datetime import timedelta
            end = hour + timedelta(hours=1) - timedelta(microseconds=1)
            status, series, series_seconds = fetch_api(base, "timeseries", instrument=instrument, measurement=column, start=hour.isoformat(), end=end.isoformat())
            after = aws_json(head_command, environment)
            if metadata["ETag"] != after["ETag"]:
                continue
            assert len(observations["rows"]) == min(8, len(records)), instrument
            for actual, (stamp, _index, values) in zip(observations["rows"], records[:8]):
                assert actual["values"] == values, (instrument, "source values changed")
                assert datetime.fromisoformat(actual["timestamp_iso"].replace("Z", "+00:00")) == stamp, (instrument, "timestamp mismatch")
            decimals = []
            for stamp, _index, values in records:
                if not hour <= stamp <= end:
                    continue
                try:
                    value = Decimal(values[column])
                    if value.is_finite() and math.isfinite(float(value)):
                        decimals.append(value)
                except (ArithmeticError, ValueError):
                    continue
            assert status == 200, (instrument, "series", status)
            assert series["measurement"] == column and series["aggregation"] == "sample_mean", instrument
            points = series["series"]
            if decimals:
                expected = sum(decimals) / len(decimals)
                assert len(points) == 1 and points[0]["n"] == len(decimals), (instrument, "sample count")
                assert abs(Decimal(str(points[0]["v"])) - expected) <= Decimal("0.000501"), (instrument, "incorrect mean")
                assert datetime.fromisoformat(points[0]["t"].replace("Z", "+00:00")) == hour, instrument
            else:
                assert not points, (instrument, "fabricated readings")
            return {"instrument": instrument, "result": "matched source snapshot", "individual_rows_checked": len(observations["rows"]), "hour_samples_checked": len(decimals), "observations_seconds": observation_seconds, "series_seconds": series_seconds, "source_etag": metadata["ETag"]}
    raise RuntimeError(f"{instrument} source changed during all three checks")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="https://yvhb48sthk.execute-api.us-west-2.amazonaws.com")
    parser.add_argument("--bucket", default="des-moines-data-pipeline-austinlab")
    parser.add_argument("--credentials-file")
    arguments = parser.parse_args()
    environment = aws_environment(arguments.credentials_file)
    for instrument in MEASUREMENTS:
        print(json.dumps(verify_instrument(arguments.api_base, arguments.bucket, instrument, environment)), flush=True)


if __name__ == "__main__":
    main()
