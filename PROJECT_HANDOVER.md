# Project Handover

Current production and operating notes for the DEOHS Des Moines air monitor.

## Live services

- Dashboard: <https://deohs-des-moines-air.vercel.app>
- API base: <https://yvhb48sthk.execute-api.us-west-2.amazonaws.com>
- GitHub: <https://github.com/austin-lab-seattle/des-moines-data-monitor>
- AWS region: `us-west-2`
- S3 bucket: `des-moines-data-pipeline-austinlab`

The previous `project-kv69p.vercel.app` address remains temporarily available
for old bookmarks. New documentation should use only the dashboard URL above.

## Data flow

```text
Field laptop -> S3 Bronze -> Silver builder Lambda -> S3 Silver
S3 Bronze/Silver -> API Lambda -> API Gateway -> Vercel dashboard
```

- The Windows field laptop runs `scripts/upload_instrument_data.py` every
  15 minutes.
- Uploads are incremental by source filename and byte offset. Local checkpoints
  are mirrored to S3.
- Failed uploads remain in `sensor_buffer.db` and retry on the next run.
- The daily Silver builder filters and deduplicates Bronze data.
- The public dashboard reads the anonymous, read-only routes.
- Researcher scripts use individually issued API keys on keyed routes.
- The private Team Console uses Cognito accounts, MFA, role claims, and audit
  records for administrative actions.

## Field laptop

Follow [`docs/FIELD_LAPTOP_SETUP.md`](docs/FIELD_LAPTOP_SETUP.md). Do not install
the recurring task until:

1. every `data_glob` matches only the intended instrument files;
2. `python scripts/upload_instrument_data.py --check` passes;
3. one manual upload exits with code `0`;
4. `collector.log` contains no upload errors; and
5. the live dashboard shows the expected new timestamp and counts.

The task name is `DesMoinesDataMonitorUpload`. Its normal interval is 15
minutes. Do not run two uploader instances at the same time.

## Public and researcher API

Anonymous dashboard routes:

- `GET /air-quality/v1/summary`
- `GET /air-quality/v1/timeseries`
- `GET /air-quality/v1/observations`
- `GET /air-quality/v1/observations/export`

Researcher routes use the corresponding `/air-quality/v1/keyed/...` paths and
require `x-api-key`.

Current self-service access flow:

1. the researcher submits the Developers-page form;
2. SES sends a 30-minute email-verification link;
3. opening the valid link creates one read-only key and emails it to that same
   address; and
4. only a one-way keyed hash is stored by the access service.

Limits are 30 requests per minute, 5,000 requests per day, and 20 exports per
day per key. Missing keys return `401`; invalid or revoked keys return `403`.
Keys never authorize record review or modification.

## Team Console

- Entry point: <https://deohs-des-moines-air.vercel.app/team>
- Authentication: Amazon Cognito hosted sign-in with MFA.
- Roles: `Admin`, `Reviewer`, `AccessManager`, and `Viewer`.
- Review annotations are separate from immutable Bronze/Silver source data.
- Internal routes require API Gateway-verified Cognito JWT claims.
- Flag and correction writes fail closed if the audit log is unavailable.

UW NetID federation was intentionally deferred. Do not weaken the Cognito
boundary or restore URL-based shared review credentials.

## Production configuration

- Vercel project: `deohs-des-moines-air`
- Vercel root directory: `frontend`
- Vercel production branch: `main`
- Cognito callback/logout URL: `https://deohs-des-moines-air.vercel.app/team`
- API Gateway browser origin: `https://deohs-des-moines-air.vercel.app`

The old Vercel origin is temporarily retained in AWS callback and CORS lists as
a rollback path. Remove it only after the team confirms old bookmarks no longer
matter.

## Security rules

- Never commit `aws_creds.json`, `instruments_config.json`, `.env` files,
  checkpoints, field data, SQLite buffers, API keys, or generated logs.
- Never put an API key or authentication token in a URL.
- Keep the public dashboard and anonymous API routes read-only.
- Use a dedicated least-privilege IAM identity for the field laptop.
- Preserve S3 public-access blocking.
- Rotate a field credential immediately if it is copied into chat, email, a
  ticket, or source control.

## Verification before deployment

```bash
python3 -m unittest discover -s tests -v
python3 scripts/audit_public_exposure.py
python3 -m py_compile lambda_api.py scripts/*.py
cd frontend && npm run lint && npm run build
```

Production smoke checks should confirm:

- dashboard, chart, and all five instruments load;
- Observation Explorer loads recent records;
- missing researcher keys return `401` and invalid keys return `403`;
- internal endpoints return `401` without a team JWT;
- the clean production origin receives the expected CORS header; and
- Team Console sign-in redirects to Cognito with the clean callback URL.

## Known operational cautions

- The current source data can be stale when the field laptop is offline. The
  dashboard reports the last completed upload rather than claiming the
  instruments are live.
- The first run for a new source filename uploads the complete lines in that
  file. Inspect globs before the first real upload.
- File identity is the basename. Replacing a file with different contents but
  the same name can confuse an existing checkpoint.
- Do not delete `checkpoints/` or `sensor_buffer.db` during routine cleanup.
