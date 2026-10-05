#!/usr/bin/env python3
"""Preserve a raw aircraft export and create a dashboard-compatible JSON copy."""

import argparse
import csv
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def read_observations(path):
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        observations = payload if isinstance(payload, list) else payload.get("observations")
        existing_metadata = {} if isinstance(payload, list) else payload.get("metadata", {})
    else:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            observations = list(csv.DictReader(handle))
        existing_metadata = {}
    if not isinstance(observations, list) or not observations:
        raise ValueError("input must contain at least one aircraft observation")
    return observations, existing_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--provider", required=True, help="For example opensky-trino")
    parser.add_argument("--coverage", required=True, choices=("complete", "partial", "unknown"))
    parser.add_argument("--window-start-utc", required=True, help="ISO 8601 timestamp with Z/offset")
    parser.add_argument("--window-end-utc", required=True, help="ISO 8601 timestamp with Z/offset")
    parser.add_argument("--cache-dir", type=Path, default=REPO / "aircraft-cache")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")

    observations, existing_metadata = read_observations(args.input)
    digest = hashlib.sha256(args.input.read_bytes()).hexdigest()
    imported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    bundle = args.cache_dir / f"{imported_at[:10]}_{digest[:12]}"
    raw_dir = bundle / "raw"
    raw_dir.mkdir(parents=True, exist_ok=False)
    raw_path = raw_dir / args.input.name
    shutil.copy2(args.input, raw_path)

    metadata = {
        **existing_metadata,
        "sample": False,
        "provider": args.provider,
        "coverage_status": args.coverage,
        "window_start_utc": args.window_start_utc,
        "window_end_utc": args.window_end_utc,
        "imported_at_utc": imported_at,
        "raw_sha256": digest,
        "raw_filename": args.input.name,
        "observation_count": len(observations),
    }
    normalized_path = bundle / "aircraft-import.json"
    normalized_path.write_text(
        json.dumps({"metadata": metadata, "observations": observations}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Raw preserved: {raw_path.resolve()}")
    print(f"Dashboard import: {normalized_path.resolve()}")


if __name__ == "__main__":
    main()
