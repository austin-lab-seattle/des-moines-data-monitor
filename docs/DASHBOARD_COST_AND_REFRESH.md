# Dashboard cost and refresh changes

## What happened

The October 28, 2026 investigation found repeated historical reads and long
API compute time. Account billing aggregates have different reporting cutoffs
from live function metrics and do not identify individual endpoints. Private
billing amounts and account identifiers are kept outside this public guide.

Two code paths repeatedly read history:

- The full summary endpoint downloaded every Bronze object to recount rows.
  Its 30-second memory cache applied only to a particular Lambda runtime. With
  approximately 9,971 current Bronze objects, a cache miss could download almost
  10,000 objects. Current Conditions pages already use separate latest-reading
  and upload-widget endpoints, so this is not evidence that every page view
  triggered a full recount.
- The scheduled Silver builder downloaded every Bronze batch on every run,
  including batches that had not changed.

Historical logs do not establish exactly how many of the billed reads came
from each path. The missing-BC warning added with the October 28 speed release
checks for the source Silver CSV. It is not proof of millions of cache misses
and cannot explain earlier monthly usage. BC currently has no Bronze objects.

S3 GET pricing is $0.0004 per 1,000 requests, or $0.40 per million, not $0.40
per thousand. See https://aws.amazon.com/s3/pricing/.

## Before and after

| Work | Before | After |
| --- | --- | --- |
| Full summary | Download and recount Bronze history | Read five small processing snapshots |
| Missing summary snapshot | Recount history | List metadata and return an unknown row count |
| Unchanged instrument | Download and process all old batches | Compare source manifest and Silver version, then skip |
| Changed instrument | Download all original batches | Reuse verified raw text in daily compressed bundles and download only new or changed batches |
| Default charts and recent rows | First visitor computes from Silver | Builder prepares the exact API responses |
| Upload widget | Each runtime independently lists history | Share a response for up to 60 seconds |
| Missing Silver CSV | HEAD, fallback GET and warning every request | Return 404 normally, with a 60-second memory cache |
| Browser polling | Every minute, including background tabs | Every five minutes, paused while hidden, with one refresh on return |
| Manual Refresh | Updated cards and upload widget | Also refreshes the currently applied chart query |
| Refresh feedback | Primarily the manual button | Button, pending cards, upload widget and chart indicators |

## What happens when a new record arrives

1. The field uploader continues writing the original batch to Bronze. Its
   checkpoints and SQLite retry queue do not change in this release.
2. The existing Silver schedule lists source object metadata. A manifest of
   keys, ETags, sizes and modification times identifies changes.
3. If the manifest and processing code match the previous successful build,
   and the Silver object still has the recorded ETag, the builder skips work.
4. For changed inputs, it loads compressed daily bundles from the private
   `_processing_cache/v1/{instrument}/` prefix. Cached entries must match the
   source metadata and their content digest. New or changed entries come from
   Bronze using conditional reads. Missing or invalid bundles fall back to the
   real source, never substitute values.
5. The original consolidation, deduplication and scientific transformations
   run on the complete ordered source text. This is incremental source reading,
   not incremental arithmetic. Late records, changed source objects and deleted
   objects remain subject to the same full consolidation.
6. The builder writes Silver, prepares recent one/eight-row views and the
   default hourly series using the API's own readers, then saves the processing
   snapshot under `_pipeline_state/v1/{instrument}.json`.
7. API requests validate the current Silver ETag. Recent-record views also
   validate the review fingerprint. A mismatch falls back to accurate parsing.
   Publication failures leave that fallback available.
8. A visible Conditions page refreshes every five minutes. The header spinner
   stays active until the cards, upload widget and chart requests finish.
   Existing values retain their original measurement timestamps during refresh.

The first build populates the raw bundles and therefore still reads existing
Bronze history once. Later changed builds read daily bundles plus new or changed
sources. Unchanged builds read no Bronze bodies. Raw bundles duplicate some data
in compressed form and add a small amount of private storage and PUT activity.

## Accuracy and freshness boundaries

- No calibration constants, scientific formulas, time zone rules or unit
  conversions change. NO2 and SMPS cards retain whole-number display rounding.
  Recent tables and exports keep source precision.
- The builder packages the same `lambda_api.py` as the API Lambda. No separate
  chart aggregation implementation is introduced. Published responses include
  the same chronological selection, row keys, sample means and sample counts.
- Input metadata changes or code changes invalidate the processing manifest.
  Corrupt bundles and conditional-read failures cannot advance a successful
  processing snapshot.
- Summary row counts represent the last successful processing snapshot. Each
  instrument includes `snapshotGeneratedAt`. A missing snapshot produces an
  unknown count rather than an invented zero.
- Silver continues to follow its existing 15-minute schedule. A browser can
  take up to another five minutes to poll after Silver changes. Manual Refresh
  fetches immediately but does not force acquisition or a Silver rebuild.
- The latest-upload widget response can lag up to 60 seconds. A previously
  missing instrument can take up to 60 seconds to clear a warm negative cache.
- Arbitrary filters, non-default measurements and exports may still parse
  Silver on demand. The default cached chart is not a replacement for every
  possible research query.
- This does not add a global processing lock or an exactly-once transaction
  across S3 objects. Source files, Silver and processing snapshots remain
  separate writes. Version checks prevent mismatched prepared responses.

## Validation and deployment

Automated tests compare bundle-backed results against the original full replay
for additions, edits, deletions and late records. They exercise corrupt or
missing bundles, interrupted reads, code invalidation, review changes, source
versions, exact precomputed responses and summary requests that never read
Bronze bodies. Browser tests check refresh indicators, retained values, chart
refresh and the five-minute/visibility behavior.

`scripts/aws/deploy_dashboard_performance.py` is a dry-run-by-default, code-only
release utility. It requires explicit credentials, account identity and a
reviewed Git baseline, validates both live source packages before updating
either, and saves private rollback ZIPs. It preserves environment variables,
IAM roles, memory, timeouts, routes and schedules. The builder ZIP includes the
same API reader. The normal backend deployment script also packages that reader
for future deployments. The field laptop needs no update for these changes.

## How to verify savings

The API emits a `dashboard_request` JSON event with a known route, status,
duration and cache mode. It excludes query strings, keys and user identities.
Silver build results report `bronze_reads` and `skipped_unchanged`. Default view
responses expose `X-Data-Cache` as `memory`, `shared`, `computed`,
`missing-source` or `missing-cached`.

Compare CloudWatch invocation/duration metrics and daily Cost Explorer S3 Tier
1/Tier 2 and Lambda compute usage over equivalent periods. Billing data arrives
later than live metrics. Existing charges remain on the bill. No percentage
saving or monthly spending cap is guaranteed by this release, and it introduces
no additional managed cache service or paid S3 request-metrics configuration.
