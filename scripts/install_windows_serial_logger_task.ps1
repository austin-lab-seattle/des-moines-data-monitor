param(
    [string]$TaskName = "DesMoinesSerialLogger",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$BatchPath = Join-Path $ScriptDir "run_serial_logger.bat"
$ConfigPath = Join-Path $RepoRoot "serial_instruments_config.json"

if (-not (Test-Path $BatchPath)) {
    throw "Missing serial logger runner: $BatchPath"
}

if (-not (Test-Path $ConfigPath)) {
    throw "Missing serial_instruments_config.json. Copy the example and verify every COM port and baud rate first."
}

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Missing .venv Python. Create the virtual environment and install requirements.txt first."
}
$LoggerPath = Join-Path $ScriptDir "log_serial_instruments.py"
& $VenvPython $LoggerPath --config $ConfigPath --check
if ($LASTEXITCODE -ne 0) {
    throw "Serial logger preflight failed. Fix the errors above before installing the task."
}

$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$BatchPath`"" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Continuously records Des Moines instruments from serial ports without PuTTY." `
    -Force | Out-Null

Write-Host "Installed continuous serial logger task '$TaskName'."
Write-Host "Log: $(Join-Path $RepoRoot 'serial_collector.log')"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started the serial logger. PuTTY must remain closed."
}
