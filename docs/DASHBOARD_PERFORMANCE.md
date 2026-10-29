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
- The one/eight-row recent views and default chart can also reuse private S3
  objects across Lambda runtimes. Fixed names under
  `_dashboard_cache/v1/{instrument}/` bound this shared cache to three objects
  per instrument. No additional IAM grants or retention rules are required.
- Cache errors fall back to the source handler. Failed source reads never turn
  an older cached response into a fresh successful reading. Failed responses and
  oversized results are not cached. Authentication and quotas run before reuse.

The scheduled Silver build now publishes these versioned views, moving normal
default-view computation off the visitor's request path. Publication failures,
review changes or non-default queries can still require an accurate source
scan. Scientific transforms and aggregation arithmetic remain unchanged.

Refresh indicators cover the readings, upload widget and applied chart query.
Polling runs every five minutes while visible, pauses in hidden tabs and runs
once on return. Previous values and the same-query chart stay visible during
refresh. See [Dashboard cost and refresh changes](DASHBOARD_COST_AND_REFRESH.md)
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
