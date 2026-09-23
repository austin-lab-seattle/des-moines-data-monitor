#!/usr/bin/env python3
"""Smoke test the public Des Moines Air Quality API.

Reads the dashboard summary and a few latest cleaned observations without
changing S3.

Usage:
    python scripts/quality/check_public_api.py
    python scripts/quality/check_public_api.py --instrument NO2-CAPS --limit 5
"""

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_API_BASE_URL = "https://yvhb48sthk.execute-api.us-west-2.amazonaws.com"
SUMMARY_PATH = "/air-quality/v1/summary"
OBSERVATIONS_PATH = "/air-quality/v1/observations"


def api_base_from_env():
    configured = os.environ.get("API_BASE_URL") or os.environ.get("VITE_API_URL")
    if not configured:
        return DEFAULT_API_BASE_URL
    return configured.rstrip("/").removesuffix(SUMMARY_PATH)


def fetch_json(url, api_key=None):
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body or "{}")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            payload = {"raw": body}
        return exc.code, payload
    except URLError as exc:
        raise RuntimeError(f"Could not reach API: {exc.reason}") from exc


def print_summary(api_base, api_key):
    status, payload = fetch_json(f"{api_base}{SUMMARY_PATH}", api_key)
    print(f"{SUMMARY_PATH} status: {status}")
    if status != 200:
        print(json.dumps(payload, indent=2))
        return False

    instruments = payload.get("instruments", [])
    print("Public API is reachable.")
    for instrument in instruments:
        print(
            f"  {instrument.get('id')}: "
            f"raw={instrument.get('bronzeRows')} "
            f"cleaned={instrument.get('silverRows')}"
        )
    return True


def print_observations(api_base, instrument, limit, start, end, api_key):
    params = {
        "instrument": instrument,
        "limit": str(limit),
        "order": "desc" if not start and not end else "asc",
    }
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    url = f"{api_base}{OBSERVATIONS_PATH}?{urlencode(params)}"
    status, payload = fetch_json(url, api_key)
    print(f"\n{OBSERVATIONS_PATH} status: {status}")

    if status == 404:
        print("Observations route is not available at this API URL yet.")
        print("Deploy the updated AWS routes with: python scripts/aws/deploy_backend.py")
        print(json.dumps(payload, indent=2))
        return False

    if status != 200:
        print(json.dumps(payload, indent=2))
        return False

    rows = payload.get("rows", [])
    columns = payload.get("columns", [])
    print(f"Instrument: {payload.get('instrument_id')}")
    print(f"Columns returned: {len(columns)}")
    print(f"Rows returned: {len(rows)}")
    print(f"Next cursor: {payload.get('next_cursor')}")

    display_columns = columns[:6]
    for index, row in enumerate(rows, start=1):
        values = row.get("values", {})
        sample = {column: values.get(column) for column in display_columns}
        print(f"\nRow {index}")
        print(f"  row_key: {row.get('row_key')}")
        print(f"  timestamp: {row.get('timestamp')}")
        print(f"  status: {row.get('status')}")
        print(f"  sample_values: {json.dumps(sample, ensure_ascii=False)}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Smoke test the public API.")
    parser.add_argument("--api-base-url", default=api_base_from_env())
    parser.add_argument("--instrument", default="NO2-CAPS")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--start", help="Optional ISO start time, for example 2026-03-02T00:00:00")
    parser.add_argument("--end", help="Optional ISO end time, for example 2026-03-02T00:05:00")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("AQ_API_KEY"),
        help="Optional API key. Defaults to AQ_API_KEY from the environment.",
    )
    args = parser.parse_args()

    api_base = args.api_base_url.rstrip("/").removesuffix(SUMMARY_PATH)
    print(f"API base: {api_base}")
    print(f"Instrument: {args.instrument}")
    print(f"Limit: {args.limit}")

    try:
        summary_ok = print_summary(api_base, args.api_key)
        rows_ok = print_observations(
            api_base,
            args.instrument,
            args.limit,
            args.start,
            args.end,
            args.api_key,
        )
    except RuntimeError as exc:
        print(exc)
        return 1

    return 0 if summary_ok and rows_ok else 1


if __name__ == "__main__":
    sys.exit(main())
