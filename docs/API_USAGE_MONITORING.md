# Researcher API usage monitoring

The Team Console API users page shows each researcher's last known use,
today's calls, recorded lifetime calls, exports prepared and CSV bytes prepared.
It combines active and revoked keys belonging to the same normalized email.
The page also shows daily history for the last 7, 30 or 90 days and retains the
existing key-revocation controls.

## What the numbers mean

- Calls are requests authenticated with a personal API key and admitted by the
  existing quota checks. Both successful responses and endpoint errors count.
  Invalid credentials and rate-limited calls are excluded from these totals.
- Anonymous public dashboard requests cannot be attributed to a named person
  and do not create per-user usage records. Operational request counts remain
  available separately in CloudWatch.
- Usage days and displayed last-used times use America/Los_Angeles. Existing
  per-key quota counters still use UTC, and the interface labels that difference.
- Exports prepared count successful CSV-link preparations. Rows and bytes refer
  to those generated CSV files. They are not proof that someone completed a
  download, and repeated downloads of a presigned URL are not counted here.
- Recorded lifetime covers this tracker's observations. Previously, minute
  counters expired after two minutes and daily quota counters after roughly two
  days. Those expired counters cannot reconstruct true historical lifetime use.
  Earlier history remains unknown. The interface displays tracking start times,
  marks the first tracked day as partial and labels earlier days Not recorded.
- Revoking or rotating a key does not reset the user's durable totals. Historical
  metadata follows the normalized email, without storing raw API keys in usage
  records. Legacy keys without an email remain separate key-based subjects.

## Correctness

Lifetime and daily rollups do not expire. A separate request-deduplication marker
is retained for seven days. One atomic transaction commits the user and key
rollups together with that marker. Repeated processing of the same gateway
request cannot count it twice within the marker retention window. A new HTTP
request, including a client retry with a new gateway request ID, is a separate use.

Optimistic versions protect concurrent counter updates. The maximum last-used
timestamp and earliest tracking start survive requests completing out of order.
Counters include total requests, successful responses, errors, exports prepared,
export rows and export bytes. The same private route reads aggregates and daily
history. It retains existing team-role authorization, and key listings paginate
the status indexes instead of silently stopping at the first page.

Authenticated researcher responses require successful accounting. If the
accounting transaction cannot complete after bounded conflict retries, the API
returns 503 with Retry-After rather than serving an unrecorded data response.
The anonymous dashboard does not depend on these accounting writes. No raw key,
token, query string or user identity is added to request logs.

The helper is packaged with both the API and the builder because the builder
uses the same API reader to prepare dashboard views. Only real researcher
requests call the accounting helper. Preparing cached dashboard views does not
increment personal usage. Existing storage permissions suffice. Tracking adds storage reads and
transactional writes for named requests. It does not require a new managed
service or an accounting write for every anonymous dashboard visitor.

## CSV export size

Chart CSV contains the hourly means currently plotted and two columns.
Export CSV contains individual Silver observations and all instrument columns,
with the existing date filters, default fourteen-day window and export limits.
It preserves values and precision.

The live SMPS schema inspected on October 29, 2026 had 146 columns and averaged
about 1,149 bytes per observation. A few thousand wide rows can legitimately
occupy several megabytes. The export response now includes its column count,
and the dashboard reports the actual row count, column count and byte size after
preparation. No precision reduction or change to the exported measurements is
used to make a file smaller.

## Refresh failures and old readings

One transient network/server failure on observation, chart or summary reads
gets one bounded retry. Authentication errors, quota errors, submissions and
export preparation are never automatically retried. Caller cancellation
prevents an obsolete request from retrying.

Older reading means the observation is more than one hour old. A refresh failure
has a separate message and retains the previous observation and timestamp.
There is no longer an orange Last available label that conflates the two.
When a file has a timestamp but no mapped concentration, the card identifies
the timestamp as a record time. No black-carbon wavelength or unit is inferred
from a newly encountered MA200 export format.
