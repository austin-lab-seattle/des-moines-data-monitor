param(
    [int]$EveryMinutes = 15,
    [string]$TaskName = "DesMoinesDataMonitorUpload",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$BatchPath = Join-Path $ScriptDir "run_pipeline.bat"
$ConfigPath = Join-Path $RepoRoot "instruments_config.json"

if ($EveryMinutes -lt 1) {
    throw "EveryMinutes must be at least 1."
}

if (-not (Test-Path $BatchPath)) {
    throw "Missing batch runner: $BatchPath"
}

if (-not (Test-Path $ConfigPath)) {
    throw "Missing instruments_config.json. Copy instruments_config.example.json and update the field-laptop data paths first."
}

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython) -and -not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found. Create .venv or add python.exe to PATH before installing the task."
}

$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$BatchPath`"" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Uploads Des Moines instrument data to S3." `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName' every $EveryMinutes minutes."
Write-Host "Runner: $BatchPath"
Write-Host "Log: $(Join-Path $RepoRoot 'collector.log')"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started the first upload run."
}

Write-Host "Verify with: Get-ScheduledTaskInfo -TaskName '$TaskName'"
