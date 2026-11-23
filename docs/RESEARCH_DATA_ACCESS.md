# Research data access

Full-history exports are available at the [Team Console](https://deohs-des-moines-air.vercel.app/team?view=research)
to existing, verified team accounts explicitly assigned `ResearchExporter`.
Ordinary API keys do not grant this permission. An existing key can receive an
explicit `research:export` scope after its verified owner has ResearchExporter
approval. The key is bound to that account, and both permissions are checked
on every request. The portal checks current group
membership and shows **Full-history access enabled** or **Full-history access
not enabled**. Administrators do not automatically receive export access.

The initial grant is for Yeon only. Granting membership does not create an
account or send a message. A welcome email, if sent separately, should link to
the portal and tell the recipient to use their existing sign-in and MFA. It
should not contain credentials or a permanent data link.

## Download in the portal

1. Sign in and open **Research data**.
2. Select the instrument and Full history, Last 7 days, or Custom dates.
3. Leave **Include flagged records** off to exclude actively flagged rows.
   Turn it on to preserve flagged rows together with their review annotations.
4. Select **Prepare CSV (.gz)**. Preparation runs independently of the page.
5. When the export is Ready, select **Download CSV**.

Exports contain indexed Silver observations at their recorded resolution,
including all measurement columns. They are not hourly/15-minute aggregates.
Malformed records or records without a usable timestamp are not represented by
the observation index. Untouched instrument-file archives require separate
access. Original measurement values are retained; correction annotations do
not rewrite those measurements.

Every CSV also includes:

| Column | Meaning |
|---|---|
| `_research_row_key` | Stable observation identity used by the review tools. |
| `_research_timestamp_utc` | Explicit UTC observation timestamp. |
| `_research_status` | Normal, flagged, or corrected status. |
| `_research_flags` | JSON array of applicable active flags, including reasons. |
| `_research_corrections` | JSON array of applicable correction annotations. |

The export metadata records the source version, reader version, review revision,
row counts, date bounds, file size and SHA-256 checksum. Identical requests reuse
prepared results. Source values and annotation versions are pinned at request
time. If review decisions change, a new download link requires a new export.

## Download using an approved API key (R/Python)

Yeon's selected existing key receives the additional `research:export` scope;
other keys, including future replacement keys, do not inherit it. No new secret
is created or emailed. Keep the existing key in the `AQ_API_KEY` environment
variable; do not put it in a script, command argument or URL.

```sh
python3 scripts/research/download_data.py --auth api-key --instrument NO2-CAPS
python3 scripts/research/download_data.py --auth api-key --instrument BC-MA200 --include-flagged
```

These commands use the key without opening a browser. Both flag settings preserve
original source values and annotation columns; the default excludes active flags.
For R, install `httr2` and `openssl` once, then:

```r
source("scripts/research/download_data.R")
path <- download_research_data("NO2-CAPS", include_flagged = TRUE)
data <- read.csv(gzfile(path), check.names = FALSE, colClasses = "character")
```

Both clients verify the downloaded file's SHA-256 checksum and send the key only
to the API, never to the signed storage URL. Character columns preserve exact
source strings; convert selected measurements to numeric for analysis as needed.
Key requests share existing per-key quotas and named-user usage tracking. There
is also a shared limit of ten preparations per approved account per hour across
portal sessions and keys. Keys cannot access another account's export jobs.

The keyed research API uses the same methods and suffixes as the team endpoints
below, under `/air-quality/v1/keyed/research`, with `x-api-key` instead of the team
Bearer token. Example: `POST /air-quality/v1/keyed/research/exports` with JSON
`{"instrument":"NO2-CAPS","include_flagged":false}`. Poll the returned job ID,
then request its `/download` suffix when Ready. Ordinary dashboard/keyed data
routes remain limited to 14 days even for a research-approved key.

## Download using team sign-in

The standard-library Python helper completes the existing team OAuth sign-in
with PKCE, including MFA, and keeps tokens in memory. It uses the existing
registered loopback callback on port 5173; stop a development server using that
port before running it. No public API key or AWS operator credentials are used.

```sh
python3 scripts/research/download_data.py --instrument NO2-CAPS
python3 scripts/research/download_data.py --instrument BC-MA200 --include-flagged
```

Date filters are optional; use explicit ISO timestamps with UTC offsets for
reproducible programmatic requests. The helper verifies the downloaded checksum
before replacing its output file. Existing files require `--overwrite` or a
different `--output` filename.

```r
data <- read.csv(gzfile("NO2-CAPS_research.csv.gz"), check.names = FALSE)
```

## Private API contract

Use the team sign-in ID token in the Authorization Bearer header. Only API
Gateway-verified team identities are accepted, and current membership is checked
before creating, listing, inspecting or downloading exports. Ordinary API keys,
unverified identities, revoked memberships and another member's job IDs cannot
authorize a download.

| Method and path | Response |
|---|---|
| `GET /air-quality/internal/v1/research/access` | Current access status for this account. |
| `POST /air-quality/internal/v1/research/exports` | `202`, with a job ID. JSON: `instrument`, `include_flagged` (boolean, default false), optional `start` and `end`. |
| `GET /air-quality/internal/v1/research/exports` | This account's latest export requests. |
| `GET /air-quality/internal/v1/research/exports/{job_id}` | This account's job status and prepared metadata. |
| `GET /air-quality/internal/v1/research/exports/{job_id}/download` | A download URL only when authorized and Ready. |

Up to ten export preparations per account per hour are allowed. Preparing a
snapshot never happens inside the API request: a dedicated worker streams the
immutable chronological chunks into a gzip CSV. A private bucket, a separate
service role and an explicit bucket policy keep the public serving role out of
research export objects. Requests and generated files expire after one day;
newly issued download URLs expire after five minutes. Anyone holding a signed
URL can use it until expiry, so do not publish or forward it. Files already
downloaded retain their original snapshot content.

## Grant or revoke access

The operator script defaults to a read-only plan. It requires the expected AWS
account, an existing verified account and a durable private audit record before
applying a membership change. Access-change audit records are retained for one
year, separately from the one-day generated exports. It never sends an email or creates a new login. Use
`scripts/aws/manage_research_access.py --help` for the explicit grant/revoke
options. Revocation stops new download links immediately; an already issued
five-minute link retains its original expiry.


## Grant or revoke a key's research scope

`scripts/aws/manage_research_key.py` defaults to a read-only plan. It invokes the
separate IAM-only operator service, which has no HTTP routes. Select exactly one
existing active key belonging to the requested verified account. Granting also
requires current ResearchExporter membership; revoking a scope does not require
continued membership. Both changes are recorded in the private access audit.

```sh
python3 scripts/aws/manage_research_key.py --aws-creds-file /private/operator-creds.json --expected-account 213598695875 --email yscheong@uw.edu --actor-email pavands@uw.edu
```

Add `--key-id <existing-id> --apply` to grant that key. Add `--revoke` to remove
only its research permission while retaining ordinary public read access. Key
revocation, scope removal, account disablement, loss of verified identity, and
ResearchExporter removal all block new export requests and new download links.
Already issued five-minute links keep their original expiry.
