# Dashboard refresh and display precision

The Conditions cards read individual Silver observations. The builder prepares
recent rows and default hourly sample means using the API's own readers.
Custom chart queries and chart-cache misses can still compute results on request.
Observation pages use a chronological chunk index prepared by the builder.
Loading a page does not trigger a Silver rebuild or change instrument readings.

## Loading behavior

Cards resolve independently. A slow instrument does not hide readings already
returned for another instrument. Refresh keeps a previous reading and its real
timestamp visible until the replacement arrives. A failed replacement marks that
reading unavailable rather than pretending it is new. Request identities and
cancellation prevent an older refresh replacing a newer one.

## API work and response reuse

Observation requests check the Silver ETag and read a compact chronological index.
The index identifies immutable, compressed chunks of at most 1,024 rows and about
512 KiB before compression. Page reads skip complete chunks using counts; date
filters skip chunks outside the requested interval. Only boundary chunks need
row-level filtering. Row identities, source offsets, timestamp rules and original
numeric strings match the direct Silver reader. Counting a complete chunk uses
metadata alone. Flags and corrections are joined from their current objects.

The builder publishes the index manifest after all chunks succeed. A missing,
outdated or damaged index returns a retryable 503; a page request never downloads
Silver to rebuild it. The next scheduled build repairs failed publications. This
avoids the former 9–18 second pagination/filter scans observed in live logs.
Authorization and quotas still run before any index access. Old unreferenced
content-addressed chunks are not read; cache storage cleanup can reclaim those
objects separately, preserving all chunks referenced by the current manifest.

Publication lists chunk metadata once and reuses blobs whose size and S3
checksum match. Only new or changed chunks are uploaded. Manifest paths include
the reader version, so preparing a code deployment cannot replace the manifest
used by the running API. Gzip headers are normalized across Python runtimes to
keep unchanged chunk identities stable between local preparation and Lambda.

The chart API also caches small response bodies, with these boundaries:

- Every reuse requires the current Silver object's S3 ETag. Conditional reads
  associate newly computed results with the version that was checked.
- Recent-record results also require the current flags/corrections fingerprint.
- Query parameters and an automatic hash of the API implementation form part of
  the identity. New code or source/review changes invalidate earlier results.
- The warm-runtime cache holds at most 16 bodies, each at most 512 KiB.
- The one/eight-row recent views, the first 100 Observations rows, and the
  default chart reuse private S3 objects across Lambda runtimes. The
  Observations page can use up to 2 MiB of shared storage per instrument,
  without increasing the warm-runtime memory limit.
- Cache errors fall back to the source handler. Failed source reads never turn
  an older cached response into a fresh successful reading. Failed responses and
  oversized results are not cached. Authentication and quotas run before reuse.

The scheduled Silver build now publishes these versioned views, moving normal
default-view computation off the visitor's request path. Publication failures,
custom chart queries can still require an accurate source scan. Scientific
transforms and aggregation arithmetic remain unchanged.

Refresh indicators cover the readings, upload widget and applied chart query.
Conditions polls every five minutes while visible, pauses in hidden tabs, and
checks again on return. Observations refresh fetches only its active query.
Previously loaded rows remain visible while that query refreshes. Date-filtered
and subsequent observation pages read the index rather than the Silver CSV.

Record review reads a versioned snapshot of 50 Silver row IDs, timestamps and
primary measurements, then applies current flags and corrections. Tim can use
it to flag anomalies in the latest samples. New observation CSVs exclude rows
covered by active flags. Flagging leaves source data intact and the review
screen can use the same snapshot after an annotation changes.
The API reads Silver directly when the snapshot is missing or outdated.

Conditions opens the chart on the last 14 days and uses hourly sample averages.
Zooming to four days or less requests 15-minute sample averages. The builder
prepares both resolutions from the same Silver samples in one pass for the
materialized measurements. A custom date range cutting through a bucket uses
the exact Silver query. Chart CSV reflects the displayed aggregates.

See [Dashboard cost and refresh changes](DASHBOARD_COST_AND_REFRESH.md)
for the processing manifest, raw source bundles and freshness limits.

## Display precision

NO2-CAPS and SMPS latest-reading cards round to the nearest whole number. Other
cards retain at most two decimal places. Recent observation tables, stored data,
chart arithmetic and CSV downloads retain their existing precision. Missing or
invalid readings remain unavailable. Display rounding does not clip negatives.

## Validation

CI checks chronological pagination, means/sample counts, timestamp semantics,
source/review/code invalidation, cold-runtime shared reuse, failed cache/source
handling, authorization before cache access, and bounded cache size. Browser
checks hold one instrument response pending while asserting that other cards
already display their values, and check that rounded cards retain precise rows
in the recent-values table.
