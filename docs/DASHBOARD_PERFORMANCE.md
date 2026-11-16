# Dashboard refresh and display precision

The Conditions cards read individual Silver observations. The builder prepares
recent rows and default hourly sample means using the API's own readers.
Non-default queries and cache misses can still compute results on request.
Loading a page does not trigger a Silver rebuild or change instrument readings.

## Loading behavior

Cards resolve independently. A slow instrument does not hide readings already
returned for another instrument. Refresh keeps a previous reading and its real
timestamp visible until the replacement arrives. A failed replacement marks that
reading unavailable rather than pretending it is new. Request identities and
cancellation prevent an older refresh replacing a newer one.

## API work and response reuse

Previously, even a `limit=1` request downloaded the full consolidated CSV, parsed
every timestamp and sorted all valid records. The reader now resolves column
indexes once and keeps a bounded chronological selection for small pages. It
still examines every timestamp, including out-of-order records and ties. SMPS
slash dates keep their established day-first parsing and legacy fallback.

The API caches small response bodies, with these boundaries:

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
review changes or non-default queries can still require an accurate source
scan. Scientific transforms and aggregation arithmetic remain unchanged.

Refresh indicators cover the readings, upload widget and applied chart query.
Conditions polls every five minutes while visible, pauses in hidden tabs, and
checks again on return. Observations refresh fetches only its active query.
Previously loaded rows remain visible while that query refreshes. Date-filtered
and subsequent pages still run the exact Silver query.

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
