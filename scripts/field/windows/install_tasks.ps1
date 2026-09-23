param(
    [int]$UploadEveryMinutes = 15,
    [string]$UploadTaskName = "DesMoinesDataMonitorUpload",
    [string]$SerialTaskName = "DesMoinesSerialLogger",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..\..")
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$UnifiedConfig = Join-Path $RepoRoot "config\instruments.json"
$UploadScript = Join-Path $RepoRoot "scripts\field\upload_to_aws.py"
$SerialScript = Join-Path $RepoRoot "scripts\field\acquire_serial.py"

if ($UploadEveryMinutes -lt 1) {
    throw "UploadEveryMinutes must be at least 1."
}
if (-not (Test-Path $PythonExe)) {
    throw "Missing $PythonExe. Create .venv and install requirements.txt first."
}
if (-not (Test-Path $UnifiedConfig)) {
    throw "Missing config\instruments.json. Copy config\instruments.example.json and set ports/paths first."
}
$ConfigPath = $UnifiedConfig

# A machine-level AWS_CREDS_FILE may have been set after this PowerShell process
# started. Copy it into the current environment so preflight and child tasks see it.
if (-not $env:AWS_CREDS_FILE) {
    $ConfiguredCreds = [Environment]::GetEnvironmentVariable("AWS_CREDS_FILE", "Machine")
    if (-not $ConfiguredCreds) {
        $ConfiguredCreds = [Environment]::GetEnvironmentVariable("AWS_CREDS_FILE", "User")
    }
    if ($ConfiguredCreds) {
        $env:AWS_CREDS_FILE = $ConfiguredCreds
    }
}
if ($env:AWS_CREDS_FILE -and -not (Test-Path -LiteralPath $env:AWS_CREDS_FILE)) {
    throw "AWS_CREDS_FILE does not exist: $env:AWS_CREDS_FILE"
}

Write-Host "Running serial and upload preflights..."
& $PythonExe $SerialScript --config $ConfigPath --check
if ($LASTEXITCODE -ne 0) {
    throw "Serial preflight failed."
}
$UploadArguments = @("--config", $ConfigPath)
if ($env:AWS_CREDS_FILE) {
    $UploadArguments += @("--aws-creds-file", $env:AWS_CREDS_FILE)
}
& $PythonExe $UploadScript @UploadArguments --check
if ($LASTEXITCODE -ne 0) {
    throw "Upload preflight failed."
}

$SerialAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$SerialScript`" --config `"$ConfigPath`"" `
    -WorkingDirectory $RepoRoot
$SerialTrigger = New-ScheduledTaskTrigger -AtLogOn
$SerialSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $SerialTaskName `
    -Action $SerialAction `
    -Trigger $SerialTrigger `
    -Settings $SerialSettings `
    -Description "Continuously records NO2, NEPH and CO2 serial data to local acquisition files." `
    -Force | Out-Null

$UploadArgumentString = "`"$UploadScript`" --config `"$ConfigPath`""
if ($env:AWS_CREDS_FILE) {
    $UploadArgumentString += " --aws-creds-file `"$env:AWS_CREDS_FILE`""
}
$UploadAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $UploadArgumentString `
    -WorkingDirectory $RepoRoot
$UploadTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $UploadEveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$UploadSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $UploadTaskName `
    -Action $UploadAction `
    -Trigger $UploadTrigger `
    -Settings $UploadSettings `
    -Description "Uploads completed instrument lines to AWS S3 Bronze every $UploadEveryMinutes minutes." `
    -Force | Out-Null

Write-Host "Installed exactly two tasks:"
Write-Host "  $SerialTaskName - continuous, starts at logon"
Write-Host "  $UploadTaskName - every $UploadEveryMinutes minutes"
if ($env:AWS_CREDS_FILE) {
    Write-Host "Upload credentials: $env:AWS_CREDS_FILE"
} else {
    Write-Host "Upload credentials: standard AWS profile/environment chain"
}

if ($RunNow) {
    Start-ScheduledTask -TaskName $SerialTaskName
    Start-ScheduledTask -TaskName $UploadTaskName
    Write-Host "Started both tasks. PuTTY must remain closed for the serial ports."
}

Get-ScheduledTask -TaskName $SerialTaskName, $UploadTaskName |
    Select-Object TaskName, State
