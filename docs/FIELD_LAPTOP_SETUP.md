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
New-Item -ItemType Directory -Force config | Out-Null
Copy-Item config\instruments.example.json config\instruments.json
```

If the repository is already installed, use `git pull origin main` instead of
cloning it again.

## 2. Configure the instrument paths

Edit `config/instruments.json`. Replace each example `data_glob` with the real
field-laptop path. Use forward slashes in JSON, even on Windows:

```json
"data_glob": "C:/InstrumentData/NO2/*NO2-CAPS*.dat"
```

Keep the glob specific enough that it cannot match exports, backups, or other
instrument files. The uploader discovers new rollover files automatically.

## 2a. Record the serial instruments without PuTTY

The uploader does not read COM ports itself. `scripts/field/acquire_serial.py`
is the continuous recorder that replaces PuTTY for the confirmed NO2-CAPS and
NEPH-PM25 serial streams. LI-COR format detection and configuration are present,
but serial acquisition is disabled until its connection method is confirmed.
The logger writes the configured growing files with the header
`PC_Date_Time<TAB>Raw_Line` in folders already read by the uploader. `Raw_Line`
is the exact instrument payload; no measurements are parsed or corrected before
Bronze. Separate lossless diagnostic logs are retained under `serial_logs/`.

Every acquisition row uses the field laptop's local clock in
`YYYY-MM-DD HH:MM:SS.sss` form. In Silver this becomes the reporting time. This
is important because the supplied captures
showed a 12-hour nephelometer clock jump, NO2 reports 1904-epoch seconds, and
LI-COR XML has no timestamp. For NO2 and the nephelometer, the original
instrument time and `PC_minus_instrument_s` are retained as diagnostic columns;
charts and time filters use the PC-local timestamp.

List the ports from PowerShell without opening Device Manager:

```powershell
.\.venv\Scripts\python.exe scripts\field\acquire_serial.py --list-ports
```

This prints entries such as `COM7  USB Serial Port (COM7)` plus the hardware
ID. The equivalent built-in Windows query is:

```powershell
Get-CimInstance Win32_SerialPort | Format-Table DeviceID, Name, PNPDeviceID -AutoSize
```

The logger can also identify the instrument connected to each port by passively
sampling its data format. It sends no commands to the instruments. Stop the
serial task and close PuTTY first, then run:

```powershell
Stop-ScheduledTask -TaskName DesMoinesSerialLogger -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe scripts\field\acquire_serial.py `
    --detect-ports `
    --probe-seconds 15
```

The output identifies the distinctive CAPS numeric record, nephelometer dated
CSV record, and LI-COR XML record if present. Detection is report-only by default. To save
the mapping, rerun with `--apply-detected-ports`; it changes the configuration
only when every enabled serial instrument is uniquely identified and saves
the previous file as `config\instruments.json.bak`:

```powershell
.\.venv\Scripts\python.exe scripts\field\acquire_serial.py `
    --apply-detected-ports `
    --probe-seconds 15
```

To change one port manually without editing JSON:

```powershell
.\.venv\Scripts\python.exe scripts\field\acquire_serial.py `
    --set-port "NO2-CAPS=COM7"
```

Use `NEPH-PM25` and `CO2-LICOR` for the other instrument IDs. After detection
or a manual change, run
`Start-ScheduledTask -TaskName DesMoinesSerialLogger`. A port reported as
`unavailable` is normally still owned by PuTTY or the running logger. An
`unknown` result means no complete recognized record arrived; try 30 seconds.

In the same `config/instruments.json`, assign the detected COM port inside the
`serial` block. NEPH and NO2 have `serial.enabled: true`. CO2-LICOR retains a
known parser and candidate port but has `serial.enabled: false`; change it to
`true` only after confirming that LI-COR should also be acquired directly.
BC and SMPS have `acquisition_type: "file"` and no serial block. The configured
serial-capable instruments use 38400 baud.

The example writes directly to the canonical growing files—there is no second
renamed acquisition copy:

```text
C:\des_moines\data\no2_caps\no2.txt
C:\des_moines\data\nephlometer\Neph.txt
C:\des_moines\data\co2_li_cor\co2.txt
```

The tracked example config uses absolute `C:/des_moines/data/...` output paths.
If the field laptop uses another folder, put its full path in `output_dir` while
keeping the required filename unchanged. A filename containing `{date}` is also
supported when daily rollover files are preferred, but it is not required.

Close PuTTY before the preflight—only one program can own each COM port—then
validate the configuration and run a short manual test:

```powershell
.\.venv\Scripts\python.exe scripts\field\acquire_serial.py --check
.\.venv\Scripts\python.exe scripts\field\acquire_serial.py
```

Wait until each active instrument reports that its port is open and confirm the
configured files under `C:\des_moines\data` are updating. Stop the manual test
with `Ctrl+C`, then
install all three required tasks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\field\windows\install_tasks.ps1 -UploadEveryMinutes 15 -RunNow
Get-ScheduledTaskInfo -TaskName DesMoinesSerialLogger
Get-ScheduledTaskInfo -TaskName DesMoinesDataMonitorUpload
Get-ScheduledTaskInfo -TaskName DesMoinesSharedDriveCopy
Get-Content .\serial_collector.log -Tail 100  # manual-test log only
```

The serial task creates local files continuously, while the upload task
checkpoints and sends completed lines to S3. Do not run PuTTY logging on these
ports after enabling the serial task.

## 3. Configure AWS credentials

The field laptop uses this protected file outside the repository:

```powershell
$credsPath = "C:\des_moines\aws_creds.json"
if (-not (Test-Path -LiteralPath $credsPath -PathType Leaf)) {
    throw "Credentials file not found: $credsPath"
}
```

The file must contain only valid JSON:

```json
{
  "aws_access_key_id": "REPLACE_ME",
  "aws_secret_access_key": "REPLACE_ME",
  "region": "us-west-2"
}
```

Use a dedicated upload-only IAM identity with access to the Des Moines S3
bucket, not a personal administrator credential. Credential JSON files and
`config/instruments.json` are gitignored. Never commit or email credentials.

The installer passes this path directly to the upload task. A machine-wide
environment variable and `aws configure` are not required.

## 4. Run the no-upload preflight

```powershell
.\.venv\Scripts\python.exe scripts\field\upload_to_aws.py `
    --aws-creds-file "C:\des_moines\aws_creds.json" `
    --check
```

This verifies the JSON configuration, active instrument globs, matched files,
and local AWS credential configuration. It does not contact S3 or upload data.
Resolve every error before continuing.

## 5. Run one manual upload

The first real run may upload every complete line in a newly discovered file.
Confirm the globs and expected source filenames before running it:

```powershell
.\.venv\Scripts\python.exe scripts\field\upload_to_aws.py `
    --aws-creds-file "C:\des_moines\aws_creds.json"
$LASTEXITCODE
Get-Content .\collector.log -Tail 100
```

Exit code `0` means every active instrument completed and the pipeline status
was written. A nonzero result means at least one instrument or the status write
failed. Failed batches stay in the SQLite buffer and are retried on the next
run. The installer moves that buffer to
`C:\des_moines\runtime\sensor_buffer.db` before registering the recurring
tasks.

Confirm the new upload timestamp and instrument counts at
<https://deohs-des-moines-air.vercel.app> before scheduling the task.

## 6. Install exactly three tasks

The combined installer creates or updates all required tasks and starts them.
Its shared-copy defaults are `C:\des_moines\data` and:

```text
C:\Users\lab_admin\OneDrive - UW\Austin Lab-Des Moines Monitoring - Raw Data - Documents\Raw Data\des_moines\data
```

```powershell
powershell -ExecutionPolicy Bypass `
    -File scripts\field\windows\install_tasks.ps1 `
    -AwsCredsFile "C:\des_moines\aws_creds.json" `
    -UploadEveryMinutes 15 `
    -RunWhenLoggedOff `
    -RunAsUser "$env:COMPUTERNAME\lab_admin" `
    -RunNow
```

The tasks are:

- `DesMoinesSerialLogger`: continuous acquisition from Windows startup for enabled serial instruments;
- `DesMoinesDataMonitorUpload`: all-instrument AWS upload every 15 minutes.
- `DesMoinesSharedDriveCopy`: stable local-data snapshots every 15 minutes,
  starting six minutes after the AWS job to reduce simultaneous reads.

The installer prompts once for the Windows password and stores it using Task
Scheduler's protected credential storage so all three tasks can run while
`lab_admin` is signed out. If the password changes, rerun the installer. The
known legacy task `desmoines_data_upload` is stopped and removed, preventing a
second uploader from reading the same files.

The upload task:

- starts one minute after installation and repeats every 15 minutes;
- runs missed executions when Windows becomes available;
- prevents overlapping uploader instances;
- retries a failed process up to three times at five-minute intervals;
- writes output to `C:\des_moines\runtime\collector.log`.

Omit `-RunWhenLoggedOff` only for a short interactive test. Do not select **Do
not store password**: that uses an S4U logon without normal network-resource
access. The tasks do not require **Run with highest privileges** and the
installer uses limited privileges.

Mapped drive letters and the interactive OneDrive sync client may be unavailable
while the account is logged off. The scheduled task writes snapshots to the
local OneDrive folder using its full path; cloud synchronization may wait until
OneDrive is running in the `lab_admin` session. Keep the actively written
instrument files and `C:\des_moines\aws_creds.json` on local disk.

### Active-file and SharePoint safety

Keep live instrument output in a local, non-synchronized acquisition directory
whenever possible. Do not point the instrument, SharePoint/OneDrive and the AWS
uploader at a file that the sync client rewrites or renames in place. Two readers
normally coexist, but on Windows the instrument or sync client can request an
exclusive sharing mode, producing a sharing-violation error.

The AWS uploader retries a locked file three times. If it remains locked, it
leaves that file's byte checkpoint unchanged and retries it on the next
15-minute run; it never skips the unread bytes. The shared-copy task similarly
defers locked or changing files, copies into a temporary destination, and only
replaces the OneDrive file after a stable complete read. It never deletes local
or shared files.

## 7. Verify the scheduled tasks

```powershell
Get-ScheduledTask -TaskName DesMoinesDataMonitorUpload
Get-ScheduledTaskInfo -TaskName DesMoinesDataMonitorUpload
Get-ScheduledTask -TaskName DesMoinesSerialLogger
Get-ScheduledTaskInfo -TaskName DesMoinesSerialLogger
Get-ScheduledTask -TaskName DesMoinesSharedDriveCopy
Get-ScheduledTaskInfo -TaskName DesMoinesSharedDriveCopy
Get-Content C:\des_moines\runtime\collector.log -Tail 100
Get-Content C:\des_moines\runtime\serial_collector.log -Tail 100
Get-Content C:\des_moines\runtime\logs\shared_drive_copy.log -Tail 100
```

`LastTaskResult` should be `0`. Recheck the live dashboard after the next
15-minute interval. Task Scheduler success alone is not enough; confirm both the
log and the dashboard timestamp.

## Updating or removing the schedule

Running the installer again safely updates all existing tasks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\field\windows\install_tasks.ps1 -UploadEveryMinutes 30
```

To disable it without deleting configuration:

```powershell
Disable-ScheduledTask -TaskName DesMoinesSerialLogger
Disable-ScheduledTask -TaskName DesMoinesDataMonitorUpload
Disable-ScheduledTask -TaskName DesMoinesSharedDriveCopy
```

To remove it:

```powershell
Unregister-ScheduledTask -TaskName DesMoinesDataMonitorUpload -Confirm:$false
Unregister-ScheduledTask -TaskName DesMoinesSerialLogger -Confirm:$false
Unregister-ScheduledTask -TaskName DesMoinesSharedDriveCopy -Confirm:$false
```

Do not delete `C:\des_moines\runtime\checkpoints` or
`C:\des_moines\runtime\sensor_buffer.db` during normal maintenance. They
protect incremental progress and failed-upload recovery.
