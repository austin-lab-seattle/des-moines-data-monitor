# Instrument freshness alerts

`aq-upload-freshness-monitor` checks each of the five instruments hourly using
the existing `aq-upload-freshness-hourly` EventBridge schedule. The default
threshold is six hours, so an alert normally arrives between six and seven hours
after the last upload or observation.

Two independent checks are made for every instrument:

- **No upload:** the newest nonempty Bronze object is at least six hours old.
- **Stale reading:** the maximum valid observation timestamp in Silver is at
  least six hours old. Fresh uploads and Silver rebuild modification times do
  not substitute for measurement time.

The monitor packages the dashboard's timestamp parser, including Pacific local
time and day-first SMPS dates. It scans rows rather than trusting file order.
Malformed and future-dated timestamps cannot manufacture freshness. An unchanged
Silver ETag reuses its previously parsed observation timestamp, not upload time.

Never-reporting instruments stay unarmed, matching the old monitor's behavior.
Once an instrument has uploads, a missing/unreadable first Silver reading gets
a six-hour grace period before a `reading-unavailable` alert. Subsequent failed
checks retain the last confirmed reading time and cannot clear an active alert.
One instrument's read failure does not stop checks of the other instruments.

New stale incidents are combined into one email per invocation. The monitor
does not repeat an incident hourly. It sends one recovery notification after
both uploads and readings are fresh and verified. S3 stores per-instrument state
in `_monitor/upload_freshness_state.json`. The legacy global checkpoint is
migrated for its actual instrument. State advances only after SES accepts a
notification; delivery/state-write failures can cause retries, so email delivery
is not guaranteed exactly once.

## Deployment

Run `python scripts/aws/deploy_upload_monitor.py` using the authorized deployment
identity for account `213598695875`. Existing sender, recipients and environment
variables are preserved. Override recipients explicitly using
`UPLOAD_ALERT_TO_EMAILS` and `UPLOAD_ALERT_CC_EMAILS` (comma-separated addresses).
The runtime role reads only the five specific Silver CSVs, lists instrument
Bronze prefixes, writes its monitor state, and sends mail from the configured
lab address. It never changes measurement data or aggregation.

The deployment's initial Lambda invocation is a dry run. An invocation with
`{"dry_run": true}` reads source data and reports proposed notifications without
sending email or writing monitor state. Normal scheduled invocations send alerts
for genuine stale conditions.

Regression tests in `tests/test_upload_freshness_monitor.py` run in normal CI
without AWS access or real email. They cover instrument isolation, stale readings
despite fresh uploads, exact thresholds, recovery, retries, no-data handling,
timezone/schema validation, caching, and side-effect-free dry runs.
