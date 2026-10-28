"""Silver builder Lambda.

Consolidates each instrument's many raw bronze batch files into a single clean
file in the silver layer. For each instrument it:

* merges every bronze object except denied test/dev sources (chronological order);
* keeps only real data rows (the same `is_data_row` logic the dashboard uses, so
  headers, comment lines, and the instrument metadata preamble are filtered out);
* drops duplicate rows (the at-least-once duplicates and any re-uploads);
* writes two objects:
    {instrument}/silver/{instrument}_data.csv      column header + unique rows
    {instrument}/silver/{instrument}_metadata.txt  preamble/header + provenance

It skips unchanged input manifests. For changed inputs it reuses version-checked
raw batches from private daily bundles and runs the original full consolidation.
It also publishes counts and the default dashboard views.
Invoke on demand from the Lambda console or the CLI:

    aws lambda invoke --function-name aq-silver-builder /dev/stdout
"""

import csv
import gzip
import hashlib
from pathlib import Path
import io
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3

s3_client = boto3.client("s3")

BUCKET = os.environ.get("S3_BUCKET", "des-moines-data-pipeline-austinlab")
INSTRUMENT_IDS = ["BC-MA200", "CO2-LICOR", "NEPH-PM25", "NO2-CAPS", "SMPS"]
MAX_WORKERS = 16

# Duwamish nephelometer conversion copied from the field team's R import code.
# The instrument's scattering coefficient is reported in 10^-6 m^-1. The R
# workflow converts it to 10^-4 m^-1 with ``/ 10 / 10`` before applying the
# Seattle-Duwamish regression.
NEPH_BSCAT_DIVISOR = 100.0
NEPH_PM25_SLOPE = 28.6
NEPH_PM25_INTERCEPT = 2.6
NEPH_RAW_BSCAT_COLUMN = "Raw Scat coefficient (10^-6 m^-1)"
NEPH_BSCAT_COLUMN = "BScat (10^-4 m^-1)"
NEPH_PM25_COLUMN = "PM2.5 (µg/m³)"
SMPS_TOTAL_CONCENTRATION_COLUMN = "Total Concentration (#/cm³)"

EPOCH_1904 = datetime(1904, 1, 1)
NO2_CANONICAL_COLUMNS = [
    "HHMMSS", "Concentration", "Loss", "Pressure", "Temperature", "Signal",
    "Span", "Status", "LastBaseline", "Timestamp", "Instrument_Timestamp",
    "PC_minus_instrument_s",
]
NEPH_CANONICAL_COLUMNS = [
    "Date_Time", "Scat coefficient", "Sample temperature", "Enclosure temperature",
    "Relative humidity", "Atmospheric pressure", "Major State", "Minor State",
    "Instrument_Date_Time", "PC_minus_instrument_s",
]
CO2_CANONICAL_COLUMNS = [
    "System_Date_(Y-M-D)", "System_Time_(h:m:s)", "CO2_(umol_mol-1)",
    "H2O_(mmol_mol-1)", "H2O_(C)", "Cell_Temp_(C)", "Cell_Pressure_(kPa)",
    "CO2_Absorption", "H2O_Absorption", "Input_Voltage_(V)", "Raw_CO2",
    "Raw_CO2_Reference", "Raw_H2O", "Raw_H2O_Reference", "Flow_Rate_(L_min-1)",
    "Timestamp_Source",
]
SERIAL_INSTRUMENT_IDS = {"NO2-CAPS", "NEPH-PM25", "CO2-LICOR"}
SERIAL_ENVELOPE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\t(.*)$"
)
PUTTY_MARKER_RE = re.compile(r"PuTTY log (\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})")
NEPH_RAW_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),"
    r"\s*(-?[\d.]+),\s*(-?[\d.]+),\s*(-?[\d.]+),\s*(-?[\d.]+),"
    r"\s*(-?[\d.]+),(\d+),(\d+)\s*$"
)
LICOR_XML_FIELDS = [
    "celltemp", "cellpres", "co2", "co2abs", "h2o", "h2oabs",
    "h2odewpoint", "ivolt", "raw/co2", "raw/co2ref", "raw/h2o",
    "raw/h2oref", "flowrate",
]

# The field nephelometer's PuTTY capture does not include a CSV header. Its
# current eight-field stream is the same instrument output used by the older
# seven-field export, with the scattering value first and two state fields at
# the end. Supply the schema explicitly so a PuTTY log marker is never mistaken
# for the header.
NEPH_HEADERS_BY_FIELD_COUNT = {
    7: (
        "Date_Time,Major State,Scat coefficient,Sample temperature,"
        "Enclosure temperature,Relative humidity,Atmospheric pressure"
    ),
    8: (
        "Date_Time,Scat coefficient,Sample temperature,Enclosure temperature,"
        "Relative humidity,Atmospheric pressure,Major State,Minor State"
    ),
}

# Bronze currently holds a batch of June 2026 SMPS instrument test/dev runs
# alongside the real Oct Duwamish export. Exclude those specific sources by name
# so Silver stays clean. This is a denylist, not an allowlist, so automated
# ingestion of new real data flows through with no per-file maintenance. Delete
# these entries once the test objects are cleared out of Bronze.
EXCLUDED_BRONZE_SOURCES = {
    "SMPS": {
        "2026-06-17_163506_SMPS",
        "2026-06-22_100022_SMPS",
        "2026-06-22_164514_SMPS",
        "2026-06-23_102848_SMPS",
        "SMPS_3082002215001_20260625_144825",
        "SMPS_3082002215001_20260625_173616",
        "SMPS_3082002215001_20260626_173616",
    },
}


def bronze_source_name(key):
    filename = key.rsplit("/", 1)[-1]
    return filename.split("__batch_", 1)[0]


def is_excluded_bronze_key(instrument_id, key):
    return bronze_source_name(key) in EXCLUDED_BRONZE_SOURCES.get(instrument_id, set())


def list_bronze_objects(instrument_id):
    objects = []
    for page in s3_client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"{instrument_id}/bronze/"):
        objects.extend(obj for obj in page.get("Contents", []) if not obj["Key"].endswith((".keep", "/")))
    return sorted(objects, key=lambda obj: obj["Key"])


def list_bronze_keys(instrument_id):
    return [obj["Key"] for obj in list_bronze_objects(instrument_id)]


def download_text(key, etag=None):
    condition = {"IfMatch": etag} if etag else {}
    body = s3_client.get_object(Bucket=BUCKET, Key=key, **condition)["Body"].read()
    return key, body.decode("utf-8", errors="replace")


def object_version(obj):
    return [obj["Key"], obj.get("ETag"), obj["Size"], obj["LastModified"].isoformat()]


def load_cached_bronze(instrument_id, objects):
    """Reuse exact source text in private daily bundles, never derived values.

    A listed key/ETag/size/time and a content digest must all match. New,
    changed or corrupt entries read Bronze conditionally. Deleted entries are
    omitted. Existing consolidate/transform functions still process every row.
    """
    groups = {}
    for obj in objects:
        groups.setdefault(obj["LastModified"].strftime("%Y-%m-%d"), []).append(obj)
    def load_group(item):
        day, source_objects = item
        key = f"_processing_cache/v1/{instrument_id}/bronze-{day}.json.gz"
        cached = {}
        try:
            saved = json.loads(gzip.decompress(s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read()))
            if (isinstance(saved, dict) and saved.get("schema_version") == 1
                    and saved.get("instrument_id") == instrument_id and isinstance(saved.get("objects"), dict)):
                cached = saved.get("objects", {})
        except Exception:
            pass
        entries, texts, reads = {}, [], 0
        for obj in source_objects:
            old = cached.get(obj["Key"], {})
            if not isinstance(old, dict):
                old = {}
            text = old.get("text")
            if (old.get("version") != object_version(obj) or not isinstance(text, str)
                    or old.get("sha256") != hashlib.sha256(text.encode()).hexdigest()):
                _, text = download_text(obj["Key"], obj.get("ETag"))
                reads += 1
            entries[obj["Key"]] = {"version": object_version(obj), "text": text,
                "sha256": hashlib.sha256(text.encode()).hexdigest()}
            texts.append((obj["Key"], text))
        if entries != cached:
            try:
                s3_client.put_object(Bucket=BUCKET, Key=key,
                    Body=gzip.compress(json.dumps({"schema_version": 1, "instrument_id": instrument_id, "objects": entries}).encode(), mtime=0),
                    ContentType="application/gzip")
            except Exception as exc:
                print(f"Optional Bronze bundle write failed for {instrument_id}: {exc}")
        return texts, reads
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        loaded = list(pool.map(load_group, groups.items()))
    return sorted((text for texts, _ in loaded for text in texts), key=lambda item: item[0]), sum(reads for _, reads in loaded)


# --- Row detection: mirrors lambda_api.py ---------------------------------

def clean_field(field):
    return field.strip().lstrip("\ufeff").strip('"').strip()


def split_fields(line):
    if "\t" in line:
        return [clean_field(field) for field in line.split("\t")]
    try:
        return [clean_field(field) for field in next(csv.reader([line]))]
    except csv.Error:
        return []


def is_float(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def csv_line(fields):
    """Serialize one Silver row without adding a trailing newline."""
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="").writerow(fields)
    return output.getvalue()


def compact_number(value):
    """Return a stable, readable representation for a derived numeric value."""
    return format(value, ".12g")


def parse_local_timestamp(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def format_local_timestamp(value):
    return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def seconds_between(received_at, instrument_time):
    return compact_number(
        (received_at.replace(tzinfo=None) - instrument_time.replace(tzinfo=None)).total_seconds()
    )


def unwrap_serial_envelope(line):
    """Return (PC time, exact raw payload) for a field-logger Bronze row."""
    match = SERIAL_ENVELOPE_RE.match(line)
    if not match:
        return None, line
    return parse_local_timestamp(match.group(1)), match.group(2)


def parse_raw_no2(payload, received_at=None):
    parts = [part.strip() for part in payload.split(",")]
    if len(parts) != 9:
        return None
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    # The CAPS time field has been seen as 1904-epoch seconds and, after an
    # instrument setup change, as a bare HHMMSS reading. Decode the epoch form so
    # historical rows are unchanged; otherwise keep the raw token and use the PC
    # receipt time as the reporting stamp, leaving the instrument clock blank for
    # the field team to confirm.
    try:
        epoch_time = EPOCH_1904 + timedelta(seconds=values[0])
    except (ValueError, OverflowError):
        epoch_time = None
    if epoch_time is not None and 2000 <= epoch_time.year <= 2100:
        reporting_time = received_at or epoch_time
        fields = [epoch_time.strftime("%H%M%S")] + [
            compact_number(value) for value in values[1:]
        ] + [
            format_local_timestamp(reporting_time),
            format_local_timestamp(epoch_time),
            seconds_between(reporting_time, epoch_time),
        ]
        return csv_line(NO2_CANONICAL_COLUMNS), csv_line(fields)
    if received_at is None:
        return None
    fields = [parts[0]] + [
        compact_number(value) for value in values[1:]
    ] + [
        format_local_timestamp(received_at),
        "",
        "",
    ]
    return csv_line(NO2_CANONICAL_COLUMNS), csv_line(fields)


def parse_raw_neph(payload, received_at=None):
    match = NEPH_RAW_RE.search(payload.replace("\x00", ""))
    if not match:
        return None
    try:
        instrument_time = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    reporting_time = received_at or instrument_time
    fields = [format_local_timestamp(reporting_time)] + list(match.groups()[1:]) + [
        format_local_timestamp(instrument_time),
        seconds_between(reporting_time, instrument_time),
    ]
    return csv_line(NEPH_CANONICAL_COLUMNS), csv_line(fields)


def parse_raw_licor(payload, received_at=None, timestamp_source=None):
    start = payload.find("<li850>")
    end = payload.rfind("</li850>")
    if start < 0 or end < 0 or received_at is None:
        return None
    try:
        root = ET.fromstring(payload[start:end + len("</li850>")])
    except ET.ParseError:
        return None
    data = root.find("data")
    if data is None:
        return None
    values = [data.findtext(name) for name in LICOR_XML_FIELDS]
    if any(value is None for value in values):
        return None
    by_name = dict(zip(LICOR_XML_FIELDS, (value.strip() for value in values)))
    fields = [
        received_at.strftime("%Y-%m-%d"), received_at.strftime("%H:%M:%S"),
        by_name["co2"], by_name["h2o"], by_name["h2odewpoint"],
        by_name["celltemp"], by_name["cellpres"], by_name["co2abs"],
        by_name["h2oabs"], by_name["ivolt"], by_name["raw/co2"],
        by_name["raw/co2ref"], by_name["raw/h2o"], by_name["raw/h2oref"],
        by_name["flowrate"], timestamp_source or "pc_received_at",
    ]
    return csv_line(CO2_CANONICAL_COLUMNS), csv_line(fields)


def normalize_serial_record(
    instrument_id,
    line,
    source_header=None,
    fallback_received_at=None,
    fallback_timestamp_source=None,
):
    """Normalize one serial-instrument Bronze row into a stable Silver schema."""
    received_at, payload = unwrap_serial_envelope(line)
    if received_at is None:
        received_at = fallback_received_at
    if instrument_id == "NO2-CAPS":
        raw = parse_raw_no2(payload, received_at)
        if raw:
            return raw
        fields = split_fields(payload)
        if len(fields) >= 10 and re.match(r"^\d{6}$", fields[0]) and is_float(fields[3]):
            if len(fields) >= len(NO2_CANONICAL_COLUMNS):
                return csv_line(NO2_CANONICAL_COLUMNS), csv_line(fields[:len(NO2_CANONICAL_COLUMNS)])
            instrument_stamp = fields[9]
            return csv_line(NO2_CANONICAL_COLUMNS), csv_line(
                fields[:10] + [instrument_stamp, ""]
            )
        return None
    if instrument_id == "NEPH-PM25":
        raw = parse_raw_neph(payload, received_at)
        if raw:
            return raw
        return normalize_tabular_neph(payload, source_header)
    if instrument_id == "CO2-LICOR":
        raw = parse_raw_licor(
            payload,
            received_at,
            "pc_received_at" if SERIAL_ENVELOPE_RE.match(line) else fallback_timestamp_source,
        )
        if raw:
            return raw
        return normalize_tabular_licor(payload, source_header)
    return None


def normalized_column_name(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def normalize_tabular_licor(payload, source_header):
    """Map existing LI-COR tab exports into the same canonical Silver schema."""
    fields = split_fields(payload)
    columns = split_fields(source_header) if source_header else []
    if len(fields) < 3 or len(columns) != len(fields):
        return None
    if not (
        re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", fields[0])
        and re.match(r"^\d{1,2}:\d{1,2}:\d{1,2}$", fields[1])
    ):
        return None
    normalized = [normalized_column_name(column) for column in columns]

    def find_value(*terms, exclude=()):
        for index, name in enumerate(normalized):
            if all(term in name for term in terms) and not any(term in name for term in exclude):
                return fields[index]
        return ""

    canonical = [
        fields[0], fields[1],
        find_value("co", "mol", exclude=("absorption", "raw")),
        find_value("h", "o", "mmol", exclude=("absorption", "raw")),
        find_value("h", "o", "c", exclude=("cell", "absorption", "raw")),
        find_value("cell", "temp"), find_value("cell", "pressure"),
        find_value("co", "absorption"), find_value("h", "o", "absorption"),
        find_value("input", "voltage"), find_value("raw", "co", exclude=("reference",)),
        find_value("raw", "co", "reference"), find_value("raw", "h", "o", exclude=("reference",)),
        find_value("raw", "h", "o", "reference"), find_value("flow", "rate"),
        "instrument_export",
    ]
    return csv_line(CO2_CANONICAL_COLUMNS), csv_line(canonical)


def normalize_tabular_neph(payload, source_header):
    """Map old seven/eight-field nephelometer exports to one Silver schema."""
    fields = split_fields(payload)
    columns = split_fields(source_header) if source_header else []
    if len(fields) < 7 or len(columns) != len(fields):
        return None
    normalized = [normalized_column_name(column) for column in columns]

    def value(*terms):
        for index, name in enumerate(normalized):
            if all(term in name for term in terms):
                return fields[index]
        return ""

    timestamp = value("date", "time")
    scat = value("scat")
    if not timestamp or not is_float(scat):
        return None
    instrument_timestamp = value("instrument", "date", "time") or timestamp
    canonical = [
        timestamp, scat, value("sample", "temp"), value("enclosure", "temp"),
        value("relative", "humidity"), value("atmospheric", "pressure"),
        value("major", "state"), value("minor", "state"), instrument_timestamp,
        value("pc", "minus", "instrument"),
    ]
    return csv_line(NEPH_CANONICAL_COLUMNS), csv_line(canonical)


def transform_silver(instrument_id, header, data_rows):
    """Apply instrument-specific cleaning and derived measurements.

    Bronze remains immutable. Transformations happen only in the consolidated
    Silver output, where their column names make the units and provenance clear.
    """
    if not header:
        return header, data_rows

    columns = split_fields(header)
    if instrument_id == "SMPS":
        columns = [
            SMPS_TOTAL_CONCENTRATION_COLUMN
            if column.lower().startswith("total concentration")
            else column
            for column in columns
        ]
        return csv_line(columns), data_rows

    if instrument_id != "NEPH-PM25":
        return header, data_rows

    try:
        scat_index = next(
            index
            for index, column in enumerate(columns)
            if column.strip().lower() in {"scat coefficient", "bscat"}
        )
    except StopIteration:
        return header, data_rows

    columns[scat_index] = NEPH_RAW_BSCAT_COLUMN
    transformed_rows = []
    for raw_line in data_rows:
        fields = split_fields(raw_line)
        if len(fields) != len(columns) or not is_float(fields[scat_index]):
            # Consolidation already guards the schema. Keep a defensive fallback
            # so one malformed value cannot prevent the full Silver rebuild.
            fields.extend(["", ""])
        else:
            bscat = float(fields[scat_index]) / NEPH_BSCAT_DIVISOR
            pm25 = (bscat * NEPH_PM25_SLOPE) + NEPH_PM25_INTERCEPT
            fields.extend([compact_number(bscat), compact_number(pm25)])
        transformed_rows.append(csv_line(fields))

    columns.extend([NEPH_BSCAT_COLUMN, NEPH_PM25_COLUMN])
    return csv_line(columns), transformed_rows


def is_data_row(instrument_id, line):
    stripped = line.strip().lstrip("\ufeff")
    if not stripped or stripped.startswith(('%', '#')):
        return False

    fields = split_fields(stripped)
    if not fields or not any(fields):
        return False

    first = fields[0]
    second = fields[1] if len(fields) > 1 else ""

    if instrument_id == "BC-MA200":
        return len(fields) > 10 and first.upper().startswith("MA") and second.isdigit()
    if instrument_id == "CO2-LICOR":
        # The Li-Cor does not zero-pad hours/minutes (e.g. 18:0:00), so accept
        # 1- or 2-digit time components.
        return (
            len(fields) >= 3
            and re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", first)
            and re.match(r"^\d{1,2}:\d{1,2}:\d{1,2}$", second)
        )
    if instrument_id == "NEPH-PM25":
        return (
            len(fields) >= 3
            and re.match(
                r"^\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$",
                first,
            )
            and is_float(second)
        )
    if instrument_id == "NO2-CAPS":
        return len(fields) >= 10 and re.match(r"^\d{6}$", first) and is_float(fields[3])
    if instrument_id == "SMPS":
        return (
            len(fields) > 40
            and first.isdigit()
            and re.match(r"^\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2}$", second)
        )
    return False


def consolidate_smps(ordered_texts):
    """Union changing SMPS schemas instead of dropping valid rollover rows.

    SMPS exports in Bronze contain different particle-bin counts (currently 146
    and 234 fields). Incremental batches after the first also omit the header.
    Keep a header per source file, form a stable union by column name, and fill
    absent columns with blanks so all valid scans survive in one Silver CSV.
    """
    source_headers = {}
    records = []
    metadata = []
    seen_meta = set()
    schema_mismatches = 0

    for key, text in ordered_texts:
        source = bronze_source_name(key)
        current_header = source_headers.get(source)
        last_nondata = None
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            if is_data_row("SMPS", line):
                fields = split_fields(line)
                candidate = split_fields(last_nondata) if last_nondata else []
                if len(candidate) == len(fields):
                    current_header = candidate
                    source_headers[source] = candidate
                elif current_header is None:
                    current_header = source_headers.get(source)
                if current_header is None or len(current_header) != len(fields):
                    schema_mismatches += 1
                    continue
                records.append((current_header, fields))
            elif line.strip():
                last_nondata = line
                if line not in seen_meta:
                    seen_meta.add(line)
                    metadata.append(line)

    union_columns = []
    union_index = {}
    for source_header, _fields in records:
        for column in source_header:
            if column not in union_index:
                union_index[column] = len(union_columns)
                union_columns.append(column)

    data_rows = []
    seen_rows = set()
    duplicates = 0
    for source_header, fields in records:
        unified = [""] * len(union_columns)
        for column, value in zip(source_header, fields):
            unified[union_index[column]] = value
        line = csv_line(unified)
        if line in seen_rows:
            duplicates += 1
        else:
            seen_rows.add(line)
            data_rows.append(line)

    header = csv_line(union_columns) if union_columns else None
    return (
        header,
        data_rows,
        metadata,
        len(records),
        duplicates,
        schema_mismatches,
    )


def consolidate(instrument_id, ordered_texts):
    """Return (header, data_rows, metadata_lines, total_rows, duplicates, schema_mismatches)."""
    if instrument_id == "SMPS":
        return consolidate_smps(ordered_texts)

    header = None
    expected_fields = None
    data_rows = []
    seen_rows = set()
    metadata = []
    seen_meta = set()
    total = 0
    duplicates = 0
    schema_mismatches = 0

    def accept_data(row_header, row_line):
        nonlocal header, expected_fields, total, duplicates, schema_mismatches
        fields = split_fields(row_line)
        if header is None and row_header:
            header = row_header
            expected_fields = len(split_fields(header))
        if expected_fields is None:
            expected_fields = len(fields)
        if len(fields) != expected_fields:
            schema_mismatches += 1
            return
        total += 1
        if row_line in seen_rows:
            duplicates += 1
        else:
            seen_rows.add(row_line)
            data_rows.append(row_line)

    for _key, text in ordered_texts:
        last_nondata = None
        putty_started_at = None
        putty_xml_index = 0
        neph_clock_adjustment = None
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            marker = PUTTY_MARKER_RE.search(line)
            if marker:
                putty_started_at = datetime.strptime(
                    marker.group(1), "%Y.%m.%d %H:%M:%S"
                )
                putty_xml_index = 0
                neph_clock_adjustment = None

            if instrument_id in SERIAL_INSTRUMENT_IDS:
                fallback_received_at = None
                fallback_source = None
                if instrument_id == "CO2-LICOR" and "<li850>" in line and putty_started_at:
                    # Legacy PuTTY XML has no row timestamp. A one-second cadence
                    # is the instrument's observed stream rate. Keep the source
                    # label in Silver so this inference is never mistaken for a
                    # directly observed timestamp.
                    fallback_received_at = putty_started_at + timedelta(seconds=putty_xml_index)
                    fallback_source = "inferred_from_putty_start_1s"
                    putty_xml_index += 1
                elif instrument_id == "NEPH-PM25" and putty_started_at:
                    _envelope_time, neph_payload = unwrap_serial_envelope(line)
                    neph_match = NEPH_RAW_RE.search(neph_payload.replace("\x00", ""))
                    if _envelope_time is None and neph_match:
                        instrument_time = datetime.strptime(
                            neph_match.group(1), "%Y-%m-%d %H:%M:%S"
                        )
                        if neph_clock_adjustment is None:
                            observed_delta = (putty_started_at - instrument_time).total_seconds()
                            # PuTTY markers are session-level, not exact row
                            # timestamps. Only repair unmistakable whole-hour
                            # clock errors; never add the normal few-second gap.
                            if abs(observed_delta) >= 6 * 3600:
                                neph_clock_adjustment = timedelta(
                                    hours=round(observed_delta / 3600)
                                )
                            else:
                                neph_clock_adjustment = timedelta(0)
                        fallback_received_at = instrument_time + neph_clock_adjustment
                normalized = normalize_serial_record(
                    instrument_id,
                    line,
                    source_header=last_nondata,
                    fallback_received_at=fallback_received_at,
                    fallback_timestamp_source=fallback_source,
                )
                if normalized:
                    row_header, row_line = normalized
                    accept_data(row_header, row_line)
                    continue
                if line.strip():
                    last_nondata = line
                    if line not in seen_meta:
                        seen_meta.add(line)
                        metadata.append(line)
                continue

            if is_data_row(instrument_id, line):
                fields = split_fields(line)
                if header is None:
                    candidate_fields = split_fields(last_nondata) if last_nondata else []
                    if len(candidate_fields) == len(fields):
                        header = last_nondata
                    elif instrument_id == "NEPH-PM25":
                        header = NEPH_HEADERS_BY_FIELD_COUNT.get(len(fields))
                    if header:
                        expected_fields = len(split_fields(header))
                accept_data(header, line)
            elif line.strip():
                last_nondata = line
                if line not in seen_meta:
                    seen_meta.add(line)
                    metadata.append(line)

    return header, data_rows, metadata, total, duplicates, schema_mismatches


def write_silver(instrument_id, header, data_rows, metadata, stats):
    data_body = ""
    if header:
        data_body += header + "\n"
    data_body += "\n".join(data_rows) + ("\n" if data_rows else "")

    meta_lines = [
        f"# Silver metadata for {instrument_id}",
        f"# generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"# bronze_objects: {stats['objects']}",
        f"# data_rows_total: {stats['total']}",
        f"# data_rows_unique: {stats['unique']}",
        f"# duplicates_removed: {stats['duplicates']}",
        f"# expected_fields: {stats.get('expected_fields') or ''}",
        f"# schema_mismatches_skipped: {stats.get('schema_mismatches', 0)}",
        f"# excluded_objects: {stats.get('excluded_objects', 0)}",
        f"# excluded_sources: {', '.join(stats.get('excluded_sources') or []) or 'none'}",
        "# --- header / preamble lines from bronze ---",
    ] + metadata
    meta_body = "\n".join(meta_lines) + "\n"

    uploaded = s3_client.put_object(
        Bucket=BUCKET,
        Key=f"{instrument_id}/silver/{instrument_id}_data.csv",
        Body=data_body.encode("utf-8"),
        ContentType="text/csv; charset=utf-8",
    )
    s3_client.put_object(
        Bucket=BUCKET,
        Key=f"{instrument_id}/silver/{instrument_id}_metadata.txt",
        Body=meta_body.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    return data_body, uploaded["ETag"]


def build_instrument(instrument_id):
    # Package the API reader with this Lambda so there is exactly one parser
    # and aggregation implementation for both published and requested views.
    import lambda_api as api
    objects = list_bronze_objects(instrument_id)
    state_key = f"_pipeline_state/v1/{instrument_id}.json"
    fingerprint = hashlib.sha256(json.dumps([object_version(obj) for obj in objects]).encode()).hexdigest()
    code_version = hashlib.sha256(Path(__file__).read_bytes() + api.SILVER_VIEW_CODE_VERSION.encode()).hexdigest()
    previous = {}
    try:
        previous = json.loads(s3_client.get_object(Bucket=BUCKET, Key=state_key)["Body"].read())
    except Exception:
        pass
    if (previous.get("source_fingerprint") == fingerprint and previous.get("builder_version") == code_version):
        if not objects:
            return {**previous["result"], "skipped_unchanged": True, "bronze_reads": 0}
        try:
            current = s3_client.head_object(Bucket=BUCKET, Key=f"{instrument_id}/silver/{instrument_id}_data.csv")
            if current["ETag"] == previous.get("silver_etag") and previous.get("views_version") == api.SILVER_VIEW_CODE_VERSION:
                return {**previous["result"], "skipped_unchanged": True, "bronze_reads": 0}
        except Exception:
            pass
    all_keys = [obj["Key"] for obj in objects]
    keys = [key for key in all_keys if not is_excluded_bronze_key(instrument_id, key)]
    included = set(keys)
    excluded_sources = sorted({bronze_source_name(key) for key in all_keys if key not in included})
    texts, bronze_reads = load_cached_bronze(instrument_id, objects) if objects else ([], 0)
    # Same detector as the legacy summary, including excluded test/dev sources.
    raw_rows = sum(1 for _, text in texts for line in text.splitlines() if api.is_data_row(instrument_id, line))
    source_texts = [(key, text) for key, text in texts if key in included]
    result = {"objects": len(keys), "rows": 0, "unique": 0, "duplicates": 0,
        "excluded_objects": len(all_keys) - len(keys), "excluded_sources": excluded_sources, "written": False}
    silver_etag, views_version = None, None
    if keys:
        header, rows, metadata, total, duplicates, mismatches = consolidate(instrument_id, source_texts)
        header, rows = transform_silver(instrument_id, header, rows)
        stats = {"objects": len(keys), "total": total, "unique": len(rows), "duplicates": duplicates,
            "expected_fields": len(split_fields(header)) if header else None, "schema_mismatches": mismatches,
            "excluded_objects": len(all_keys) - len(keys), "excluded_sources": excluded_sources}
        silver_text, silver_etag = write_silver(instrument_id, header, rows, metadata, stats)
        result.update(rows=total, unique=len(rows), duplicates=duplicates, schema_mismatches=mismatches, written=True)
        try:
            api.publish_dashboard_views(instrument_id, silver_text, silver_etag)
            views_version = api.SILVER_VIEW_CODE_VERSION
        except Exception as exc:
            print(f"Dashboard view publication failed for {instrument_id}: {exc}")
    generated_at = datetime.now(timezone.utc).isoformat()
    latest = max((obj["LastModified"] for obj in objects), default=None)
    state = {"schema_version": 1, "source_fingerprint": fingerprint, "builder_version": code_version,
        "silver_etag": silver_etag, "views_version": views_version, "generated_at": generated_at,
        "inventory": {"id": instrument_id, "name": instrument_id.replace("-", " "),
            "bronzeSize": sum(obj["Size"] for obj in objects), "bronzeRows": raw_rows,
            "silverRows": result["unique"], "lastUpdate": latest.isoformat() if latest else None}, "result": result}
    s3_client.put_object(Bucket=BUCKET, Key=state_key, Body=json.dumps(state).encode(), ContentType="application/json")
    return {**result, "bronze_reads": bronze_reads, "skipped_unchanged": False}


def lambda_handler(event, context):
    results = {}
    for instrument_id in INSTRUMENT_IDS:
        try:
            results[instrument_id] = build_instrument(instrument_id)
        except Exception as exc:
            results[instrument_id] = {"error": str(exc)}

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Silver build complete",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "instruments": results,
        }),
    }
