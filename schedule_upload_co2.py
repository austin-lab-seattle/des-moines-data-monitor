import asyncio
import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3


# -------------------------
# Logging
# -------------------------
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


# -------------------------
# Config
# -------------------------
CONFIG_FILE = "instruments_config.json"


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


# -------------------------
# AWS credentials
# -------------------------
CREDS_FILE = "aws_creds.json"


def load_aws_credentials():
    """Load AWS credentials from aws_creds.json."""
    with open(CREDS_FILE, "r") as f:
        creds = json.load(f)
    return {
        "aws_access_key_id": creds["aws_access_key_id"],
        "aws_secret_access_key": creds["aws_secret_access_key"],
        "region": creds.get("region", "us-west-2"),
    }


def create_s3_client():
    creds = load_aws_credentials()
    return boto3.client(
        "s3",
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        region_name=creds["region"],
    )


# -------------------------
# SQLite buffer
# -------------------------
DB_FILE = "sensor_buffer.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buffer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id TEXT NOT NULL,
            batch_name TEXT NOT NULL,
            raw_data TEXT,
            s3_key TEXT NOT NULL,
            uploaded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            uploaded_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def insert_buffer_row(instrument_id, batch_name, raw_data, s3_key):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.execute(
        """INSERT INTO buffer (instrument_id, batch_name, raw_data, s3_key, uploaded, created_at)
           VALUES (?, ?, ?, ?, 0, ?)""",
        (instrument_id, batch_name, raw_data, s3_key, datetime.now(timezone.utc).isoformat()),
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
        "SELECT id, batch_name, raw_data, s3_key FROM buffer WHERE instrument_id=? AND uploaded=0",
        (instrument_id,),
    ).fetchall()
    conn.close()
    return rows


# -------------------------
# Checkpoints
# -------------------------
CHECKPOINTS_DIR = "checkpoints"


def load_checkpoint(instrument_id):
    os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
    path = os.path.join(CHECKPOINTS_DIR, f"{instrument_id}.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f).get("offset", 0)
    return 0


def save_checkpoint(instrument_id, offset):
    os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
    path = os.path.join(CHECKPOINTS_DIR, f"{instrument_id}.json")
    with open(path, "w") as f:
        json.dump(
            {
                "instrument_id": instrument_id,
                "offset": offset,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            f,
            indent=2,
        )


# -------------------------
# S3 upload with retries
# -------------------------
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


# -------------------------
# Ingestion handlers
# -------------------------
def handle_growing_file(instrument, s3, config, loop, executor):
    """Handler for instruments that write to a single continuously-growing file."""
    instrument_id = instrument["id"]
    bucket = config["s3_bucket"]

    async def run():
        rows_uploaded = 0
        current_offset = 0

        try:
            # Step 1: retry any pending SQLite rows (WiFi may have been down)
            pending = await loop.run_in_executor(executor, get_pending_rows, instrument_id)
            for row_id, batch_name, raw_data, s3_key in pending:
                if raw_data is None:
                    log("warning", instrument_id, f"Skipping row {row_id} — raw_data already nulled but uploaded=0")
                    continue
                try:
                    await loop.run_in_executor(executor, upload_to_s3, s3, bucket, s3_key, raw_data)
                    await loop.run_in_executor(executor, mark_uploaded, row_id)
                    log("info", instrument_id, f"Retry succeeded: {s3_key}")
                    rows_uploaded += 1
                except Exception as exc:
                    log("error", instrument_id, f"Retry failed for {s3_key}: {exc}")

            # Step 2: read new data from file using byte offset
            current_offset = await loop.run_in_executor(executor, load_checkpoint, instrument_id)

            def read_file():
                with open(instrument["data_file"], "r", encoding="utf-8", errors="replace") as f:
                    f.seek(current_offset)
                    data = f.read()
                    new_offset = f.tell()
                return data, new_offset

            data, new_offset = await loop.run_in_executor(executor, read_file)

            # Step 3: no new data
            if not data.strip():
                log("info", instrument_id, "No new data.")
                return {"status": "ok", "rows_uploaded": rows_uploaded, "offset": current_offset}

            # Step 4: insert into SQLite with uploaded=0
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            batch_name = f"batch_{timestamp}.txt"
            now = datetime.now(timezone.utc)
            s3_key = (
                f"licor/bronze/{instrument_id}"
                f"/year={now.strftime('%Y')}"
                f"/month={now.strftime('%m')}"
                f"/{batch_name}"
            )

            row_id = await loop.run_in_executor(
                executor, insert_buffer_row, instrument_id, batch_name, data, s3_key
            )

            # Step 5: upload to S3 with retries
            try:
                await loop.run_in_executor(executor, upload_to_s3, s3, bucket, s3_key, data)
            except Exception as exc:
                log("error", instrument_id, f"All retries failed for {s3_key}: {exc}")
                # Step 7: leave uploaded=0, do NOT save checkpoint
                return {"status": "error", "rows_uploaded": rows_uploaded, "offset": current_offset, "error": str(exc)}

            # Step 6: mark uploaded, null raw_data, save checkpoint
            await loop.run_in_executor(executor, mark_uploaded, row_id)
            await loop.run_in_executor(executor, save_checkpoint, instrument_id, new_offset)
            rows_uploaded += 1

            line_count = len([ln for ln in data.splitlines() if ln.strip()])
            log("info", instrument_id, f"Uploaded {line_count} lines to s3://{bucket}/{s3_key}")

            return {"status": "ok", "rows_uploaded": rows_uploaded, "offset": new_offset}

        except Exception as exc:
            log("error", instrument_id, f"Unhandled error: {exc}")
            return {"status": "error", "rows_uploaded": rows_uploaded, "offset": current_offset, "error": str(exc)}

    return run()


# -------------------------
# Instrument router
# -------------------------
def process_instrument(instrument, s3, config, loop, executor):
    ingestion_type = instrument.get("ingestion_type")
    if ingestion_type == "growing_file":
        return handle_growing_file(instrument, s3, config, loop, executor)

    async def unsupported():
        log("error", instrument["id"], f"Unknown ingestion_type: {ingestion_type}")
        return {"status": "error", "error": f"Unknown ingestion_type: {ingestion_type}"}

    return unsupported()


# -------------------------
# Pipeline status
# -------------------------
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


# -------------------------
# Main
# -------------------------
async def main():
    config = load_config()
    init_db()
    s3 = create_s3_client()
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
