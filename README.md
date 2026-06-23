# Des Moines Data Monitor

Air quality data pipeline and monitoring dashboard for the DEOHS research project.
The field laptop uploads instrument data to S3, AWS publishes pipeline metrics, and
the Vercel React dashboard reads those metrics through an API Gateway endpoint.

## Instruments

| ID | Instrument | Status |
|----|------------|--------|
| BC-MA200 | Black Carbon MA200 | Active |
| CO2-LICOR | CO2 Li-Cor | Active |
| NEPH-PM25 | Nephelometer PM25 | Active |
| NO2-CAPS | NO2 CAPS | Active |
| SMPS | SMPS | Active |

## Architecture

```text
Field laptop                            AWS Cloud                         Vercel
------------                            ---------                         ------
Instrument files (data_glob)            S3 bucket                         React dashboard
     |                                  des-moines-data-pipeline-austinlab      |
scripts/upload_instrument_data.py  -->  {instrument}/bronze/...                 |
     |                                       |                                   |
per-file checkpoints + SQLite buffer    dq_collector Lambda, hourly             |
                                             |                                   |
                                        CloudWatch AirQuality/Pipeline           |
                                             |                                   |
                                        aq-dashboard-api Lambda                  |
                                             |                                   |
                                        API Gateway /metrics  ------------------+
```

## Repository layout

```text
.
├── lambda_api.py               # dashboard API Lambda handler (deployed to AWS)
├── lambda/dq_collector.py      # hourly data-quality collector Lambda handler
├── instruments_config.json     # local instrument config (gitignored)
├── instruments_config.example.json  # tracked template for the config above
├── aws_creds.json              # optional local credential fallback (gitignored)
├── requirements.txt
├── scripts/
│   ├── upload_instrument_data.py     # the uploader (run from repo root)
│   ├── deploy_aws.py                 # creates/updates all AWS resources
│   ├── run_pipeline.sh / .bat        # wrappers the schedulers call
│   ├── install_launchd_schedule.sh   # macOS scheduler installer
│   └── install_windows_task.ps1      # Windows scheduler installer
├── checkpoints/                # per-instrument, per-file byte offsets (gitignored)
├── data/                       # local instrument files (gitignored)
└── frontend/                   # Vite React dashboard (deployed via Vercel)
```

## Components

- `scripts/upload_instrument_data.py` reads all active instruments from
  `instruments_config.json`, discovers source files with a **glob pattern**
  (`data_glob`), keeps a **byte offset per file**, buffers upload attempts in
  SQLite, and writes bronze batches to S3. Run it from the repository root.
- `lambda/dq_collector.py` scans S3 once per hour and publishes file count, byte
  size, freshness, and row-count metrics to CloudWatch namespace
  `AirQuality/Pipeline`. Its per-instrument `is_data_row()` logic skips headers
  and comment lines so only real data rows are counted.
- `lambda_api.py` serves the dashboard JSON payload through API Gateway at
  `/metrics`; it reads dashboard metrics from CloudWatch/S3 and reads
  month-to-date AWS account cost from Cost Explorer.
- `scripts/deploy_aws.py` creates or updates the bucket, Lambda role, both
  Lambdas, API Gateway, and the hourly EventBridge rule.
- `frontend/` is the Vite React dashboard deployed through the existing Vercel
  project.

## File discovery and checkpoints

Instrument filenames encode a date range, for example
`2026Feb12-25_CO2-46_Duwamish.txt`. When the instrument rolls over to a new file
(`2026Apr12-28_CO2-46_Duwamish.txt`) the uploader picks it up automatically
because each instrument is configured with a glob, not a single path:

```json
{ "id": "CO2-LICOR", "ingestion_type": "growing_file",
  "data_glob": "data/co2_li_cor/*CO2-*.txt", "active": true }
```

The uploader then:

- globs all matching files each run (a single string or a list of patterns);
- tracks a byte offset **per file** in `checkpoints/{instrument}.json`
  (`{"files": {"<filename>": {"offset": N}}}`), so a brand-new file starts at 0
  while existing files continue where they left off — no re-uploads, no gaps;
- holds back a trailing partial line until the instrument finishes writing it,
  so a row is never split across two batches (this is how new rows appended to
  any file are captured cleanly);
- resets a file to offset 0 if its stored offset is past the current end of file
  (file rotated or truncated in place);
- names each S3 object after its source file
  (`{stem}__batch_{timestamp}.txt`) so every bronze object is traceable.

Legacy single-offset checkpoints (`{"offset": N}`) are migrated automatically on
the next run.

## Scheduling

There are two separate schedules:

- Laptop upload schedule: runs on the field laptop because it reads local
  instrument files and uploads new bytes to S3.
- AWS collector schedule: runs in EventBridge and invokes `dq_collector` hourly
  to summarize what is already in S3.

Run one upload pass manually:

```bash
python3 scripts/upload_instrument_data.py
```

Install a Windows Task Scheduler job that runs every 15 minutes:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows_task.ps1 -EveryMinutes 15
```

Install a macOS launchd job that runs every 900 seconds:

```bash
bash scripts/install_launchd_schedule.sh 900
```

Run the wrappers directly:

```bash
scripts/run_pipeline.sh        # macOS/Linux
scripts\run_pipeline.bat       # Windows
```

Use a 15-minute upload interval while instruments are actively writing. A 30- or
60-minute interval is fine when near-real-time visibility is not needed.

## AWS credentials

Both the uploader and `deploy_aws.py` use the **standard boto3 credential
chain** first — environment variables, a shared AWS profile, or an attached IAM
role — and fall back to `aws_creds.json` only if the chain finds nothing. Prefer
one of:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-west-2
# or
aws configure --profile des-moines    # then export AWS_PROFILE=des-moines
```

If you keep using `aws_creds.json`, it stays gitignored. Rotate that IAM key
periodically and keep it scoped to least privilege (S3 write to the data bucket,
plus whatever the deploy user needs).

## Local config

The sensitive/local files are gitignored: `aws_creds.json`,
`instruments_config.json`, `checkpoints/`, `sensor_buffer.db`, `collector.log`,
`data/`. Copy `instruments_config.example.json` to `instruments_config.json` and
point each `data_glob` at the live file locations on the laptop.

Current AWS target:

```text
Region: us-west-2
Bucket: des-moines-data-pipeline-austinlab
API: https://yvhb48sthk.execute-api.us-west-2.amazonaws.com/metrics
```

## Common commands

```bash
python3 -m pip install -r requirements.txt   # install deps
python3 scripts/upload_instrument_data.py     # one upload pass
python3 scripts/deploy_aws.py                 # deploy/update AWS resources
cd frontend && npm install && npm run dev     # run the dashboard locally
```

For Vercel, set `VITE_API_URL` to the API Gateway `/metrics` URL printed by
`scripts/deploy_aws.py`.

## Data layout (medallion)

Today the pipeline is **Bronze only**. The uploader lands raw, unmodified
instrument bytes; `dq_collector` only *reads* bronze to publish metrics — it does
not transform data into silver or gold (those S3 prefixes are currently empty).

S3 layout:

```text
{instrument_id}/bronze/year=YYYY/month=MM/{stem}__batch_YYYYMMDDTHHMMSS.txt
{instrument_id}/checkpoints/checkpoint.json
pipeline_status.json
```

To extend into a full medallion architecture later:

| Layer | Status | What to add on the AWS side |
|-------|--------|------------------------------|
| Bronze | done | raw files, partitioned by year/month (optionally add an S3 lifecycle rule) |
| Silver | to build | a per-instrument transform (AWS Glue PySpark job, or Lambda for small volumes) that parses, types, dedupes, and normalizes timestamps to UTC, writing Parquet to `{id}/silver/`; register tables in the Glue Data Catalog |
| Gold | to build | business aggregates (hourly/daily means, QA flags, cross-instrument joins) via Athena CTAS or a Glue job to `{id}/gold/`; the dashboard reads gold instead of recounting bronze |
| Query/catalog | partial | Athena workgroup + Glue Catalog tables |
| Orchestration | partial | Step Functions or Glue Workflow to chain bronze → silver → gold (currently only an hourly EventBridge rule drives `dq_collector`) |

The real work in Silver is unifying five different raw schemas into one canonical
schema per instrument; `dq_collector.is_data_row()` is a useful parsing head
start.

## Cost tile

The dashboard `MTD COST` tile comes from AWS Cost Explorer through `lambda_api.py`
(`Dashboard -> API Gateway -> aq-dashboard-api Lambda -> Cost Explorer`):

- It is account-level month-to-date unblended cost, not per-bucket or
  per-instrument.
- Cost Explorer data can lag, so the tile may not match live usage minute-by-minute.
- If the Lambda role lacks `ce:GetCostAndUsage`, the tile shows `N/A`.
- The API Lambda calls Cost Explorer in `us-east-1` (normal for billing APIs)
  even though project resources live in `us-west-2`.

Add an AWS Budget or billing alarm for hard guardrails; the tile is only visibility.

## Operational walkthrough

1. Confirm `instruments_config.json` `data_glob` patterns match the live files.
2. Run `python3 scripts/upload_instrument_data.py` once and check `collector.log`.
3. Confirm S3 has `{instrument_id}/bronze/...` files and
   `{instrument_id}/checkpoints/checkpoint.json`.
4. Run `python3 scripts/deploy_aws.py` after Lambda/API changes.
5. Wait for the hourly `dq_collector` run or invoke it manually in AWS Lambda.
6. Open the API Gateway `/metrics` URL and confirm JSON contains `kpis`,
   `refreshTime`, and all five instruments.
7. Set Vercel `VITE_API_URL` to that `/metrics` URL and redeploy the frontend.
8. Install the laptop scheduler only after a clean manual upload pass.

## Security notes

- **The `/metrics` endpoint is public and unauthenticated, and it returns
  month-to-date AWS account cost.** Anyone with the URL can read your billing
  number. Before this is widely shared, either drop `mtdCost` from the public
  payload or put the API behind auth (API key / Cognito / signed requests).
- Do not commit AWS credentials, Vercel tokens, sample data, checkpoint files,
  logs, SQLite buffers, or generated Lambda zips.
- The Lambda execution role currently attaches the broad managed policies
  `CloudWatchReadOnlyAccess` and `AmazonS3ReadOnlyAccess`; tighten to
  bucket-scoped least privilege when convenient.
- `dq_collector` re-downloads and recounts every bronze file each hour
  (O(all data) per run). Replace full recounts with a manifest/table or
  incremental approach once bronze grows large.

## Next steps

- Add an AWS Budget and project resource tags so billing can be separated.
- Address the public cost endpoint (see Security notes).
- Build the Silver layer once the raw upload path has been stable for several days.
