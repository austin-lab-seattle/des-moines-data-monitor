param(
    [int]$EveryMinutes = 15,
    [string]$TaskName = "DesMoinesDataMonitorUpload"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$BatchPath = Join-Path $ScriptDir "run_pipeline.bat"

if (-not (Test-Path $BatchPath)) {
    throw "Missing batch runner: $BatchPath"
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
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Uploads Des Moines instrument data to S3." `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName' every $EveryMinutes minutes."
Write-Host "Runner: $BatchPath"
