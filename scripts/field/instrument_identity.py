"""Recognize Des Moines instrument payloads before they enter S3 Bronze.

The local folder name and configured instrument ID are not evidence of what an
instrument actually wrote.  This module uses stable wire/file-format
signatures so the uploader can fail closed when a batch belongs to another
instrument or contains a mixture of instruments.
"""

import csv
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta


INSTRUMENT_IDS = (
    "BC-MA200",
    "CO2-LICOR",
    "NEPH-PM25",
    "NO2-CAPS",
    "SMPS",
)

PC_ENVELOPE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\t"
)
DATE_TIME_RE = re.compile(
    r"\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$"
)
SMPS_DATE_TIME_RE = re.compile(
    r"^\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2}$"
)
TIME_RE = re.compile(r"^\d{1,2}:\d{1,2}:\d{1,2}$")
DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")


def _fields(line):
    try:
        return [part.strip() for part in next(csv.reader([line]))]
    except (csv.Error, StopIteration):
        return []


def _is_float(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def unwrap_acquisition_envelope(line):
    """Remove the logger's PC receipt timestamp without changing raw payload."""
    stripped = line.strip().lstrip("\ufeff")
    if PC_ENVELOPE_RE.match(stripped):
        return stripped.split("\t", 1)[1].strip()
    return stripped


def _is_complete_licor_xml(payload):
    start = payload.find("<li850>")
    end = payload.rfind("</li850>")
    if start < 0 or end < 0:
        return False
    try:
        root = ET.fromstring(payload[start:end + len("</li850>")])
    except ET.ParseError:
        return False
    data = root.find("data")
    return (
        root.tag == "li850"
        and data is not None
        and data.find("celltemp") is not None
        and data.find("cellpres") is not None
        and data.find("co2") is not None
    )


def classify_line(line):
    """Return an instrument ID for a recognizable data row, otherwise ``None``."""
    payload = unwrap_acquisition_envelope(line).replace("\x00", "")
    if not payload:
        return None

    if _is_complete_licor_xml(payload):
        return "CO2-LICOR"

    fields = _fields(payload)
    if not fields or not any(fields):
        return None
    first = fields[0].lstrip("\ufeff")
    second = fields[1] if len(fields) > 1 else ""

    if len(fields) > 40 and first.isdigit() and SMPS_DATE_TIME_RE.match(second):
        return "SMPS"

    if len(fields) > 10 and first.upper().startswith("MA") and second.isdigit():
        return "BC-MA200"

    # Raw nephelometer rows have eight fields.  Normalized historical rows can
    # have additional clock-diagnostic fields, but retain the same leading
    # timestamp and scattering value.
    if len(fields) >= 8 and DATE_TIME_RE.search(first) and _is_float(second):
        return "NEPH-PM25"

    # Historical LI-COR files are tabular; current serial captures are XML.
    if len(fields) >= 3 and DATE_RE.match(first) and TIME_RE.match(second):
        return "CO2-LICOR"

    # Current CAPS wire data contains nine numeric fields beginning with 1904
    # epoch seconds.  Historical normalized rows begin with HHMMSS.
    if len(fields) == 9 and all(_is_float(value) for value in fields):
        try:
            instrument_time = datetime(1904, 1, 1) + timedelta(seconds=float(first))
        except (OverflowError, ValueError):
            instrument_time = None
        if instrument_time is not None and 2000 <= instrument_time.year <= 2100:
            return "NO2-CAPS"
    if (
        len(fields) >= 10
        and re.match(r"^\d{6}$", first)
        and len(fields) > 3
        and _is_float(fields[3])
    ):
        return "NO2-CAPS"

    return None


def _header_identity(line):
    """Recognize strong instrument-specific headers without treating them as data."""
    normalized = unwrap_acquisition_envelope(line).lower()
    if "scan number" in normalized and "datetime sample start" in normalized:
        return "SMPS"
    if "<li850>" in normalized or "celltemp" in normalized and "cellpres" in normalized:
        return "CO2-LICOR"
    return None


def _is_benign_header(line):
    normalized = line.strip().lstrip("\ufeff").lower()
    return (
        not normalized
        or normalized.startswith(("%", "#"))
        or normalized == "pc_date_time\traw_line"
        or "putty log" in normalized
    )


def inspect_batch(expected_instrument, text):
    """Validate a complete uploader batch against its configured instrument.

    A batch is rejected if any row positively identifies as another instrument,
    even when expected rows are also present.  Unknown metadata is tolerated
    only alongside at least one expected row; this supports SMPS preambles while
    still making arbitrary or damaged content fail closed.
    """
    if expected_instrument not in INSTRUMENT_IDS:
        return {
            "valid": False,
            "reason": f"unsupported configured instrument {expected_instrument!r}",
            "counts": {},
            "unknown_lines": 0,
        }

    lines = [line for line in text.splitlines() if line.strip()]
    counts = {instrument_id: 0 for instrument_id in INSTRUMENT_IDS}
    header_counts = {instrument_id: 0 for instrument_id in INSTRUMENT_IDS}
    unknown_lines = 0

    for line in lines:
        instrument_id = classify_line(line)
        if instrument_id:
            counts[instrument_id] += 1
            continue
        header_id = _header_identity(line)
        if header_id:
            header_counts[header_id] += 1
        elif not _is_benign_header(line):
            unknown_lines += 1

    # LI-COR XML can be split across physical lines.  Check the joined payload
    # once so valid multiline XML is not rejected as unknown text.
    if counts["CO2-LICOR"] == 0:
        joined = "".join(unwrap_acquisition_envelope(line) for line in lines)
        if _is_complete_licor_xml(joined):
            counts["CO2-LICOR"] = 1
            if joined.startswith("<li850>") and joined.endswith("</li850>"):
                unknown_lines = 0

    foreign = {
        instrument_id: count
        for instrument_id, count in counts.items()
        if instrument_id != expected_instrument and count
    }
    foreign_headers = {
        instrument_id: count
        for instrument_id, count in header_counts.items()
        if instrument_id != expected_instrument and count
    }
    if foreign or foreign_headers:
        detected = {**foreign_headers, **foreign}
        summary = ", ".join(
            f"{instrument_id} ({count} matching row(s))"
            for instrument_id, count in sorted(detected.items())
        )
        return {
            "valid": False,
            "reason": f"expected {expected_instrument}, detected {summary}",
            "counts": counts,
            "unknown_lines": unknown_lines,
        }

    expected_count = counts[expected_instrument]
    if expected_count:
        if unknown_lines > max(5, expected_count):
            return {
                "valid": False,
                "reason": (
                    f"matched {expected_count} {expected_instrument} row(s), but "
                    f"found {unknown_lines} unrecognized non-header line(s)"
                ),
                "counts": counts,
                "unknown_lines": unknown_lines,
            }
        return {
            "valid": True,
            "reason": f"matched {expected_count} {expected_instrument} data row(s)",
            "counts": counts,
            "unknown_lines": unknown_lines,
        }

    if header_counts[expected_instrument] or (lines and unknown_lines == 0):
        return {
            "valid": True,
            "reason": "header/metadata only; no conflicting instrument signature",
            "counts": counts,
            "unknown_lines": unknown_lines,
        }

    return {
        "valid": False,
        "reason": (
            f"no recognizable {expected_instrument} data rows "
            f"({unknown_lines} unrecognized non-header line(s))"
        ),
        "counts": counts,
        "unknown_lines": unknown_lines,
    }


def identify_text(text):
    """Return the unique instrument represented by text, or ``None``."""
    matches = []
    for instrument_id in INSTRUMENT_IDS:
        result = inspect_batch(instrument_id, text)
        if result["valid"] and result["counts"].get(instrument_id):
            matches.append((instrument_id, result["counts"][instrument_id]))
    if not matches:
        return None
    matches.sort(key=lambda item: item[1], reverse=True)
    if len(matches) > 1 and matches[0][1] == matches[1][1]:
        return None
    return matches[0][0]
