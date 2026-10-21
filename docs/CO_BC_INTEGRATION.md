# Preparing CO and BC file integration

Folder copying and scientific ingestion are separate operations. Copying a
vendor folder does not make its contents validated measurements or publish it
on the dashboard. CO (carbon monoxide) is not CO2 (carbon dioxide).

## Current readiness

| Stage | BC | CO |
| --- | --- | --- |
| Local data tree to UW OneDrive | Generic recursive copy is available | Same generic copy is available |
| File upload to AWS Bronze | `BC-MA200` adapter and config exist | Not supported yet; unknown IDs are rejected |
| Silver, API and dashboard | `BC-MA200` code exists; real export validation is still required | No parser, measurement mapping or dashboard entry yet |
| Real export reference in this repository | Not available; the MA200 reference is explicitly a format-only fixture | Not available |

The code does not prove that the field laptop has these folders or a running
scheduled task. Check the actual laptop before enabling ingestion. A different
BC model or export layout needs its own validation, not an assumed MA200 match.

## Copy-only preparation

The installed `DesMoinesSharedDriveCopy` task runs
`scripts/field/copy_to_shared_drive.py` recursively from the configured local
data root, normally `C:\des_moines\data`. There is no instrument allowlist or
extension filter in that copier. New CO and BC subfolders under that root are
picked up automatically on the next scheduled copy. Relative paths, source
bytes (including line endings and encoding), and file modification times are
preserved. Sources and unrelated destination files are not deleted; files that
are locked or changing are deferred.

The task does not collect files from arbitrary vendor folders elsewhere on the
laptop. Your planned vendor-export code should first copy stable, unmodified
files into separate folders under the canonical data root, preserving filenames
and provenance. Do not point the shared copier at a broad user/system directory.
Do not let two jobs write the same destination files.

Check the existing task without starting it:

```powershell
Get-ScheduledTask -TaskName DesMoinesSharedDriveCopy | Select-Object -ExpandProperty Actions
Get-ScheduledTaskInfo -TaskName DesMoinesSharedDriveCopy
Get-Content C:\des_moines\runtime\logs\shared_drive_copy.log -Tail 40
```

For an explicit copy run, use the real UW shared folder as `--destination`:

```powershell
py -3 scripts\field\copy_to_shared_drive.py --source "C:\des_moines\data" --destination "<actual UW shared data folder>"
```

That command copies files. It does not upload them to AWS or change scientific
values. The script's `--check` option validates paths without copying files but
may create the destination directory.

## BC activation checklist

1. Obtain one actual unmodified export, including headers and several complete
   rows. Confirm the BC model, channels, units, timestamp format, timezone and
   missing/error flags. Do not infer these from the folder name.
2. Validate it offline with
   `py -3 scripts\field\identify_instrument_data.py "<actual BC export>"`.
   Recognition alone does not validate its time/measurement mapping.
3. Review `BC-MA200` in local `config/instruments.json`. The tracked example
   matches `C:/des_moines/data/black_carbon/*MA200*.csv` only: a folder called
   `BC`, nested exports, or filenames without `MA200` will not match that glob
   automatically. Set narrow patterns for the confirmed export layout.
4. Add regression coverage from the verified header/schema for pre-Bronze
   identity, Silver values and missing-value handling, timestamp conversion,
   API records, averages and displayed units. Keep synthetic tests labeled.
5. Run the uploader's `--check` preflight before enabling/scheduling uploads.
   Compare actual Bronze bytes and Silver values against the source, then check
   the dashboard and per-instrument freshness alert. Existing active config
   begins ingesting matching recognized files on the next uploader run.

## CO activation checklist

Do not add a guessed CO entry to the active config or reuse `CO2-LICOR`.
Keep CO copy-only until the instrument model, actual export, units, timestamps,
timezone, acquisition cadence, missing/error flags and any approved calibration
are known. Then add one explicit, distinct instrument ID across:

- Field config/discovery and content-identity validation.
- Silver parsing, deduplication, timestamp handling and documented units or
  scientifically approved conversions.
- API validation, latest readings, observation/export paths and averages.
- Conditions, recent-values tables, chart labels and team-console selection.
- S3/IAM deployment configuration and per-instrument freshness monitoring.
- Offline source-to-Silver-to-API accuracy tests and frontend regressions,
  including distinguishing CO from CO2 and rejecting unsupported/mixed content.

No CO concentration, unit conversion, parser or production reading is invented
by this preparation. The existing CI runs the recursive-copy and unsupported-CO
regressions with the rest of the Python suite.

## Supplied CO excerpt: still awaiting a schema

The excerpt supplied on October 21 has a leading token, a timestamp like
`2026/10/01 00:00:00`, then numbered code/value pairs. The complete pasted rows
use codes `53, 13, 49, 75, 72, 73, 77, 79, 83, 110, 86, 84` in that order.
The last pasted row is incomplete. The leading tokens vary and are not yet
identified as status, framing, checksums or corruption. The timestamps have no
timezone offset. The instrument model, field-code meanings, concentration units
and timezone remain unconfirmed. Do not label code 53 as CO, assume ppm, treat
all numeric diagnostics as measurements, or repair incomplete rows by inventing
values. No BC export was included with that excerpt.

There is a tentative match to the Serinus protocol, not a confirmed model
identification. The manufacturer-hosted
[Serinus 30 manual, Appendix A, pages 207-213](https://metone.com/wp-content/uploads/2022/08/Acoem-Met-One-Instruments-GAS-1030-user-manual-version-3.3-20220726.pdf)
maps code 53 to the primary gas average, 50 to the instantaneous primary gas
concentration, 86 to primary raw concentration, 116 to instrument units and
163 to the averaging-period setting. Codes 50, 116 and 163 are absent from the
excerpt. Confirm the actual model/firmware and these settings before applying
that mapping. Do not present repeated running-average values as independent
instantaneous observations or average them again without labeling the source
window and defining the intended statistic.
