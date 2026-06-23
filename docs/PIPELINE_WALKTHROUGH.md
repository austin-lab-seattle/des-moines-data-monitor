# Des Moines Data Monitor — Pipeline Bible

A complete, deep walkthrough of the ingestion pipeline: every component, every
function, the checkpoint model, the SQLite buffer, the retry logic, delivery
semantics (idempotency / exactly-once vs at-least-once), concurrency, failure
isolation, and every edge case we could think of.

This document describes the code in
[`scripts/upload_instrument_data.py`](../scripts/upload_instrument_data.py),
[`lambda/dq_collector.py`](../lambda/dq_collector.py), and
[`lambda_api.py`](../lambda_api.py).

---

## Table of contents

1. [System overview](#1-system-overview)
2. [Repository layout](#2-repository-layout)
3. [Configuration reference](#3-configuration-reference)
4. [The uploader: anatomy of one run](#4-the-uploader-anatomy-of-one-run)
5. [File discovery (glob)](#5-file-discovery-glob)
6. [Incremental reads (`read_new_bytes`)](#6-incremental-reads-read_new_bytes)
7. [The checkpoint system](#7-the-checkpoint-system)
8. [The SQLite buffer (`sensor_buffer.db`)](#8-the-sqlite-buffer-sensor_bufferdb)
9. [Upload and retry logic](#9-upload-and-retry-logic)
10. [Delivery semantics: idempotency, at-least-once, crash windows](#10-delivery-semantics-idempotency-at-least-once-crash-windows)
11. [Concurrency and failure isolation](#11-concurrency-and-failure-isolation)
12. [Pipeline status and S3 layout](#12-pipeline-status-and-s3-layout)
13. [Credentials resolution](#13-credentials-resolution)
14. [The AWS side: dq_collector and lambda_api](#14-the-aws-side-dq_collector-and-lambda_api)
15. [Per-instrument formats and row detection](#15-per-instrument-formats-and-row-detection)
16. [Edge cases and failure modes](#16-edge-cases-and-failure-modes)
17. [Operational runbook](#17-operational-runbook)
18. [End-to-end worked example: a CO2 file rollover](#18-end-to-end-worked-example-a-co2-file-rollover)
19. [Known limitations and future hardening](#19-known-limitations-and-future-hardening)
20. [Glossary](#20-glossary)
21. [FAQ](#21-faq)

---

## 1. System overview

The pipeline has three stages across two machines and the cloud:

```text
 FIELD LAPTOP                         AWS                                  VERCEL
 ────────────                         ───                                  ──────
 instrument files                     S3 bucket                            React dashboard
   (data_glob)                        des-moines-data-pipeline-austinlab
       │                                    │
 upload_instrument_data.py  ─PUT──►  {instrument}/bronze/year=/month=/...
       │                                    │
 per-file checkpoints                 dq_collector  (Lambda, hourly via EventBridge)
 + SQLite buffer                        • scans bronze, counts rows/bytes/freshness
       │                                • publishes to CloudWatch AirQuality/Pipeline
 checkpoint mirror  ◄────────────►          │
 {instrument}/checkpoints/            lambda_api  (Lambda, behind API Gateway /metrics)
   checkpoint.json                      • reads CloudWatch + S3 + Cost Explorer
                                        • returns dashboard JSON ──────────►  GET /metrics
```

**Responsibility split:**

| Component | Where | Cadence | Job |
|---|---|---|---|
| `upload_instrument_data.py` | laptop | every 15 min (scheduler) | read **new** bytes from local files, write raw batches to S3 bronze |
| `dq_collector` | AWS Lambda | hourly (EventBridge) | summarize what is in bronze → CloudWatch metrics |
| `lambda_api` | AWS Lambda | on request | assemble dashboard JSON from CloudWatch + S3 + Cost Explorer |
| `frontend` | Vercel | browser | render the dashboard, poll `/metrics` |

The two schedules are **independent**: the laptop pushes data; the cloud
summarizes it. Neither blocks the other.

---

## 2. Repository layout

```text
.
├── lambda_api.py                    # dashboard API Lambda handler
├── lambda/dq_collector.py           # hourly data-quality collector Lambda handler
├── instruments_config.json          # LOCAL instrument config (gitignored)
├── instruments_config.example.json  # tracked template
├── aws_creds.json                   # OPTIONAL local credential fallback (gitignored)
├── requirements.txt                 # boto3
├── scripts/
│   ├── upload_instrument_data.py    # THE UPLOADER (run from repo root)
│   ├── deploy_aws.py                # creates/updates all AWS resources
│   ├── run_pipeline.sh / .bat       # wrappers the schedulers invoke
│   ├── install_launchd_schedule.sh  # macOS scheduler installer
│   └── install_windows_task.ps1     # Windows scheduler installer
├── checkpoints/                     # per-instrument, per-file byte offsets (gitignored)
├── data/                            # local instrument files (gitignored)
└── frontend/                        # Vite React dashboard (deployed via Vercel)
```

**Runtime artifacts** (all gitignored): `sensor_buffer.db`, `collector.log`,
`checkpoints/`, `data/`, `aws_creds.json`, `instruments_config.json`.

> **Why run from the repo root?** The uploader uses CWD-relative paths for the
> config, credentials, log, SQLite DB, and checkpoints. The scheduler wrappers
> `cd` to the repo root before launching it, so all relative paths resolve
> consistently regardless of where the scheduler itself lives.

---

## 3. Configuration reference

`instruments_config.json` (copy from `instruments_config.example.json`):

```json
{
  "instruments": [
    {
      "id": "CO2-LICOR",
      "display_name": "CO2 Li-Cor",
      "location": "Duwamish",
      "ingestion_type": "growing_file",
      "data_glob": "data/co2_li_cor/*CO2-*.txt",
      "active": true
    }
  ],
  "s3_bucket": "des-moines-data-pipeline-austinlab",
  "aws_region": "us-west-2",
  "pipeline_status_key": "pipeline_status.json"
}
```

| Field | Meaning |
|---|---|
| `id` | Stable instrument key; also the top-level S3 prefix. Must match the IDs in `dq_collector.py` and `lambda_api.py`. |
| `display_name` | Human label (dashboard). |
| `location` | Site label, informational. |
| `ingestion_type` | Only `growing_file` is implemented. Anything else returns an error for that instrument (others still run). |
| `data_glob` | **Glob pattern (string or list)** that matches the instrument's source file(s). This is what makes rotation/renaming transparent. |
| `data_file` | *Legacy* single path. Still accepted (treated as a one-element glob) for backward compatibility. |
| `active` | If `false`, the instrument is skipped entirely. |
| `s3_bucket` | Destination bucket. |
| `aws_region` | Default region when using the boto3 credential chain. |
| `pipeline_status_key` | S3 key for the run summary object. |

**Environment overrides** (handy for testing without editing files):

| Variable | Default | Controls |
|---|---|---|
| `INSTRUMENT_CONFIG` | `instruments_config.json` | config path |
| `AWS_CREDS_FILE` | `aws_creds.json` | credential fallback path |
| `SENSOR_BUFFER_DB` | `sensor_buffer.db` | SQLite buffer path |
| `CHECKPOINTS_DIR` | `checkpoints` | local checkpoint directory |
| `AWS_REGION` | — | region for the credential chain |

---

## 4. The uploader: anatomy of one run

`main()` is the entry point. One run does the following:

```text
main()
 ├─ load_config()                      read instruments_config.json
 ├─ init_db()                          create/upgrade the SQLite buffer schema
 ├─ create_s3_client(config)           resolve credentials, build the S3 client
 ├─ active = [i for i if active]       drop inactive instruments
 ├─ gather(process_instrument(i) ...)  run ALL instruments concurrently
 │     return_exceptions=True          one failing instrument never cancels others
 ├─ collect per-instrument results
 └─ write_pipeline_status(...)         write pipeline_status.json to S3
```

Each instrument is handled by `handle_instrument()`, whose coroutine `run()`
performs, in order:

```text
handle_instrument.run()
 1. discover_files(instrument)                 glob → sorted list of paths
 2. load_file_checkpoints(...)                 per-file offsets, local ∪ S3 (max)
 3. drain pending buffer rows (RETRY phase)    re-upload anything left uploaded=0
 4. for each discovered file (INGEST phase):
      a. read_new_bytes(path, offset)          new complete-line data since offset
      b. if nothing new → keep offset, continue
      c. build_s3_key(...)                      deterministic-ish object name
      d. insert_buffer_row(uploaded=0)          WAL: record intent + raw bytes + key
      e. upload_to_s3(...)                       PUT with internal backoff
      f. mark_uploaded(row_id)                   uploaded=1, raw_data=NULL
      g. advance this file's offset
      h. save_file_checkpoints(...)              persist local + S3
 5. save_file_checkpoints(...) (final)
 6. return {status, rows_uploaded, files:{name:offset}}
```

Everything inside `run()` is wrapped in a `try/except` that returns an `error`
status dict, so an unexpected exception in one instrument cannot escape into the
gather and disturb the others.

---

## 5. File discovery (glob)

```python
def discover_files(instrument):
    patterns = instrument.get("data_glob")
    if patterns is None and instrument.get("data_file"):
        patterns = [instrument["data_file"]]      # legacy single-path support
    if isinstance(patterns, str):
        patterns = [patterns]
    matched = set()
    for pattern in patterns:
        matched.update(glob.glob(pattern))
    files = [p for p in matched
             if os.path.isfile(p) and "_quarantine" not in p.split(os.sep)]
    return sorted(files)
```

Key behaviors:

- **Pattern, not a path.** `data/co2_li_cor/*CO2-*.txt` matches
  `2026Feb12-25_CO2-46_Duwamish.txt` today and
  `2026Apr12-28_CO2-46_Duwamish.txt` next month — no config change needed when a
  file rolls over.
- **Non-recursive.** `glob.glob` without `**` does not descend into
  subdirectories, so files in a `_quarantine/` subfolder are not matched. We also
  explicitly drop any path containing a `_quarantine` segment as a second guard
  (this is how the duplicate `… (1).txt` CO2 download is kept out of ingestion).
- **Directories are skipped** (`os.path.isfile`).
- **Sorted** for deterministic processing order (lexicographic; for these
  date-prefixed names that is also roughly chronological).
- **Empty match set** → returns `[]`; the caller logs a warning and the
  instrument contributes 0 rows (others continue).

> **Gotcha:** make the glob specific enough to exclude sibling files you don't
> want (calibration logs, exports). `*CO2-*.txt` is intentionally narrow.

---

## 6. Incremental reads (`read_new_bytes`)

This is the heart of "only upload what's new, and never split a row."

```python
def read_new_bytes(path, offset):
    file_size = os.path.getsize(path)
    used_offset = offset
    if used_offset > file_size:          # file shrank → rotated/truncated in place
        used_offset = 0
    with open(path, "rb") as f:
        f.seek(used_offset)
        raw = f.read()
    if not raw:
        return "", used_offset, used_offset, 0, file_size   # nothing new
    complete = raw
    held_bytes = 0
    if not raw.endswith(b"\n"):
        last_newline = raw.rfind(b"\n")
        if last_newline < 0:
            return "", used_offset, used_offset, len(raw), file_size  # partial-only
        complete = raw[:last_newline + 1]
        held_bytes = len(raw) - len(complete)
    new_offset = used_offset + len(complete)
    data = complete.decode("utf-8", errors="replace")
    return data, used_offset, new_offset, held_bytes, file_size
```

Returns `(data, used_offset, new_offset, held_bytes, file_size)`.

### Byte-level picture

```text
file:   [ ......... already uploaded ......... | new complete lines |partial]
        0                                       ^offset             ^        ^EOF
                                                                    └ last \n └ held_bytes
        |<------------- used_offset ----------->|<--- data -------->|
                                                |<------- new_offset ------->|
```

- **Complete-line hold-back.** If the read does not end on `\n`, we trim back to
  the last newline and *hold* the trailing partial line (`held_bytes`). It is not
  uploaded and the offset does not move past it, so next run re-reads from the
  start of that line once the instrument finishes writing it. **A row is never
  split across two batches.**
- **Partial-only read.** If there is no newline at all in the new bytes (a single
  in-progress line), nothing is uploaded; we wait.
- **Rotation / truncation in place.** If the stored offset is *past* current EOF,
  the file was clearly replaced/truncated; we reset to 0 and re-read from the
  start (`used_offset < offset` in the caller signals this and is logged).
- **Encoding.** Bytes are decoded as UTF-8 with `errors="replace"`, so an odd
  byte never crashes the run; it becomes `�`. We read in **binary** and slice on
  byte boundaries precisely so offsets are exact byte counts (not character
  counts), which keeps `seek()` correct for multi-byte content.

---

## 7. The checkpoint system

### What a checkpoint is

A checkpoint answers one question per file: **"how many bytes of this file have
we durably uploaded?"** It is **both file-level and byte-level** — a byte offset
stored under each filename.

```json
// checkpoints/CO2-LICOR.json   (local)  and
// s3://…/CO2-LICOR/checkpoints/checkpoint.json   (mirror)
{
  "instrument_id": "CO2-LICOR",
  "files": {
    "2026Feb12-25_CO2-46_Duwamish.txt": { "offset": 9537866, "updated_at": "2026-06-23T…" },
    "2026Apr12-28_CO2-46_Duwamish.txt": { "offset":  250000, "updated_at": "2026-06-23T…" }
  },
  "updated_at": "2026-06-23T…"
}
```

A brand-new file simply has no entry yet → defaults to offset 0 → read from the
beginning. Old files keep their entry and resume exactly where they stopped.

### Two copies: local + S3

| Copy | Path | Purpose |
|---|---|---|
| Local | `checkpoints/{id}.json` | fast, authoritative on the laptop |
| S3 mirror | `{id}/checkpoints/checkpoint.json` | disaster recovery if the laptop / `checkpoints/` is lost |

**Load = union, merged by max offset per file:**

```python
merged[base] = {"offset": max(local_offset, s3_offset)}
```

So whichever copy is further ahead wins per file. If the S3 copy is ahead (laptop
was reimaged), we log a recovery message and continue from S3's offset.

**Save** writes the local file and then best-effort PUTs the same payload to S3.
We save **after every successful file upload** (so progress is durable mid-run)
*and* once more at the end of the instrument's run.

### Legacy migration

The previous design stored a single offset per instrument:
`{"instrument_id": "...", "offset": N}`. `_normalize_checkpoint()` upgrades it on
read:

- New format (`{"files": {...}}`) → used as-is.
- Legacy with exactly one matching file → that offset is assigned to that file.
- Legacy with **no** matching files → empty (start fresh).
- Legacy with **several** matching files → ambiguous: assign the offset to the
  lexicographically last file and start the rest at 0, **and log a warning**
  (we cannot know which file the bare offset belonged to).

### What the checkpoint is *not*

We do **not** list the bronze objects in S3 to reconcile "what's already
uploaded." Dedup is purely the offset bookmark (plus the stable S3 key for
retries). Consequence: if bronze objects are deleted in S3 but the checkpoint
still points past them, they are **not** re-uploaded. Recovery from accidental
bronze deletion means lowering/clearing the relevant offset.

---

## 8. The SQLite buffer (`sensor_buffer.db`)

A single table acts as a **write-ahead log** for in-flight batches.

### Schema

```sql
CREATE TABLE buffer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT NOT NULL,
    batch_name    TEXT NOT NULL,
    raw_data      TEXT,            -- the batch text; NULLed after upload
    s3_key        TEXT NOT NULL,   -- the FIXED destination key for this batch
    start_offset  INTEGER,         -- byte offset this batch started at
    end_offset    INTEGER,         -- byte offset after this batch
    source_file   TEXT,            -- basename the batch came from
    uploaded      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    uploaded_at   TEXT
);
CREATE INDEX idx_buffer_pending ON buffer (instrument_id, uploaded);
```

`init_db()` is **idempotent and self-migrating**: it `CREATE TABLE IF NOT
EXISTS`, then `ALTER TABLE ADD COLUMN` for `start_offset`, `end_offset`, and
`source_file` if an older DB lacks them. The index makes "find pending rows for
this instrument" cheap.

### Row lifecycle (state machine)

```text
            insert_buffer_row()                 upload ok + mark_uploaded()
   (none) ────────────────────► uploaded=0 ───────────────────────────► uploaded=1
                                raw_data=<bytes>                          raw_data=NULL
                                s3_key=<fixed>                            uploaded_at=now
                                     │
                                     │ upload fails
                                     ▼
                                stays uploaded=0  ──► retried next run (same s3_key)
```

### Why it exists (the important part)

1. **Stable destination key.** `s3_key` is computed once and stored. Retries
   reuse the *same* key, so re-uploading **overwrites the same object** instead
   of creating a second file. This is what prevents most duplicates after a crash
   (see §10).
2. **Retry queue across runs.** A failed upload leaves `uploaded=0`; the next run
   re-attempts it in the RETRY phase before reading new data — even days later.
3. **Crash safety.** Intent (the row) is recorded *before* the network PUT, so we
   never "forget" a batch we started.

**Honest note on redundancy:** for `growing_file` sources, the data also lives
durably in the file on disk, so storing `raw_data` in SQLite is partly redundant
for durability. Its non-redundant value is the stable-key idempotency and the
cross-run retry queue. A future optimization could store only metadata
(`s3_key`, `start_offset`, `end_offset`, `source_file`) and re-read bytes from
the file on retry, shrinking the DB.

---

## 9. Upload and retry logic

There are **two** retry layers.

### Layer A — within a single PUT (`upload_to_s3`)

```python
def upload_to_s3(s3, bucket, s3_key, raw_data):
    delays = [2, 4, 8]
    for attempt, delay in enumerate(delays, start=1):
        try:
            s3.put_object(Bucket=bucket, Key=s3_key, Body=raw_data.encode("utf-8"))
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < len(delays):
                time.sleep(delay)
    raise last_exc
```

Timeline of a fully-failing upload:

```text
attempt 1 ──fail──► sleep 2s ──► attempt 2 ──fail──► sleep 4s ──► attempt 3 ──fail──► raise
```

- **3 attempts**, with **2s then 4s** backoff between them.
- **Nuance / minor latent bug:** the list has three delays `[2, 4, 8]`, but the
  sleep is guarded by `attempt < len(delays)`, so the `8` is **never slept** — it
  is paired with the third attempt, which raises instead of sleeping. Net effect:
  two sleeps (2s, 4s). If three sleeps were intended, the loop needs a fourth
  attempt. Behaviorally harmless today; flagged for cleanup.

### Layer B — across runs (the RETRY phase)

Before reading any new data, `handle_instrument` drains pending rows:

```python
for row_id, batch_name, raw_data, s3_key, end_offset, source_file in pending:
    if raw_data is None:                      # marked-but-not-nulled? skip safely
        continue
    upload_to_s3(s3, bucket, s3_key, raw_data)   # SAME key as the original attempt
    mark_uploaded(row_id)
    if source_file and end_offset > current:     # advance that file's checkpoint
        file_offsets[source_file] = {"offset": end_offset}
```

So a batch that failed N runs ago is retried (idempotently, same key) on the next
run, and only then does its file's offset advance. **Failure of one batch does
not advance past it**, which is what guarantees no data is skipped.

---

## 10. Delivery semantics: idempotency, at-least-once, crash windows

**Summary:** the pipeline is **at-least-once with no data loss**. Duplicates are
possible only in one narrow crash window, and the dedup design eliminates them in
every other window.

Recall the per-batch order in the INGEST phase:

```text
(1) insert_buffer_row()      uploaded=0, raw_data set, s3_key fixed
(2) upload_to_s3()           PUT object at s3_key
(3) mark_uploaded()          uploaded=1, raw_data=NULL
(4) file_offsets[base]=new   advance offset (in memory)
(5) save_file_checkpoints()  persist offset (local + S3)
```

Now consider a crash at each gap:

| Crash point | On restart | Outcome |
|---|---|---|
| **After (1), before (2)** | row is `uploaded=0` with bytes + key → RETRY phase PUTs it (same key), marks uploaded, advances offset | **exactly once** ✅ |
| **After (2), before (3)** | object exists; row still `uploaded=0` → RETRY PUTs the **same key again** (overwrite, identical bytes), marks uploaded, advances offset | **once** (object written twice, same key/content) ✅ |
| **After (3), before (5)** | row is `uploaded=1` (won't retry) but offset **not persisted** → INGEST re-reads the same bytes, generates a **new timestamped key**, PUTs a **second object** | **duplicate object** (same data, different name) ⚠️ |
| **After (5)** | offset persisted; nothing to redo | **exactly once** ✅ |

So the only duplication window is the small gap between marking a batch uploaded
and persisting its offset. Because the S3 key currently embeds a **timestamp**, a
re-read produces a *different* key and therefore a *second* object. `dq_collector`
would then count those rows twice until the duplicate is removed. **No data is
ever lost** — at worst a batch is delivered twice.

### How to make it exactly-once (recommended hardening)

Make the S3 key **deterministic from the byte range** instead of a timestamp:

```text
{id}/bronze/year=/month=/{stem}__off_{start_offset}-{end_offset}.txt
```

Then a re-read of the same byte range yields the **same key**, so the second PUT
overwrites the first → no duplicate, even in the after-(3)-before-(5) window.
(Alternatively, persist the offset advance in the *same* SQLite transaction as
`mark_uploaded`, making (3)+(5) atomic.)

### What protects against data loss

- The offset only advances **after** a confirmed upload (or a confirmed retry).
- A failed upload leaves the row pending and the offset unmoved → retried.
- The complete-line hold-back means a half-written row is never consumed early.
- The S3 checkpoint mirror means a lost laptop resumes, it doesn't restart.

---

## 11. Concurrency and failure isolation

```python
results = await asyncio.gather(*coroutines, return_exceptions=True)
```

- **Per-instrument isolation.** Each instrument is a separate coroutine.
  `return_exceptions=True` means a raised exception becomes that instrument's
  *result* rather than propagating — the other four are unaffected. On top of
  that, `handle_instrument.run()` has its own `try/except` returning an `error`
  dict, so exceptions almost never reach the gather at all.
- **Blocking I/O off the event loop.** File reads, SQLite calls, and S3 PUTs are
  dispatched via `loop.run_in_executor(executor, ...)` on a `ThreadPoolExecutor`,
  so a slow disk or slow S3 call for one instrument does not stall the others.
- **What "silent instrument" looks like:**
  - file present, no new bytes → `read_new_bytes` returns empty → logged "no new
    data", `rows_uploaded=0`, status `ok`.
  - glob matches nothing → logged **warning** "No files matched", status `ok`,
    `rows_uploaded=0`.
- **Nuance:** a silent instrument still reports `ok` (the warning is only in the
  log); it is not escalated to `degraded` in `pipeline_status.json`. Staleness is
  surfaced on the dashboard via the `Freshness` metric from `dq_collector`, not
  via the pipeline status. If you want a disconnected instrument to *alarm*, add a
  `stale` status (e.g. when no file matched, or when freshness exceeds a
  threshold).
- **No cross-process lock.** Two uploader processes running at once could
  double-process. The schedulers run it serially, so this does not happen in
  practice, but there is no hard mutex. (A lockfile or `flock` would close it.)

---

## 12. Pipeline status and S3 layout

### `pipeline_status.json` (written once per run)

```json
{
  "last_run": "2026-06-23T18:05:11",
  "status": "ok",                      // "degraded" if ANY instrument != ok
  "instruments": {
    "CO2-LICOR": { "status": "ok", "rows_uploaded": 0,
                   "files": { "2026Feb12-25_CO2-46_Duwamish.txt": 9537866 } },
    "BC-MA200":  { "status": "ok", "rows_uploaded": 0, "files": { … } }
  }
}
```

### S3 object layout

```text
{instrument_id}/bronze/year=YYYY/month=MM/{stem}__batch_YYYYMMDDTHHMMSS.txt
{instrument_id}/checkpoints/checkpoint.json
pipeline_status.json
```

The batch name embeds the **source file stem** (sanitized to
`[A-Za-z0-9._-]`) so every bronze object is traceable to the file it came from,
and the `year=/month=` path is Hive-style partitioning that Athena/Glue can read
directly when Silver/Gold are built.

---

## 13. Credentials resolution

Both the uploader and `deploy_aws.py` resolve credentials the same way:

```text
1. boto3 default chain (env vars → AWS profile → IAM role)   ← preferred
2. aws_creds.json (if the chain found nothing and the file exists)  ← fallback
3. otherwise: warn (uploader) / exit 1 (deploy)
```

```python
session = boto3.Session()
if session.get_credentials() is not None:
    return session.client("s3", region_name=session.region_name or region)
if os.path.exists(CREDS_FILE):
    creds = load_aws_credentials()
    return boto3.client("s3", aws_access_key_id=…, aws_secret_access_key=…, region_name=…)
```

Region precedence: `config.aws_region` → `AWS_REGION` env → `us-west-2`. Keeping
the standard chain first means production hosts can use an IAM role or profile and
no static key ever has to live in the repo; the field laptop can still drop an
`aws_creds.json` (gitignored) if that is simpler.

---

## 14. The AWS side: dq_collector and lambda_api

### `dq_collector` (hourly metrics producer)

For each instrument and each tier (`bronze`, `silver`, `gold`):

- list objects under `{id}/{tier}/`, count files and total bytes;
- emit `{Tier}Files` and `{Tier}Size` to CloudWatch namespace
  `AirQuality/Pipeline`, dimensioned by instrument;
- for **bronze**: also compute `Freshness` (hours since latest object) and
  `BronzeRows` — by **downloading every bronze object and counting data rows**
  with the per-instrument `is_data_row()` parser.

It then emits `LambdaDuration` and `LambdaSuccess`, batching metric puts in groups
of 20 (the CloudWatch limit). On any exception it emits `LambdaSuccess=0` and
re-raises.

> **Cost/scale caveat:** `BronzeRows` re-downloads and re-counts **all** bronze
> data every hour — O(total data) per run. Fine for now; replace with
> push-from-uploader metrics or a manifest as bronze grows. Silver/Gold tiers are
> always empty today, so their metrics are 0.

### `lambda_api` (on-demand dashboard JSON)

For each instrument:

- read latest `BronzeSize` and `BronzeRows` from CloudWatch (`Maximum` over the
  last 168h) — these come **only** from `dq_collector`;
- read the most recent bronze object's `LastModified` directly from S3 (this is
  independent of `dq_collector`).

Then it computes `refreshTime`, `systemStatus` (`ONLINE` if any data exists else
`DEGRADED`), and the KPI block including `mtdCost` from **Cost Explorer**
(`us-east-1`, account-level month-to-date unblended). Response is JSON with
permissive CORS.

> **Security caveat:** `/metrics` is public and unauthenticated and includes
> `mtdCost` — your AWS bill is readable by anyone with the URL. Drop `mtdCost`
> from the public payload or put the API behind auth before sharing widely.

### Dependency summary

```text
uploader ──► bronze objects ──► dq_collector ──► CloudWatch (rows/size/freshness)
                   │                                   │
                   └────────────► lambda_api ◄─────────┘ (+ S3 LastModified + Cost Explorer)
                                       │
                                       ▼  GET /metrics  → dashboard
```

Kill `dq_collector` and the dashboard still shows last-update time, online status,
and cost — but row/size tiles go to 0 (nothing else produces those metrics).

---

## 15. Per-instrument formats and row detection

Each instrument has a different raw layout; `dq_collector.is_data_row()`
recognizes a *data* row (vs header/comment) so counts are accurate. A line is a
data row only if it is non-empty, does not start with `%` or `#`, and matches the
instrument's shape:

| Instrument | File ends in | Delimiter | A data row looks like |
|---|---|---|---|
| BC-MA200 | `.csv` | quoted CSV | >10 fields, field0 starts `MA…`, field1 is digits |
| CO2-LICOR | `.txt` | TAB | field0 = `YYYY-MM-DD`, field1 = `HH:MM:SS` |
| NEPH-PM25 | `.csv` | CSV (BOM) | field0 = `YYYY/MM/DD HH:MM:SS`, field1 numeric |
| NO2-CAPS | `.dat` | CSV | ≥10 fields, field0 = 6 digits, field3 numeric; `%` comments |
| SMPS | `.csv` | CSV | >40 fields, field0 digits, field1 a date-time; huge metadata header |

This parser is also a head start for a future Silver transform (it already knows
how to find the real rows in each format).

---

## 16. Edge cases and failure modes

| # | Scenario | Behavior | Risk |
|---|---|---|---|
| 1 | New file appears (rotation) | no entry → offset 0 → ingested | none ✅ |
| 2 | Several files growing at once | independent offsets, all read | none ✅ |
| 3 | Rows appended mid-file | resume at offset, complete lines only | none ✅ |
| 4 | Last line half-written | held back via `held_bytes`, not uploaded | none ✅ |
| 5 | Upload fails (network) | 3 attempts; row stays pending; retried next run | none ✅ |
| 6 | Crash before PUT | RETRY phase re-PUTs same key | none ✅ |
| 7 | Crash after PUT, before mark | RETRY re-PUTs same key (overwrite) | none ✅ |
| 8 | Crash after mark, before offset save | re-read → **new key** → 2nd object | **duplicate** ⚠️ |
| 9 | Local `checkpoints/` wiped | recovered from S3 (max merge) | none ✅ |
| 10 | File truncated/rotated in place | offset > size → reset to 0, re-upload file | duplicate of that file once ⚠️ |
| 11 | One instrument errors/silent | others continue; silent one logs warning, status `ok` | staleness not alarmed ⚠️ |
| 12 | File replaced, same name, size ≥ offset | resume at old offset → miss data | **data miss** ⚠️ (identity is by name) |
| 13 | Bronze objects deleted in S3 | not re-uploaded (offset past them) | recovery needs offset reset ⚠️ |
| 14 | Two uploaders at once | no lock → possible double-process | duplicates ⚠️ (serial in practice) |
| 15 | Non-UTF-8 bytes | decoded with `errors="replace"` (`�`) | cosmetic ⚠️ |
| 16 | Checkpoint grows over years | one entry per file ever seen | tiny, unbounded ⚠️ |
| 17 | `ingestion_type` unknown | that instrument returns error; others run | isolated ✅ |
| 18 | Glob matches an unwanted sibling file | it gets ingested | make glob specific ⚠️ |

Legend: ✅ handled, ⚠️ known limitation / needs operator awareness.

---

## 17. Operational runbook

### Run one pass manually

```bash
cd <repo root>
python3 scripts/upload_instrument_data.py
tail -f collector.log         # watch results
```

### Install the scheduler

```bash
# macOS (every 900s)
bash scripts/install_launchd_schedule.sh 900
# Windows (every 15 min)
powershell -ExecutionPolicy Bypass -File scripts/install_windows_task.ps1 -EveryMinutes 15
```

### Verify in S3

```bash
aws s3 ls s3://des-moines-data-pipeline-austinlab/CO2-LICOR/bronze/ --recursive
aws s3 cp s3://des-moines-data-pipeline-austinlab/CO2-LICOR/checkpoints/checkpoint.json -
```

### Recovery procedures

| Situation | Action |
|---|---|
| Re-ingest a file from scratch | lower/remove its entry in `checkpoints/{id}.json` (and the S3 copy), then run |
| Laptop reimaged | just run — offsets recover from the S3 checkpoint mirror |
| Suspected duplicate batch | find duplicate-content objects under `bronze/`, delete the extra; counts self-correct next `dq_collector` run |
| Stuck pending rows | inspect `sqlite3 sensor_buffer.db "SELECT id,instrument_id,s3_key,uploaded FROM buffer WHERE uploaded=0"` |
| Bronze accidentally deleted | reset the affected offsets to 0 (or before the deleted range) and run |

### Inspect the buffer

```bash
sqlite3 sensor_buffer.db "SELECT instrument_id, COUNT(*) , SUM(uploaded) FROM buffer GROUP BY 1;"
```

---

## 18. End-to-end worked example: a CO2 file rollover

Setup: `data_glob = data/co2_li_cor/*CO2-*.txt`. The instrument has been writing
`2026Feb12-25_CO2-46_Duwamish.txt` and we're caught up (offset = file size). Today
it starts a new file `2026Apr12-28_CO2-46_Duwamish.txt`.

**Run at T0** (only the old file exists, no new bytes):

```text
discover_files → [2026Feb12-25_…txt]
checkpoint     → {2026Feb12-25_…txt: 9537866}
read_new_bytes(old, 9537866) → "", offset stays 9537866   (caught up)
result: rows_uploaded=0, status ok
```

**Run at T1** (new file now has 500 KB, ends mid-line):

```text
discover_files → [2026Apr12-28_…txt, 2026Feb12-25_…txt]   (sorted)
checkpoint     → {2026Feb12-25_…txt: 9537866}             (new file: no entry → 0)

file = 2026Apr12-28_…txt, offset 0
  read_new_bytes(new, 0):
     raw = 500 KB, does NOT end on \n
     trim to last \n → complete = 499.8 KB, held_bytes = 200
  build_s3_key → CO2-LICOR/bronze/year=2026/month=04/2026Apr12-28_CO2-46_Duwamish__batch_20260412T0900xx.txt
  insert_buffer_row(uploaded=0, start=0, end=499800, source=2026Apr12-28_…txt)
  upload_to_s3 → ok
  mark_uploaded
  offset[2026Apr12-28_…txt] = 499800
  save_file_checkpoints (local + S3)

file = 2026Feb12-25_…txt, offset 9537866 → no new data → skip

result: rows_uploaded=1, status ok
checkpoint now: {2026Feb12-25_…txt: 9537866, 2026Apr12-28_…txt: 499800}
```

**Run at T2** (new file grew by another 1 MB, now ends on `\n`):

```text
read_new_bytes(new, 499800): reads the 200 held bytes + ~1 MB, ends on \n, held=0
  → uploads the next batch, offset advances to new EOF
```

The old file was never re-read, the new file was picked up with **zero config
change**, the partial line at T1 was completed and uploaded cleanly at T2, and
each batch is a distinct, traceable bronze object.

---

## 19. Known limitations and future hardening

| Area | Limitation | Suggested fix |
|---|---|---|
| Delivery | rare duplicate in the mark→offset crash window (§10) | deterministic offset-range S3 keys, or atomic mark+offset |
| Backoff | third backoff delay (`8`) is never slept (§9) | add a 4th attempt or restructure the loop |
| File identity | by **filename**, not content/inode | also track inode + mtime, or a content hash |
| Concurrency | no cross-process lock | lockfile / `flock` around a run |
| Silent instrument | reports `ok`, not `degraded` | add a `stale` status + threshold |
| dq_collector | re-counts all bronze hourly (O(data)) | push metrics from the uploader, or a manifest |
| Buffer size | stores full `raw_data` | store metadata only, re-read on retry |
| API | public, leaks `mtdCost` | drop the field or add auth |
| Checkpoint growth | one entry per file ever seen | prune entries for files no longer matched |
| Medallion | Bronze only | build Silver (Glue/Lambda → Parquet + Glue Catalog), then Gold |

---

## 20. Glossary

| Term | Meaning |
|---|---|
| **Bronze** | raw, unmodified instrument bytes landed in S3 (the only tier today) |
| **Silver / Gold** | cleaned/typed (Silver) and aggregated/business-ready (Gold) tiers — not built yet |
| **Checkpoint** | per-file byte offset recording how far a file has been uploaded |
| **Buffer** | the SQLite write-ahead log of in-flight/failed batches |
| **Batch** | one contiguous run of new complete lines uploaded as a single S3 object |
| **Offset** | a byte position within a source file |
| **Held bytes** | a trailing partial line withheld until it's complete |
| **Freshness** | hours since the latest bronze object for an instrument |
| **At-least-once** | every batch is delivered one or more times (never zero) |

---

## 21. FAQ

**Q: If I rename or rotate a file mid-day, do I lose data?**
No. The glob picks up the new name; the old file keeps its checkpoint; the new
file starts at 0. (Caveat: a *replaced* file reusing the *same* name with size ≥
the stored offset — see edge case 12.)

**Q: Will the same row ever be uploaded twice?**
Only in the narrow crash window in §10 (after a batch is marked uploaded but
before its offset is persisted). No row is ever lost. Deterministic keys remove
even that case.

**Q: Do I need `dq_collector`?**
It's required for the dashboard's row/size tiles (it's the only producer of those
CloudWatch metrics). Last-update, status, and cost work without it. Its hourly
full re-count is the weak point and a good candidate to replace.

**Q: Is Athena used?**
Not today. It only becomes useful once Silver/Gold Parquet exists to query.

**Q: Where do credentials come from?**
The standard boto3 chain first (env / profile / role), then `aws_creds.json` as a
fallback. Nothing secret needs to be committed.

**Q: What happens if the laptop dies?**
Re-run on a fresh machine — offsets recover from the S3 checkpoint mirror, so
ingestion resumes rather than restarting.
