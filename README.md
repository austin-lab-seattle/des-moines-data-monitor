# Des Moines Data Monitor

Air quality data pipeline and monitoring dashboard for the DEOHS research project.
The field laptop uploads instrument data to S3 Bronze, AWS builds a deduplicated
Silver layer, an API Lambda serves metrics and review records, and the Vercel
React dashboard reads it through API Gateway.

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
Field laptop                            AWS Cloud                              Vercel
------------                            ---------                              ------
Instrument files (data_glob)            S3 bucket                              React dashboard
     |                                  des-moines-data-pipeline-austinlab           |
scripts/upload_instrument_data.py  -->  {instrument}/bronze/...                      |
     |                                       |                                        |
per-file checkpoints + SQLite buffer    aq-silver-builder Lambda                     |
                                             |                                        |
                                        {instrument}/silver/...                      |
                                             |                                        |
                                        aq-dashboard-api Lambda                      |
                                        /metrics + review endpoints                  |
                                             |                                        |
                                        API Gateway  -------------------------------+
```

## Repository layout

```text
.
├── lambda_api.py               # dashboard API Lambda: metrics + silver review API
├── lambda/
│   └── silver_builder.py       # rebuilds deduplicated silver CSVs from bronze
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
- `lambda/silver_builder.py` rebuilds one Silver CSV per instrument from Bronze,
  keeps only real data rows, removes duplicates, and writes a metadata sidecar
  with the unique row count.
- `lambda_api.py` serves the dashboard JSON payload through API Gateway at
  `/metrics`. On every request it scans the bronze prefix and counts the real
  data rows and bytes per instrument live (its `is_data_row()` logic skips
  headers and comment lines), reads Silver row counts from metadata, and reads
  month-to-date AWS account cost from Cost Explorer. It also exposes Silver
  record review endpoints for browsing, flagging, and correction notes.
- `scripts/deploy_aws.py` creates or updates the bucket, Lambda role, the API
  Lambda, and the API Gateway.
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

There are two schedules:

- The laptop upload job runs on the field laptop because it reads local
  instrument files and uploads new bytes to S3 Bronze.
- The cloud Silver builder runs daily in EventBridge and rebuilds the
  deduplicated Silver CSVs from Bronze.

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
API base: https://yvhb48sthk.execute-api.us-west-2.amazonaws.com
```

API routes configured by `scripts/deploy_aws.py`:

```text
GET  /metrics
GET  /silver-records?instrument=NO2-CAPS&start=...&end=...
GET  /record-flags?instrument=NO2-CAPS
POST /record-flags?api_key=<review-api-key>
GET  /record-corrections?instrument=NO2-CAPS
POST /record-corrections?api_key=<review-api-key>
```

Write routes require the API Lambda environment variable `REVIEW_API_KEY`.
For the current review flow, open the dashboard with `?api_key=<review-api-key>`.
The dashboard then passes that key to the AWS write endpoints as a URL query
parameter.

## Common commands

```bash
python3 -m pip install -r requirements.txt   # install deps
python3 scripts/upload_instrument_data.py     # one upload pass
python3 scripts/deploy_aws.py                 # deploy/update AWS resources
python3 scripts/check_review_api.py --limit 5 # verify metrics + Silver review API
cd frontend && npm install && npm run dev     # run the dashboard locally
```

For Vercel, set `VITE_API_URL` to the API Gateway `/metrics` URL printed by
`scripts/deploy_aws.py`.

## Data layout (medallion)

Today the pipeline has Bronze and Silver. Bronze stores raw instrument batches.
Silver stores one deduplicated CSV and metadata file per instrument. Review
flags and corrections are saved as JSON sidecar records in S3, so we can mark
bad time ranges or selected rows without rewriting the raw Bronze files.

S3 layout:

```text
{instrument_id}/bronze/year=YYYY/month=MM/{stem}__batch_YYYYMMDDTHHMMSS.txt
{instrument_id}/silver/{instrument_id}_data.csv
{instrument_id}/silver/{instrument_id}_metadata.txt
{instrument_id}/flags/year=YYYY/month=MM/{flag_id}.json
{instrument_id}/corrections/year=YYYY/month=MM/{correction_id}.json
{instrument_id}/checkpoints/checkpoint.json
pipeline_status.json
```

Current layer status:

| Layer | Status | What to add on the AWS side |
|-------|--------|------------------------------|
| Bronze | done | raw files, partitioned by year/month (optionally add an S3 lifecycle rule) |
| Silver | done, CSV | daily Lambda rebuild that filters headers/comments, dedupes rows, and writes `{id}/silver/` |
| Review sidecars | deployed | flags and corrections stored as JSON under `{id}/flags/` and `{id}/corrections/` through authenticated write routes |
| Gold | partial | only SMPS hourly summary was seen in S3; decide the required aggregates before expanding |
| Query/catalog | optional | Athena and Glue Catalog can be added later if querying large historical data becomes important |
| Orchestration | partial | laptop scheduler for uploads, EventBridge daily schedule for Silver |

Delta Lake or Iceberg is not needed for the current review flow. S3 JSON
sidecars are simpler and cheaper for the present data size. Revisit Iceberg only
when many users need concurrent edits, versioned table history, or SQL updates
over large Silver/Gold datasets.

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
5. Open the API Gateway `/metrics` URL and confirm JSON contains `kpis`,
   `refreshTime`, and all five instruments with Bronze and Silver row counts.
6. Set Vercel `VITE_API_URL` to that `/metrics` URL and redeploy the frontend.
7. If enabling Data Review writes, set `REVIEW_API_KEY` on the API Lambda before
   deploying the new API routes.
8. Install the laptop scheduler only after a clean manual upload pass.

## Security notes

- **The `/metrics` endpoint is public and unauthenticated, and it returns
  month-to-date AWS account cost.** Anyone with the URL can read your billing
  number. Before this is widely shared, either drop `mtdCost` from the public
  payload or put the API behind auth (API key / Cognito / signed requests).
- Review write endpoints require `REVIEW_API_KEY`. The current dashboard passes
  this as `?api_key=...` in the URL for write actions. This is convenient for
  review testing, but it can appear in browser history and shared links, so move
  to proper user auth before broad access.
- Do not commit AWS credentials, Vercel tokens, sample data, checkpoint files,
  logs, SQLite buffers, or generated Lambda zips.
- The Lambda execution role attaches the broad managed policy
  `AmazonS3ReadOnlyAccess`; tighten to bucket-scoped least privilege when
  convenient.
- The API recounts every bronze file on each request. This is fine while the
  data is small. Once bronze grows large, have the uploader maintain a running
  count (written to a small file the API reads) instead of recounting live.

## Next steps

- Add an AWS Budget and project resource tags so billing can be separated.
- Address the public cost endpoint (see Security notes).
- Keep `REVIEW_API_KEY` configured in AWS Lambda.
- Add a small audit view for flags/corrections once the team decides the exact
  correction approval workflow.
- Decide Gold requirements after Silver review usage is clear.
