"""Incremental instrument-data uploader for the Des Moines pipeline.

Each instrument writes to one or more local files whose names encode a date
range, e.g. ``2026Feb12-25_CO2-46_Duwamish.txt``. When an instrument rolls over
to a new file (``2026Apr12-28_CO2-46_Duwamish.txt``) the uploader must keep
ingesting without re-uploading old data and without missing the new file.

To make that robust this module:

* discovers source files per instrument with a glob pattern (``data_glob``)
  instead of a single hardcoded path, so any new file matching the convention
  is picked up automatically;
* checkpoints a byte offset **per file** (keyed by basename) instead of one
  offset per instrument, so each file is read incrementally and a brand-new
  file simply starts at offset 0;
* only uploads complete lines (holds back a trailing partial line until the
  instrument finishes writing it) so no row is ever split across batches;
* buffers each batch in SQLite and retries failed uploads on the next run.

Run from the repository root:

    python3 scripts/upload_instrument_data.py
"""

import asyncio
import glob
import json
import logging
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def log(level, instrument_id, msg):
    getattr(logger, level)(f"[{instrument_id}] {msg}")


CONFIG_FILE = os.environ.get("INSTRUMENT_CONFIG", "instruments_config.json")


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


CREDS_FILE = os.environ.get("AWS_CREDS_FILE", "aws_creds.json")
DEFAULT_REGION = "us-west-2"


def load_aws_credentials():
    """Load AWS credentials from the local aws_creds.json fallback file."""
    with open(CREDS_FILE, "r") as f:
        creds = json.load(f)
    return {
        "aws_access_key_id": creds["aws_access_key_id"],
        "aws_secret_access_key": creds["aws_secret_access_key"],
        "region": creds.get("region", DEFAULT_REGION),
    }


def create_s3_client(config):
    """Create an S3 client.

    Prefers the standard boto3 credential chain (environment variables, shared
    AWS config/credentials, or an attached IAM role). Falls back to the local
    aws_creds.json file only when the chain finds nothing, which keeps the
    field-laptop setup working without committing static keys to the project.
    """
    region = config.get("aws_region") or os.environ.get("AWS_REGION") or DEFAULT_REGION

    session = boto3.Session()
    if session.get_credentials() is not None:
        return session.client("s3", region_name=session.region_name or region)

    if os.path.exists(CREDS_FILE):
        creds = load_aws_credentials()
        logger.info("Using AWS credentials from %s", CREDS_FILE)
        return boto3.client(
            "s3",
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            region_name=creds["region"],
        )

    logger.warning(
        "No AWS credentials found via the default chain and %s is missing; "
        "uploads will fail until credentials are configured.",
        CREDS_FILE,
    )
    return session.client("s3", region_name=region)


DB_FILE = os.environ.get("SENSOR_BUFFER_DB", "sensor_buffer.db")


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buffer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id TEXT NOT NULL,
            batch_name TEXT NOT NULL,
            raw_data TEXT,
            s3_key TEXT NOT NULL,
            start_offset INTEGER,
            end_offset INTEGER,
            source_file TEXT,
            uploaded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            uploaded_at TEXT
        )
    """)
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(buffer)").fetchall()
    }
    for column in ("start_offset", "end_offset", "source_file"):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE buffer ADD COLUMN {column} TEXT"
                         if column == "source_file"
                         else f"ALTER TABLE buffer ADD COLUMN {column} INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_buffer_pending ON buffer (instrument_id, uploaded)"
    )
    conn.commit()
    conn.close()


def insert_buffer_row(
    instrument_id,
    batch_name,
    raw_data,
    s3_key,
    start_offset,
    end_offset,
    source_file,
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.execute(
        """INSERT INTO buffer (
               instrument_id, batch_name, raw_data, s3_key,
               start_offset, end_offset, source_file, uploaded, created_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (
            instrument_id,
            batch_name,
            raw_data,
            s3_key,
            start_offset,
            end_offset,
            source_file,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id


def mark_uploaded(row_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE buffer SET uploaded=1, raw_data=NULL, uploaded_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), row_id),
    )
    conn.commit()
    conn.close()


def get_pending_rows(instrument_id):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        """SELECT id, batch_name, raw_data, s3_key, end_offset, source_file
           FROM buffer
           WHERE instrument_id=? AND uploaded=0
           ORDER BY id""",
        (instrument_id,),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Per-file checkpoints
# ---------------------------------------------------------------------------

CHECKPOINTS_DIR = os.environ.get("CHECKPOINTS_DIR", "checkpoints")


def _normalize_checkpoint(raw, known_basenames):
    """Coerce a checkpoint payload into ``{basename: {"offset": int}}``.

    Accepts both the current per-file format (``{"files": {...}}``) and the
    legacy single-offset format (``{"offset": N}``). For a legacy payload the
    offset is mapped onto the only matching file; if several files match it is
    assigned to the most recent one and the rest start fresh (logged).
    """
    if not raw:
        return {}

    files = raw.get("files")
    if isinstance(files, dict):
        out = {}
        for base, info in files.items():
            offset = info.get("offset", 0) if isinstance(info, dict) else info
            out[base] = {"offset": int(offset or 0)}
        return out

    legacy_offset = int(raw.get("offset", 0) or 0)
    if legacy_offset <= 0:
        return {}
    if len(known_basenames) == 1:
        return {known_basenames[0]: {"offset": legacy_offset}}
    if not known_basenames:
        return {}
    target = sorted(known_basenames)[-1]
    logger.warning(
        "Legacy checkpoint offset %s is ambiguous across %d files; assigning it "
        "to %s and starting the others at 0.",
        legacy_offset, len(known_basenames), target,
    )
    return {target: {"offset": legacy_offset}}


def _read_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_s3_checkpoint_raw(s3, bucket, instrument_id):
    s3_key = f"{instrument_id}/checkpoints/checkpoint.json"
    try:
        obj = s3.get_object(Bucket=bucket, Key=s3_key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as exc:
        log("warning", instrument_id, f"Could not read S3 checkpoint: {exc}")
        return None


def load_file_checkpoints(instrument_id, known_basenames, s3=None, bucket=None):
    """Merge local and S3 per-file checkpoints, taking the max offset per file."""
    os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
    local_path = os.path.join(CHECKPOINTS_DIR, f"{instrument_id}.json")
    local = _normalize_checkpoint(_read_json(local_path), known_basenames)

    remote = {}
    if s3 and bucket:
        remote = _normalize_checkpoint(
            load_s3_checkpoint_raw(s3, bucket, instrument_id), known_basenames
        )

    merged = {}
    for base in set(local) | set(remote):
        local_offset = local.get(base, {}).get("offset", 0)
        s3_offset = remote.get(base, {}).get("offset", 0)
        merged[base] = {"offset": max(local_offset, s3_offset)}
        if s3_offset > local_offset:
            log("info", instrument_id,
                f"Recovered checkpoint for {base} from S3: {s3_offset} "
                f"(local was {local_offset})")
    return merged


def save_file_checkpoints(instrument_id, file_offsets, s3=None, bucket=None):
    """Persist per-file checkpoints locally and (best-effort) to S3."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "instrument_id": instrument_id,
        "files": {
            base: {"offset": info["offset"], "updated_at": now}
            for base, info in file_offsets.items()
        },
        "updated_at": now,
    }
    os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
    local_path = os.path.join(CHECKPOINTS_DIR, f"{instrument_id}.json")
    with open(local_path, "w") as f:
        json.dump(payload, f, indent=2)

    if s3 and bucket:
        s3_key = f"{instrument_id}/checkpoints/checkpoint.json"
        try:
            s3.put_object(
                Bucket=bucket,
                Key=s3_key,
                Body=json.dumps(payload, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as exc:
            log("warning", instrument_id, f"Could not write S3 checkpoint: {exc}")


# ---------------------------------------------------------------------------
# File discovery and incremental reads
# ---------------------------------------------------------------------------

def discover_files(instrument):
    """Return the sorted list of source files for an instrument.

    Honours ``data_glob`` (a glob string or list of globs). For backward
    compatibility a single ``data_file`` path is also accepted.
    """
    patterns = instrument.get("data_glob")
    if patterns is None and instrument.get("data_file"):
        patterns = [instrument["data_file"]]
    if isinstance(patterns, str):
        patterns = [patterns]
    if not patterns:
        return []

    matched = set()
    for pattern in patterns:
        matched.update(glob.glob(pattern))
    # Skip directories and anything under a _quarantine folder.
    files = [
        path for path in matched
        if os.path.isfile(path) and "_quarantine" not in path.split(os.sep)
    ]
    return sorted(files)


def read_new_bytes(path, offset):
    """Read new, complete-line data from ``path`` starting at ``offset``.

    Returns ``(data, used_offset, new_offset, held_bytes, file_size)``.

    * ``used_offset`` is the offset actually read from. It is reset to 0 if the
      stored offset is past the current end of file (file rotated/truncated in
      place), so the file is re-read from the start.
    * A trailing partial line (no newline yet) is held back: ``held_bytes`` is
      its length and it is excluded from ``data``/``new_offset``.
    """
    file_size = os.path.getsize(path)
    used_offset = offset
    if used_offset > file_size:
        used_offset = 0

    with open(path, "rb") as f:
        f.seek(used_offset)
        raw = f.read()

    if not raw:
        return "", used_offset, used_offset, 0, file_size

    complete = raw
    held_bytes = 0
    if not raw.endswith(b"\n"):
        last_newline = raw.rfind(b"\n")
        if last_newline < 0:
            # Only a partial first line so far; nothing complete to upload yet.
            return "", used_offset, used_offset, len(raw), file_size
        complete = raw[:last_newline + 1]
        held_bytes = len(raw) - len(complete)

    new_offset = used_offset + len(complete)
    data = complete.decode("utf-8", errors="replace")
    return data, used_offset, new_offset, held_bytes, file_size


def build_s3_key(instrument_id, source_basename, now):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(source_basename)[0])
    timestamp = now.strftime("%Y%m%dT%H%M%S")
    batch_name = f"{stem}__batch_{timestamp}.txt"
    s3_key = (
        f"{instrument_id}/bronze"
        f"/year={now.strftime('%Y')}"
        f"/month={now.strftime('%m')}"
        f"/{batch_name}"
    )
    return s3_key, batch_name


def upload_to_s3(s3, bucket, s3_key, raw_data):
    delays = [2, 4, 8]
    last_exc = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            s3.put_object(Bucket=bucket, Key=s3_key, Body=raw_data.encode("utf-8"))
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < len(delays):
                time.sleep(delay)
    raise last_exc


def handle_instrument(instrument, s3, config, loop, executor):
    """Ingest all matching files for one instrument, tracking per-file offsets."""
    instrument_id = instrument["id"]
    bucket = config["s3_bucket"]

    async def run():
        rows_uploaded = 0
        had_error = False

        try:
            files = await loop.run_in_executor(executor, discover_files, instrument)
            known_basenames = [os.path.basename(p) for p in files]
            file_offsets = await loop.run_in_executor(
                executor, load_file_checkpoints, instrument_id, known_basenames, s3, bucket
            )

            # 1) Retry anything left buffered from a previous run.
            pending = await loop.run_in_executor(executor, get_pending_rows, instrument_id)
            for row_id, batch_name, raw_data, s3_key, end_offset, source_file in pending:
                if raw_data is None:
                    log("warning", instrument_id,
                        f"Skipping row {row_id}; raw_data is empty but uploaded=0")
                    continue
                try:
                    await loop.run_in_executor(
                        executor, upload_to_s3, s3, bucket, s3_key, raw_data)
                    await loop.run_in_executor(executor, mark_uploaded, row_id)
                    if source_file and end_offset is not None:
                        current = file_offsets.get(source_file, {}).get("offset", 0)
                        if end_offset > current:
                            file_offsets[source_file] = {"offset": end_offset}
                    elif end_offset is not None:
                        log("warning", instrument_id,
                            f"Retry succeeded for row {row_id} but no source_file was "
                            "stored; cannot advance its checkpoint.")
                    log("info", instrument_id, f"Retry succeeded: {s3_key}")
                    rows_uploaded += 1
                except Exception as exc:
                    had_error = True
                    log("error", instrument_id, f"Retry failed for {s3_key}: {exc}")

            # 2) Read new data from each discovered file.
            if not files:
                log("warning", instrument_id,
                    "No files matched the configured data_glob; nothing to ingest.")

            for path in files:
                base = os.path.basename(path)
                current_offset = file_offsets.get(base, {}).get("offset", 0)

                data, used_offset, new_offset, held_bytes, file_size = (
                    await loop.run_in_executor(
                        executor, read_new_bytes, path, current_offset)
                )

                if used_offset < current_offset:
                    log("warning", instrument_id,
                        f"{base}: checkpoint {current_offset} is beyond size "
                        f"{file_size}; file looks rotated/truncated, re-reading from 0.")

                if held_bytes:
                    log("info", instrument_id,
                        f"{base}: holding back {held_bytes} trailing bytes until the "
                        "current line is complete.")

                if not data.strip():
                    file_offsets[base] = {"offset": max(current_offset, used_offset)}
                    continue

                now = datetime.now(timezone.utc)
                s3_key, batch_name = build_s3_key(instrument_id, base, now)

                row_id = await loop.run_in_executor(
                    executor, insert_buffer_row,
                    instrument_id, batch_name, data, s3_key,
                    used_offset, new_offset, base,
                )

                try:
                    await loop.run_in_executor(
                        executor, upload_to_s3, s3, bucket, s3_key, data)
                except Exception as exc:
                    had_error = True
                    log("error", instrument_id,
                        f"All retries failed for {s3_key}: {exc}")
                    continue

                await loop.run_in_executor(executor, mark_uploaded, row_id)
                file_offsets[base] = {"offset": new_offset}
                await loop.run_in_executor(
                    executor, save_file_checkpoints, instrument_id, file_offsets, s3, bucket)
                rows_uploaded += 1

                line_count = len([ln for ln in data.splitlines() if ln.strip()])
                log("info", instrument_id,
                    f"Uploaded {line_count} lines from {base} to s3://{bucket}/{s3_key}")

            await loop.run_in_executor(
                executor, save_file_checkpoints, instrument_id, file_offsets, s3, bucket)

            return {
                "status": "error" if had_error else "ok",
                "rows_uploaded": rows_uploaded,
                "files": {base: info["offset"] for base, info in file_offsets.items()},
            }

        except Exception as exc:
            log("error", instrument_id, f"Unhandled error: {exc}")
            return {
                "status": "error",
                "rows_uploaded": rows_uploaded,
                "error": str(exc),
            }

    return run()


def process_instrument(instrument, s3, config, loop, executor):
    ingestion_type = instrument.get("ingestion_type")
    if ingestion_type == "growing_file":
        return handle_instrument(instrument, s3, config, loop, executor)

    async def unsupported():
        log("error", instrument["id"], f"Unknown ingestion_type: {ingestion_type}")
        return {"status": "error", "error": f"Unknown ingestion_type: {ingestion_type}"}

    return unsupported()


def write_pipeline_status(s3, config, instrument_results):
    all_ok = all(r.get("status") == "ok" for r in instrument_results.values())
    status_obj = {
        "last_run": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "ok" if all_ok else "degraded",
        "instruments": instrument_results,
    }
    s3.put_object(
        Bucket=config["s3_bucket"],
        Key=config["pipeline_status_key"],
        Body=json.dumps(status_obj, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(
        f"Pipeline status written to s3://{config['s3_bucket']}/{config['pipeline_status_key']}"
    )


async def main():
    config = load_config()
    init_db()
    s3 = create_s3_client(config)
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor()

    active_instruments = [i for i in config["instruments"] if i.get("active", True)]

    if not active_instruments:
        logger.info("No active instruments. Exiting.")
        return

    coroutines = [
        process_instrument(inst, s3, config, loop, executor)
        for inst in active_instruments
    ]

    results = await asyncio.gather(*coroutines, return_exceptions=True)

    instrument_results = {}
    for inst, result in zip(active_instruments, results):
        instrument_id = inst["id"]
        if isinstance(result, Exception):
            instrument_results[instrument_id] = {"status": "error", "error": str(result)}
            log("error", instrument_id, f"Task raised exception: {result}")
        else:
            instrument_results[instrument_id] = result

    try:
        write_pipeline_status(s3, config, instrument_results)
    except Exception as exc:
        logger.error(f"Failed to write pipeline status: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
