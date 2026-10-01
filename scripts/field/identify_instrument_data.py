"""Identify local instrument files without uploading or changing configuration."""

import argparse
from pathlib import Path

try:
    from scripts.field.instrument_identity import INSTRUMENT_IDS, identify_text, inspect_batch
except ModuleNotFoundError:  # Direct execution from scripts/field on Windows.
    from instrument_identity import INSTRUMENT_IDS, identify_text, inspect_batch


def candidate_files(paths):
    supported_suffixes = {".csv", ".dat", ".log", ".txt", ".xml"}
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            yield from sorted(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in supported_suffixes
            )
        else:
            yield path


def describe(path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    instrument_id = identify_text(text)
    if instrument_id:
        result = inspect_batch(instrument_id, text)
        return instrument_id, result["reason"]

    detected = {}
    for expected in INSTRUMENT_IDS:
        result = inspect_batch(expected, text)
        for found, count in result["counts"].items():
            if count:
                detected[found] = max(detected.get(found, 0), count)
    if len(detected) > 1:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(detected.items()))
        return "MIXED", summary
    return "UNKNOWN", "no unique supported instrument signature"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="one or more files or folders to inspect")
    args = parser.parse_args()

    status = 0
    for path in candidate_files(args.paths):
        try:
            identity, detail = describe(path)
        except OSError as exc:
            identity, detail = "ERROR", str(exc)
        print(f"{path}: {identity} ({detail})")
        if identity in {"UNKNOWN", "MIXED", "ERROR"}:
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
