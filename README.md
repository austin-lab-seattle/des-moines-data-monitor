# Des Moines Data Monitor

Air quality data pipeline and monitoring dashboard for the DEOHS research project.
The field laptop uploads instrument data to S3 Bronze, AWS builds a deduplicated
Silver layer, an API Lambda serves public read endpoints, and the Vercel
React dashboard reads it through API Gateway.

Live dashboard: <https://deohs-des-moines-air.vercel.app>

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
Serial instruments -> configured files  S3 bucket                              React dashboard
scripts/field/acquire_serial.py                |                                      |
Instrument files (data_glob)                   |                                      |
     |                                  des-moines-data-pipeline-austinlab           |
scripts/field/upload_to_aws.py     -->  {instrument}/bronze/...                      |
     |                                       |                                        |
per-file checkpoints + SQLite buffer    aq-silver-builder Lambda                     |
                                             |                                        |
                                        {instrument}/silver/...                      |
                                             |                                        |
                                        aq-dashboard-api Lambda                      |
                                        /air-quality/v1/...                          |
                                             |                                        |
                                        API Gateway  -------------------------------+
```

## Field-laptop layout

Runtime data and credentials stay outside the Git checkout:

```text
C:\des_moines\
├── aws_creds.json
├── data\                       # canonical raw instrument files
├── runtime\                    # logs, checkpoints, buffer and serial receipts
└── des-moines-data-monitor\    # Git checkout; code and local config only
```

## Repository layout

```text
.
├── lambda_api.py               # dashboard API Lambda: public API + summary payload
├── lambda/
│   └── silver_builder.py       # rebuilds deduplicated silver CSVs from bronze
├── config/
│   ├── instruments.json             # one local config for acquisition + upload (gitignored)
│   └── instruments.example.json     # tracked template
├── requirements.txt
├── scripts/
│   ├── field/                        # acquisition, upload and laptop scheduling
│   │   ├── acquire_serial.py         # continuous PuTTY replacement
│   │   ├── upload_to_aws.py          # incremental Bronze uploader
│   │   ├── copy_to_shared_drive.py   # stable, non-destructive OneDrive snapshots
│   │   └── windows/install_tasks.ps1 # installs the three Windows field tasks
│   ├── aws/                          # deploy and administrative commands
│   └── quality/                      # read-only audit and smoke checks
└── frontend/                   # Vite React dashboard (deployed via Vercel)
```

## Components

- `scripts/field/upload_to_aws.py` reads all active instruments from
  `config/instruments.json`, discovers source files with a **glob pattern**
  (`data_glob`), keeps a **byte offset per file**, buffers upload attempts in
  SQLite, and writes bronze batches to S3. Run it from the repository root.
- `scripts/field/acquire_serial.py` continuously owns the enabled serial ports.
  NO2 and the nephelometer are enabled; LI-COR detection/support is present but
  disabled until its acquisition method is confirmed. It writes only a receipt timestamp and the exact raw
  instrument line; the uploader sends this acquisition envelope to Bronze
  unchanged. Silver owns parsing and scientific transformations. Its
  `--detect-ports` mode can passively identify the three wire formats, and
  `--apply-detected-ports` saves only a complete, unambiguous mapping.
- `lambda/silver_builder.py` rebuilds one Silver CSV per instrument from Bronze,
  keeps only real data rows, removes duplicates, derives Duwamish PM2.5 from
  corrected BScat, normalizes the SMPS Total Concentration header, and writes a
  metadata sidecar with the unique row count.
- `lambda_api.py` serves the dashboard JSON payload through API Gateway at
  `/air-quality/v1/summary`. On every request it scans the bronze prefix and counts the real
  data rows and bytes per instrument live (its `is_data_row()` logic skips
  headers and comment lines), reads Silver row counts from metadata, and exposes
  public read-only API routes for the dashboard and cleaned data access.
- `scripts/aws/deploy_backend.py` creates or updates the bucket, Lambda role, the API
  Lambda, and the API Gateway.
- `frontend/` is the Vite React dashboard deployed through the existing Vercel
  project.
- `docs/PUBLIC_API_ACCESS.md` is the public API access guide for the read-only
  endpoints that are safe to share externally.
- `PROJECT_HANDOVER.md` is the AI-agnostic source of truth for future
  collaborators.

## File discovery and checkpoints

Instrument filenames encode a date range, for example
`2026Feb12-25_CO2-46_Duwamish.txt`. When the instrument rolls over to a new file
(`2026Apr12-28_CO2-46_Duwamish.txt`) the uploader picks it up automatically
because each instrument is configured with a glob, not a single path:

```json
{ "id": "CO2-LICOR", "ingestion_type": "growing_file",
  "data_glob": "C:/des_moines/data/co2_li_cor/*CO2-*.txt", "active": true }
```

The uploader then:

- globs all matching files each run (a single string or a list of patterns);
- tracks a byte offset **per file** in
  `C:\des_moines\runtime\checkpoints\{instrument}.json`
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

There are three field-laptop tasks plus one cloud schedule:

- The serial logger runs continuously on the field laptop and replaces PuTTY
  for the enabled serial instruments (currently NO2 and the nephelometer).
- The laptop upload job runs on the field laptop because it reads local
  instrument files and uploads new bytes to S3 Bronze.
- The shared-drive copy job snapshots the same canonical local data tree into
  the UW OneDrive folder without deleting destination files.
- The cloud Silver builder runs every 15 minutes in EventBridge and rebuilds the
  deduplicated Silver CSVs from Bronze.

Run one upload pass manually:

```bash
python scripts/field/upload_to_aws.py --check  # validates without uploading
python scripts/field/upload_to_aws.py
```

Install all three Windows tasks (serial logging, AWS upload, and shared copy):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/field/windows/install_tasks.ps1 -AwsCredsFile "C:\des_moines\aws_creds.json" -UploadEveryMinutes 15 -RunWhenLoggedOff -RunAsUser "$env:COMPUTERNAME\lab_admin" -RunNow
```

Install a macOS launchd job that runs every 900 seconds:

```bash
bash scripts/field/macos/install_upload_schedule.sh 900
```

Use a 15-minute upload interval while instruments are actively writing. A 30- or
60-minute interval is fine when near-real-time visibility is not needed.

For a new Windows laptop, follow the complete checklist in
[`docs/FIELD_LAPTOP_SETUP.md`](docs/FIELD_LAPTOP_SETUP.md) before enabling the
scheduled task.

## AWS credentials

On the field laptop, the task installer passes
`C:\des_moines\aws_creds.json` directly to the uploader. An explicitly supplied
`--aws-creds-file` is authoritative, so Task Scheduler does not depend on
whether it inherited a newly configured environment variable.

Other environments may use the standard boto3 credential chain:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-west-2
# or
aws configure --profile des-moines    # then export AWS_PROFILE=des-moines
```

All `aws_creds*.json` files are gitignored. Rotate the field IAM key periodically
and keep it scoped to least privilege (S3 write to the data bucket only).

## Local config

The sensitive/local files are gitignored, but the field installer also keeps
them outside the checkout: credentials and canonical raw data live directly
under `C:\des_moines`, while logs, checkpoints and the SQLite retry buffer live
under `C:\des_moines\runtime`. Copy `config/instruments.example.json` to
`config/instruments.json` and set the COM ports before installation.

Current AWS target:

```text
Region: us-west-2
Bucket: des-moines-data-pipeline-austinlab
API: https://yvhb48sthk.execute-api.us-west-2.amazonaws.com/air-quality/v1/summary
API base: https://yvhb48sthk.execute-api.us-west-2.amazonaws.com
```

Public dashboard routes remain anonymous and read-only. Researcher API routes
use the `/air-quality/v1/keyed/...` prefix and require a personal key:

```text
GET  /air-quality/v1/keyed/summary
GET  /air-quality/v1/keyed/observations?instrument=NO2-CAPS&start=...&end=...
GET  /air-quality/v1/keyed/timeseries?instrument=SMPS&measurement=...
GET  /air-quality/v1/keyed/observations/export?instrument=SMPS&start=...&end=...
POST /air-quality/v1/access-requests
```

When API key registration is enabled, users request access, verify their email,
and automatically receive a personal read-only key at that same address. Keys
are rate-limited and are stored only as one-way keyed hashes.

Observation exports default to the latest 14 days, are capped at a 31-day
window and 250,000 rows, and are generated as temporary private S3 objects.
Their signed URLs expire after five minutes and lifecycle cleanup removes the
objects after one day.

Review/admin routes are separate internal endpoints protected by Cognito JWTs
and named team roles. The public Vercel views remain read-only.

## Common commands

```bash
python3 -m pip install -r requirements.txt   # install deps
python scripts/field/upload_to_aws.py                 # one upload pass
python scripts/aws/deploy_backend.py                  # deploy/update AWS resources
python scripts/quality/check_public_api.py --limit 5  # verify public API reads
cd frontend && npm install && npm run dev     # run the dashboard locally
```

To enable access requests after SES has a verified sender address:

```bash
export ACCESS_REQUEST_FROM_EMAIL="elaustin@uw.edu"
export API_KEY_HASH_PEPPER="set-this-from-a-secret-manager"
ENABLE_API_KEY_REGISTRATION=1 python scripts/aws/deploy_backend.py
```

After the user opens the verification link, SES emails the key to that verified
address. If delivery fails, the new key is revoked. The Lambda execution role
needs permission to send from `ACCESS_REQUEST_FROM_EMAIL`.

Keep `PUBLIC_API_KEY_REQUIRED=0` until the public dashboard has a server-side
data proxy. A static browser dashboard cannot keep a shared key secret.

For Vercel, set `VITE_API_URL` to the API Gateway summary URL printed by
`scripts/aws/deploy_backend.py`.

For an internal dashboard deployment that should show AWS MTD cost in Overview:

```bash
ENABLE_COST_KPI=1 python scripts/aws/deploy_backend.py
```

## Data layout (medallion)

Today the pipeline has Bronze and Silver. Bronze stores raw instrument batches.
Silver stores one cleaned, deduplicated CSV and metadata file per instrument.
For NEPH-PM25 it preserves raw scattering, adds `BScat = raw / 100`, and adds
`PM2.5 = (28.6 * BScat) + 2.6`; PM2.5 is the dashboard default. Review
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
| Silver | done, CSV | 15-minute Lambda rebuild that filters headers/comments, dedupes rows, applies documented transforms, and writes `{id}/silver/` |
| Review sidecars | deployed privately | Cognito-protected flags/corrections stored separately from immutable Bronze and Silver, with audit entries |
| Gold | partial | only SMPS hourly summary was seen in S3; decide the required aggregates before expanding |
| Query/catalog | optional | Athena and Glue Catalog can be added later if querying large historical data becomes important |
| Orchestration | partial | laptop scheduler for uploads, EventBridge daily schedule for Silver |

Delta Lake or Iceberg is not needed for the current review flow. S3 JSON
sidecars are simpler and cheaper for the present data size. Revisit Iceberg only
when many users need concurrent edits, versioned table history, or SQL updates
over large Silver/Gold datasets.

## Operational walkthrough

1. Confirm `config/instruments.json` `data_glob` patterns match the live files.
2. Run `python scripts/field/upload_to_aws.py` once and check `collector.log`.
3. Confirm S3 has `{instrument_id}/bronze/...` files and
   `{instrument_id}/checkpoints/checkpoint.json`.
4. Run `python scripts/aws/deploy_backend.py` after Lambda/API changes.
5. Open the API Gateway summary URL and confirm JSON contains `kpis`,
   `refreshTime`, and all five instruments with Bronze and Silver row counts.
6. Set Vercel `VITE_API_URL` to that summary URL and redeploy the frontend.
7. Keep public pages read-only; use the Cognito-protected Team Console for
   review and access administration.
8. Install the laptop scheduler only after a clean manual upload pass.

## Security notes

- Public API and dashboard routes must stay read-only.
- Do not pass private access tokens in URLs. URLs end up in browser history,
  logs, screenshots, and shared messages.
- Record modification workflows must be private, explicitly enabled, and reviewed
  before deployment.
- Do not commit AWS credentials, Vercel tokens, sample data, checkpoint files,
  logs, SQLite buffers, or generated Lambda zips.
- The Lambda execution role attaches the broad managed policy
  `AmazonS3ReadOnlyAccess`; tighten to bucket-scoped least privilege when
  convenient.
- The API recounts every bronze file on each request. This is fine while the
  data is small. Once bronze grows large, have the uploader maintain a running
  count (written to a small file the API reads) instead of recounting live.

## Next steps

- Add an AWS Budget and project resource tags so cost tracking stays in AWS
  and internal dashboard cost visibility stays deliberate. The Overview tile can
  show MTD cost when the API Lambda is deployed with `ENABLE_COST_KPI=1`.
- Keep review/admin workflows inside the Cognito-protected Team Console.
- Review Team Console roles and audit records periodically.
- Decide Gold requirements after Silver review usage is clear.
