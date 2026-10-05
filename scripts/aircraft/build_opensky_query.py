#!/usr/bin/env python3
"""Build a bounded OpenSky Trino query for one local analysis window.

This does not call OpenSky or require credentials. Approved researchers run the
generated SQL in the OpenSky Trino client, export CSV, then import that file
with import_aircraft.py or through the dashboard.
"""

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO / "frontend" / "public" / "aircraft" / "config.json"


def parse_local(value, zone):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError("local date/time must not include an offset; --timezone supplies it")
    return parsed.replace(tzinfo=zone).astimezone(timezone.utc)


def hour_partitions(start_utc, end_utc):
    current = start_utc.replace(minute=0, second=0, microsecond=0)
    values = []
    while current <= end_utc:
        values.append(int(current.timestamp()))
        current += timedelta(hours=1)
    return values


def build_query(latitude, longitude, radius_km, start_utc, end_utc):
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(latitude)))
    partitions = ", ".join(str(value) for value in hour_partitions(start_utc, end_utc))
    return f"""-- Bounded OpenSky state-vector export for the aircraft analysis POC.
-- Altitudes are reported geometric/barometric values in metres, not AGL.
SELECT
  time AS timestamp,
  icao24 AS aircraft_id,
  trim(callsign) AS callsign,
  lat AS latitude,
  lon AS longitude,
  geoaltitude,
  baroaltitude,
  CAST(NULL AS VARCHAR) AS aircraft_type
FROM state_vectors_data4
WHERE hour IN ({partitions})
  AND time BETWEEN {int(start_utc.timestamp())} AND {int(end_utc.timestamp())}
  AND lat BETWEEN {latitude - lat_delta:.6f} AND {latitude + lat_delta:.6f}
  AND lon BETWEEN {longitude - lon_delta:.6f} AND {longitude + lon_delta:.6f}
  AND lat IS NOT NULL
  AND lon IS NOT NULL
  AND lastcontact >= time - 2
ORDER BY time, icao24;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--date", help="Local YYYY-MM-DD; selects the entire local day")
    parser.add_argument("--start-local", help="Local YYYY-MM-DDTHH:MM")
    parser.add_argument("--end-local", help="Local YYYY-MM-DDTHH:MM")
    parser.add_argument("--timezone", help="Override the configured IANA timezone")
    parser.add_argument("--latitude", type=float)
    parser.add_argument("--longitude", type=float)
    parser.add_argument("--radius-km", type=float, default=10.0)
    parser.add_argument("--output", type=Path, help="Write SQL here instead of stdout")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    site = config["site"]
    zone = ZoneInfo(args.timezone or site["timezone"])
    latitude = args.latitude if args.latitude is not None else float(site["latitude"])
    longitude = args.longitude if args.longitude is not None else float(site["longitude"])
    if not 0 < args.radius_km <= 100:
        parser.error("--radius-km must be greater than 0 and no more than 100")

    if args.date:
        start_local = f"{args.date}T00:00"
        next_day = datetime.fromisoformat(args.date) + timedelta(days=1)
        end_local = next_day.strftime("%Y-%m-%dT00:00")
    elif args.start_local and args.end_local:
        start_local, end_local = args.start_local, args.end_local
    else:
        parser.error("provide --date or both --start-local and --end-local")

    start_utc = parse_local(start_local, zone)
    end_utc = parse_local(end_local, zone)
    if end_utc <= start_utc or end_utc - start_utc > timedelta(days=1, hours=1):
        parser.error("window must be positive and no longer than one local day")
    sql = build_query(latitude, longitude, args.radius_km, start_utc, end_utc)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(sql, encoding="utf-8")
        print(args.output.resolve())
    else:
        print(sql, end="")


if __name__ == "__main__":
    main()
