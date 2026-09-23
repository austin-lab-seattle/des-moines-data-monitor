param(
    [int]$UploadEveryMinutes = 15,
    [string]$UploadTaskName = "DesMoinesDataMonitorUpload",
    [string]$SerialTaskName = "DesMoinesSerialLogger",
    [string]$AwsCredsFile = "C:\des_moines\aws_creds.json",
    [switch]$RunWhenLoggedOff,
    [string]$RunAsUser = "$env:USERDOMAIN\$env:USERNAME",
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

if (-not (Test-Path -LiteralPath $AwsCredsFile -PathType Leaf)) {
    throw "AWS credentials file not found: $AwsCredsFile"
}

$TaskPassword = $null
if ($RunWhenLoggedOff) {
    $TaskCredential = Get-Credential `
        -UserName $RunAsUser `
        -Message "Enter the Windows password used to run both Des Moines tasks while logged off."
    if (-not $TaskCredential) {
        throw "Windows task credentials were not provided."
    }
    $RunAsUser = $TaskCredential.UserName
    $TaskPassword = $TaskCredential.GetNetworkCredential().Password
}

function Register-DesMoinesTask {
    param(
        [string]$TaskName,
        $Action,
        $Trigger,
        $Settings,
        [string]$Description
    )

    $Registration = @{
        TaskName = $TaskName
        Action = $Action
        Trigger = $Trigger
        Settings = $Settings
        Description = $Description
        Force = $true
    }
    if ($RunWhenLoggedOff) {
        $Registration.User = $RunAsUser
        $Registration.Password = $TaskPassword
        $Registration.RunLevel = "Limited"
    }
    Register-ScheduledTask @Registration | Out-Null
}

Write-Host "Running serial and upload preflights..."
& $PythonExe $SerialScript --config $ConfigPath --check
if ($LASTEXITCODE -ne 0) {
    throw "Serial preflight failed."
}
$UploadArguments = @(
    "--config", $ConfigPath,
    "--aws-creds-file", $AwsCredsFile
)
& $PythonExe $UploadScript @UploadArguments --check
if ($LASTEXITCODE -ne 0) {
    throw "Upload preflight failed."
}

$SerialAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$SerialScript`" --config `"$ConfigPath`"" `
    -WorkingDirectory $RepoRoot
if ($RunWhenLoggedOff) {
    $SerialTrigger = New-ScheduledTaskTrigger -AtStartup
} else {
    $SerialTrigger = New-ScheduledTaskTrigger -AtLogOn
}
$SerialSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-DesMoinesTask `
    -TaskName $SerialTaskName `
    -Action $SerialAction `
    -Trigger $SerialTrigger `
    -Settings $SerialSettings `
    -Description "Continuously records the enabled serial instruments to local acquisition files."

$UploadArgumentString = (
    "`"$UploadScript`" --config `"$ConfigPath`" " +
    "--aws-creds-file `"$AwsCredsFile`""
)
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

Register-DesMoinesTask `
    -TaskName $UploadTaskName `
    -Action $UploadAction `
    -Trigger $UploadTrigger `
    -Settings $UploadSettings `
    -Description "Uploads completed instrument lines to AWS S3 Bronze every $UploadEveryMinutes minutes."

# Remove the known hand-created predecessor only after both replacement tasks
# have registered successfully, so a credential/registration error cannot leave
# the laptop with no uploader at all.
$LegacyTaskNames = @("desmoines_data_upload")
foreach ($LegacyTaskName in $LegacyTaskNames) {
    $LegacyTask = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
    if ($LegacyTask) {
        Stop-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false
        Write-Host "Removed legacy task: $LegacyTaskName"
    }
}

Write-Host "Installed exactly two tasks:"
if ($RunWhenLoggedOff) {
    Write-Host "  $SerialTaskName - continuous, starts with Windows"
} else {
    Write-Host "  $SerialTaskName - continuous, starts at logon"
}
Write-Host "  $UploadTaskName - every $UploadEveryMinutes minutes"
Write-Host "Upload credentials: $AwsCredsFile"
if ($RunWhenLoggedOff) {
    Write-Host "Windows account: $RunAsUser (runs whether logged on or not)"
} else {
    Write-Host "Windows account: current interactive user"
}

if ($RunNow) {
    Start-ScheduledTask -TaskName $SerialTaskName
    Start-ScheduledTask -TaskName $UploadTaskName
    Write-Host "Started both tasks. PuTTY must remain closed for the serial ports."
}

Get-ScheduledTask -TaskName $SerialTaskName, $UploadTaskName |
    Select-Object TaskName, State
