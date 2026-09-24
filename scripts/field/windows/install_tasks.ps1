param(
    [int]$UploadEveryMinutes = 15,
    [string]$UploadTaskName = "DesMoinesDataMonitorUpload",
    [string]$SerialTaskName = "DesMoinesSerialLogger",
    [string]$SharedCopyTaskName = "DesMoinesSharedDriveCopy",
    [string]$AwsCredsFile = "C:\des_moines\aws_creds.json",
    [string]$LocalDataRoot = "C:\des_moines\data",
    [string]$SharedDataRoot = "C:\Users\lab_admin\OneDrive - UW\Austin Lab-Des Moines Monitoring - Raw Data - Documents\Raw Data\des_moines\data",
    [string]$RuntimeRoot = "C:\des_moines\runtime",
    [string]$PythonExe = "",
    [switch]$RunWhenLoggedOff,
    [string]$RunAsUser = "$env:USERDOMAIN\$env:USERNAME",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..\..")
$UnifiedConfig = Join-Path $RepoRoot "config\instruments.json"
$UnifiedConfigExample = Join-Path $RepoRoot "config\instruments.example.json"
$UploadScript = Join-Path $RepoRoot "scripts\field\upload_to_aws.py"
$SerialScript = Join-Path $RepoRoot "scripts\field\acquire_serial.py"
$SharedCopyScript = Join-Path $RepoRoot "scripts\field\copy_to_shared_drive.py"

function Resolve-PythonExecutable {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (-not (Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
            throw "Configured Python executable does not exist: $RequestedPath"
        }
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }

    # Preserve existing installations that already use a repository venv, but
    # do not require one. A normal system Python is valid for field laptops.
    $VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        return (Resolve-Path -LiteralPath $VenvPython).Path
    }

    foreach ($CommandName in @("py.exe", "python.exe")) {
        $PythonCommand = Get-Command $CommandName -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $PythonCommand) {
            continue
        }
        if ($CommandName -eq "py.exe") {
            $ResolvedPython = & $PythonCommand.Source -3 -c "import sys; print(sys.executable)" 2>$null |
                Select-Object -Last 1
        } else {
            $ResolvedPython = & $PythonCommand.Source -c "import sys; print(sys.executable)" 2>$null |
                Select-Object -Last 1
        }
        if ($LASTEXITCODE -eq 0 -and $ResolvedPython) {
            $ResolvedPython = "$ResolvedPython".Trim()
            if (Test-Path -LiteralPath $ResolvedPython -PathType Leaf) {
                return (Resolve-Path -LiteralPath $ResolvedPython).Path
            }
        }
    }

    throw "Python 3 was not found. Install Python 3 for the lab_admin account and rerun this command."
}

$PythonExe = Resolve-PythonExecutable $PythonExe
$RequirementsFile = Join-Path $RepoRoot "requirements.txt"
Write-Host "Using Python: $PythonExe"
& $PythonExe -c "import boto3, serial"
if ($LASTEXITCODE -ne 0) {
    throw "Required Python packages are missing. Run: `"$PythonExe`" -m pip install -r `"$RequirementsFile`""
}

if ($UploadEveryMinutes -lt 1) {
    throw "UploadEveryMinutes must be at least 1."
}
if (-not (Test-Path -LiteralPath $UnifiedConfig -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $UnifiedConfigExample -PathType Leaf)) {
        throw "Missing both config\instruments.json and its tracked example. Run git pull and retry."
    }
    Copy-Item -LiteralPath $UnifiedConfigExample -Destination $UnifiedConfig
    Write-Host "Created local unified config: $UnifiedConfig"

    # Preserve only known-safe COM assignments from the retired serial config.
    # Enable flags and paths remain controlled by the current unified template.
    $LegacySerialConfig = Join-Path $RepoRoot "serial_instruments_config.json"
    if (Test-Path -LiteralPath $LegacySerialConfig -PathType Leaf) {
        try {
            $NewConfigObject = Get-Content -LiteralPath $UnifiedConfig -Raw | ConvertFrom-Json
            $LegacySerialObject = Get-Content -LiteralPath $LegacySerialConfig -Raw | ConvertFrom-Json
            $MigratedPorts = @()
            foreach ($LegacyInstrument in $LegacySerialObject.instruments) {
                if (-not $LegacyInstrument.id -or -not $LegacyInstrument.port) {
                    continue
                }
                $TargetInstrument = $NewConfigObject.instruments |
                    Where-Object { $_.id -eq $LegacyInstrument.id } |
                    Select-Object -First 1
                if ($TargetInstrument -and $TargetInstrument.serial) {
                    $TargetInstrument.serial.port = [string]$LegacyInstrument.port
                    $MigratedPorts += "$($LegacyInstrument.id)=$($LegacyInstrument.port)"
                }
            }
            if ($MigratedPorts.Count -gt 0) {
                $ConfigJson = $NewConfigObject | ConvertTo-Json -Depth 20
                $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
                [System.IO.File]::WriteAllText(
                    $UnifiedConfig,
                    $ConfigJson + [Environment]::NewLine,
                    $Utf8NoBom
                )
                Write-Host "Preserved legacy COM assignments: $($MigratedPorts -join ', ')"
            }
        } catch {
            Write-Warning "Could not read the legacy serial config; using current template ports. $($_.Exception.Message)"
        }
    }
    Write-Warning "A first-run config was created. Use acquire_serial.py --detect-ports later if the COM assignments need verification."
}
$ConfigPath = $UnifiedConfig

if (-not (Test-Path -LiteralPath $AwsCredsFile -PathType Leaf)) {
    throw "AWS credentials file not found: $AwsCredsFile"
}
if (-not (Test-Path -LiteralPath $LocalDataRoot -PathType Container)) {
    throw "Local data directory not found: $LocalDataRoot"
}
New-Item -ItemType Directory -Force $SharedDataRoot | Out-Null
$RuntimeLogs = Join-Path $RuntimeRoot "logs"
New-Item -ItemType Directory -Force $RuntimeLogs | Out-Null
$SharedCopyLog = Join-Path $RuntimeLogs "shared_drive_copy.log"

# Migrate the known repository-relative example paths to the canonical local
# data tree. Absolute or custom paths are left untouched.
$ConfigObject = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$ConfigChanged = $false
$DataRootForward = $LocalDataRoot -replace "\\", "/"
function Convert-CanonicalDataPath {
    param([string]$Value)
    if (-not $Value) {
        return $Value
    }
    $Normalized = $Value -replace "\\", "/"
    if ($Normalized.StartsWith("data/")) {
        $script:ConfigChanged = $true
        return "$DataRootForward/$($Normalized.Substring(5))"
    }
    return $Value
}
foreach ($Instrument in $ConfigObject.instruments) {
    if ($Instrument.data_glob -is [string]) {
        $Instrument.data_glob = Convert-CanonicalDataPath $Instrument.data_glob
    } elseif ($null -ne $Instrument.data_glob) {
        $Instrument.data_glob = @(
            $Instrument.data_glob | ForEach-Object { Convert-CanonicalDataPath $_ }
        )
    }
    if ($null -ne $Instrument.serial -and $Instrument.serial.output_dir) {
        $Instrument.serial.output_dir = Convert-CanonicalDataPath $Instrument.serial.output_dir
    }
}
if ($ConfigChanged) {
    Copy-Item -LiteralPath $ConfigPath -Destination "$ConfigPath.bak-data-root" -Force
    $ConfigJson = $ConfigObject | ConvertTo-Json -Depth 20
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $ConfigPath,
        $ConfigJson + [Environment]::NewLine,
        $Utf8NoBom
    )
    Write-Host "Updated relative instrument paths to $LocalDataRoot"
}

$OldRepoData = Join-Path $RepoRoot "data"
if (Test-Path -LiteralPath $OldRepoData -PathType Container) {
    $OldRepoFile = Get-ChildItem -LiteralPath $OldRepoData -File -Recurse | Select-Object -First 1
    if ($OldRepoFile) {
        Write-Warning (
            "Old files still exist under $OldRepoData. The tasks now use $LocalDataRoot. " +
            "Merge those historical files into the canonical data folder after checking for duplicates."
        )
    }
}

$TaskPassword = $null
if ($RunWhenLoggedOff) {
    $TaskCredential = Get-Credential `
        -UserName $RunAsUser `
        -Message "Enter the Windows password used to run all three Des Moines tasks while logged off."
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

Write-Host "Running serial, upload and shared-copy preflights..."
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
& $PythonExe $SharedCopyScript `
    --source $LocalDataRoot `
    --destination $SharedDataRoot `
    --log-file $SharedCopyLog `
    --check
if ($LASTEXITCODE -ne 0) {
    throw "Shared-drive copy preflight failed."
}

# Stop any old processes before relocating their state. Existing tasks remain
# registered until all replacements have been created successfully.
foreach ($TaskToStop in @($SerialTaskName, $UploadTaskName, $SharedCopyTaskName, "desmoines_data_upload")) {
    Stop-ScheduledTask -TaskName $TaskToStop -ErrorAction SilentlyContinue
}

function Move-LegacyRuntimeItem {
    param(
        [string]$Source,
        [string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source)) {
        return
    }
    if (Test-Path -LiteralPath $Destination) {
        Write-Warning "Kept legacy runtime item because destination already exists: $Source"
        return
    }
    $DestinationParent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force $DestinationParent | Out-Null
    Move-Item -LiteralPath $Source -Destination $Destination
    Write-Host "Moved runtime state: $Source -> $Destination"
}

Move-LegacyRuntimeItem `
    -Source (Join-Path $RepoRoot "checkpoints") `
    -Destination (Join-Path $RuntimeRoot "checkpoints")
Move-LegacyRuntimeItem `
    -Source (Join-Path $RepoRoot "sensor_buffer.db") `
    -Destination (Join-Path $RuntimeRoot "sensor_buffer.db")
Move-LegacyRuntimeItem `
    -Source (Join-Path $RepoRoot "collector.log") `
    -Destination (Join-Path $RuntimeRoot "collector.log")
Move-LegacyRuntimeItem `
    -Source (Join-Path $RepoRoot "serial_collector.log") `
    -Destination (Join-Path $RuntimeRoot "serial_collector.log")
Move-LegacyRuntimeItem `
    -Source (Join-Path $RepoRoot "serial_logs") `
    -Destination (Join-Path $RuntimeRoot "serial_logs")

$SerialAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$SerialScript`" --config `"$ConfigPath`"" `
    -WorkingDirectory $RuntimeRoot
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
    -WorkingDirectory $RuntimeRoot
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

$SharedCopyArgumentString = (
    "`"$SharedCopyScript`" --source `"$LocalDataRoot`" " +
    "--destination `"$SharedDataRoot`" --log-file `"$SharedCopyLog`""
)
$SharedCopyAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $SharedCopyArgumentString `
    -WorkingDirectory $RuntimeRoot
$SharedCopyTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(7) `
    -RepetitionInterval (New-TimeSpan -Minutes $UploadEveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$SharedCopySettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-DesMoinesTask `
    -TaskName $SharedCopyTaskName `
    -Action $SharedCopyAction `
    -Trigger $SharedCopyTrigger `
    -Settings $SharedCopySettings `
    -Description "Copies stable local instrument snapshots into the UW OneDrive shared folder."

# Remove the known hand-created predecessor only after all replacement tasks
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

$LegacyConfigDirectory = Join-Path $RuntimeRoot "legacy-config"
New-Item -ItemType Directory -Force $LegacyConfigDirectory | Out-Null
foreach ($LegacyConfigName in @("instruments_config.json", "serial_instruments_config.json")) {
    $LegacyConfigPath = Join-Path $RepoRoot $LegacyConfigName
    if (Test-Path -LiteralPath $LegacyConfigPath -PathType Leaf) {
        $ArchivedConfigPath = Join-Path $LegacyConfigDirectory $LegacyConfigName
        if (-not (Test-Path -LiteralPath $ArchivedConfigPath)) {
            Move-Item -LiteralPath $LegacyConfigPath -Destination $ArchivedConfigPath
            Write-Host "Archived obsolete config: $LegacyConfigPath"
        } else {
            Write-Warning "Legacy config already archived; left local file untouched: $LegacyConfigPath"
        }
    }
}

Write-Host "Installed exactly three tasks:"
if ($RunWhenLoggedOff) {
    Write-Host "  $SerialTaskName - continuous, starts with Windows"
} else {
    Write-Host "  $SerialTaskName - continuous, starts at logon"
}
Write-Host "  $UploadTaskName - every $UploadEveryMinutes minutes"
Write-Host "  $SharedCopyTaskName - every $UploadEveryMinutes minutes, staggered by 6 minutes"
Write-Host "Upload credentials: $AwsCredsFile"
Write-Host "Shared copy: $LocalDataRoot -> $SharedDataRoot"
if ($RunWhenLoggedOff) {
    Write-Host "Windows account: $RunAsUser (runs whether logged on or not)"
} else {
    Write-Host "Windows account: current interactive user"
}

if ($RunNow) {
    Start-ScheduledTask -TaskName $SerialTaskName
    Start-ScheduledTask -TaskName $UploadTaskName
    Start-ScheduledTask -TaskName $SharedCopyTaskName
    Write-Host "Started all three tasks. Do not run another serial reader on the configured ports."
}

Get-ScheduledTask -TaskName $SerialTaskName, $UploadTaskName, $SharedCopyTaskName |
    Select-Object TaskName, State
