# Field Laptop Setup (Windows)

This runbook installs the Des Moines uploader on the field laptop and schedules
it with Windows Task Scheduler. Complete the manual checks before enabling the
recurring task.

## 1. Install the project

Open PowerShell as the Windows account that will own the scheduled task:

```powershell
git clone https://github.com/austin-lab-seattle/des-moines-data-monitor.git
cd des-moines-data-monitor
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item instruments_config.example.json instruments_config.json
```

If the repository is already installed, use `git pull origin main` instead of
cloning it again.

## 2. Configure the instrument paths

Edit `instruments_config.json`. Replace each example `data_glob` with the real
field-laptop path. Use forward slashes in JSON, even on Windows:

```json
"data_glob": "C:/InstrumentData/NO2/*NO2-CAPS*.dat"
```

Keep the glob specific enough that it cannot match exports, backups, or other
instrument files. The uploader discovers new rollover files automatically.

## 3. Configure AWS credentials

Preferred: configure the default AWS profile for the same Windows user that
runs the task:

```powershell
aws configure
```

Use region `us-west-2`. The credential should be a dedicated upload-only IAM
identity with access to the Des Moines S3 bucket, not a personal administrator
credential.

Temporary fallback: create a local `aws_creds.json` in the repository root. It
must contain only valid JSON:

```json
{
  "aws_access_key_id": "REPLACE_ME",
  "aws_secret_access_key": "REPLACE_ME",
  "region": "us-west-2"
}
```

Both `aws_creds.json` and `instruments_config.json` are gitignored. Never commit
or email them.

## 4. Run the no-upload preflight

```powershell
.\.venv\Scripts\python.exe scripts\upload_instrument_data.py --check
```

This verifies the JSON configuration, active instrument globs, matched files,
and local AWS credential configuration. It does not contact S3 or upload data.
Resolve every error before continuing.

## 5. Run one manual upload

The first real run may upload every complete line in a newly discovered file.
Confirm the globs and expected source filenames before running it:

```powershell
.\.venv\Scripts\python.exe scripts\upload_instrument_data.py
$LASTEXITCODE
Get-Content .\collector.log -Tail 100
```

Exit code `0` means every active instrument completed and the pipeline status
was written. A nonzero result means at least one instrument or the status write
failed. Failed batches stay in `sensor_buffer.db` and are retried on the next
run.

Confirm the new upload timestamp and instrument counts at
<https://deohs-des-moines-air.vercel.app> before scheduling the task.

## 6. Install the recurring task

Install a 15-minute task and start its first run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -EveryMinutes 15 -RunNow
```

The task is named `DesMoinesDataMonitorUpload`. It:

- starts one minute after installation and repeats every 15 minutes;
- runs missed executions when Windows becomes available;
- prevents overlapping uploader instances;
- retries a failed process up to three times at five-minute intervals;
- writes output to `collector.log` in the repository root.

By default, Windows registers it for the current user. If uploads must continue
while that user is signed out, open Task Scheduler, open the task's Properties,
choose **Run whether user is logged on or not**, and provide the dedicated task
account credentials when Windows requests them.

## 7. Verify the scheduled task

```powershell
Get-ScheduledTask -TaskName DesMoinesDataMonitorUpload
Get-ScheduledTaskInfo -TaskName DesMoinesDataMonitorUpload
Get-Content .\collector.log -Tail 100
```

`LastTaskResult` should be `0`. Recheck the live dashboard after the next
15-minute interval. Task Scheduler success alone is not enough; confirm both the
log and the dashboard timestamp.

## Updating or removing the schedule

Running the installer again safely updates the existing task:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -EveryMinutes 30
```

To disable it without deleting configuration:

```powershell
Disable-ScheduledTask -TaskName DesMoinesDataMonitorUpload
```

To remove it:

```powershell
Unregister-ScheduledTask -TaskName DesMoinesDataMonitorUpload -Confirm:$false
```

Do not delete `checkpoints/` or `sensor_buffer.db` during normal maintenance.
They protect incremental progress and failed-upload recovery.
