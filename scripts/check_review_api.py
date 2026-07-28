"""Quick smoke test for the dashboard review API.

Reads a few latest Silver rows from API Gateway without changing S3.

Usage:
    python3 scripts/check_review_api.py
    python3 scripts/check_review_api.py --instrument NO2-CAPS --limit 5
"""

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_API_BASE_URL = "https://yvhb48sthk.execute-api.us-west-2.amazonaws.com"


def api_base_from_env():
    configured = os.environ.get("API_BASE_URL") or os.environ.get("VITE_API_URL")
    if not configured:
        return DEFAULT_API_BASE_URL
    return configured.rstrip("/").removesuffix("/metrics")


def fetch_json(url):
    headers = {"Accept": "application/json"}
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


def print_metrics_summary(api_base):
    status, payload = fetch_json(f"{api_base}/metrics")
    print(f"/metrics status: {status}")
    if status != 200:
        print(json.dumps(payload, indent=2))
        return False

    instruments = payload.get("instruments", [])
    print("Dashboard API is reachable.")
    for instrument in instruments:
        print(
            f"  {instrument.get('id')}: "
            f"bronze={instrument.get('bronzeRows')} "
            f"silver={instrument.get('silverRows')}"
        )
    return True


def print_silver_rows(api_base, instrument, limit, start, end):
    params = {
        "instrument": instrument,
        "limit": str(limit),
        "order": "desc" if not start and not end else "asc",
    }
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    url = f"{api_base}/silver-records?{urlencode(params)}"
    status, payload = fetch_json(url)
    print(f"\n/silver-records status: {status}")

    if status == 404:
        print("Silver review route is not available at this API URL yet.")
        print("Deploy the updated AWS routes with: python3 scripts/deploy_aws.py")
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
    parser = argparse.ArgumentParser(description="Smoke test the review API.")
    parser.add_argument("--api-base-url", default=api_base_from_env())
    parser.add_argument("--instrument", default="NO2-CAPS")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--start", help="Optional ISO start time, for example 2026-03-02T00:00:00")
    parser.add_argument("--end", help="Optional ISO end time, for example 2026-03-02T00:05:00")
    args = parser.parse_args()

    api_base = args.api_base_url.rstrip("/").removesuffix("/metrics")
    print(f"API base: {api_base}")
    print(f"Instrument: {args.instrument}")
    print(f"Limit: {args.limit}")

    try:
        metrics_ok = print_metrics_summary(api_base)
        rows_ok = print_silver_rows(
            api_base,
            args.instrument,
            args.limit,
            args.start,
            args.end,
        )
    except RuntimeError as exc:
        print(exc)
        return 1

    return 0 if metrics_ok and rows_ok else 1


if __name__ == "__main__":
    sys.exit(main())
