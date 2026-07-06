param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000,
    [int]$IntervalSeconds = 10,
    [int]$CrashCooldownSeconds = 30,
    [int]$StartupWaitSeconds = 20,
    [string]$StopFile = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunLogDir = Join-Path $ProjectRoot "data\runlogs"
New-Item -ItemType Directory -Path $RunLogDir -Force | Out-Null

$WatchdogLog = Join-Path $RunLogDir "backend-watchdog.log"
$BackendStdout = Join-Path $RunLogDir "backend.out.log"
$BackendStderr = Join-Path $RunLogDir "backend.err.log"
$PidFile = Join-Path $RunLogDir "backend.pid"

if ([string]::IsNullOrWhiteSpace($StopFile)) {
    $StopFile = Join-Path $RunLogDir "backend-watchdog.stop"
}

function Write-WatchdogLog([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $WatchdogLog -Value $line -Encoding UTF8
}

function Test-ListenPort([int]$PortToCheck) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort $PortToCheck -State Listen -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}

function Test-ProcessAlive([int]$ProcessId) {
    if ($ProcessId -le 0) {
        return $false
    }
    try {
        return [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}

function Get-BackendPidFromFile {
    try {
        if (-not (Test-Path -LiteralPath $PidFile)) {
            return 0
        }
        return [int]((Get-Content -LiteralPath $PidFile -Raw).Trim())
    } catch {
        return 0
    }
}

function Start-BackendProcess {
    $venvUvicorn = Join-Path $ProjectRoot ".venv\Scripts\uvicorn.exe"
    if (Test-Path -LiteralPath $venvUvicorn) {
        $filePath = $venvUvicorn
        $arguments = @("main:app", "--host", $HostName, "--port", [string]$Port, "--access-log")
    } else {
        $filePath = "uv"
        $arguments = @("run", "uvicorn", "main:app", "--host", $HostName, "--port", [string]$Port, "--access-log")
    }

    if ([string]::IsNullOrWhiteSpace($env:STORAGE_BACKEND)) {
        $env:STORAGE_BACKEND = "sqlite"
    }
    if ([string]::IsNullOrWhiteSpace($env:CHATGPT2API_DISABLE_REGISTER_AUTOSTART)) {
        $env:CHATGPT2API_DISABLE_REGISTER_AUTOSTART = "1"
    }

    $process = Start-Process `
        -FilePath $filePath `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $BackendStdout `
        -RedirectStandardError $BackendStderr `
        -PassThru

    Set-Content -LiteralPath $PidFile -Value ([string]$process.Id) -Encoding ASCII
    Write-WatchdogLog "backend_started pid=$($process.Id) url=http://$HostName`:$Port"
    return $process
}

$IntervalSeconds = [Math]::Max(5, $IntervalSeconds)
$CrashCooldownSeconds = [Math]::Max(10, $CrashCooldownSeconds)
$StartupWaitSeconds = [Math]::Max(5, $StartupWaitSeconds)
$lastStartAt = [DateTime]::MinValue

Write-WatchdogLog "watchdog_started port=$Port interval=$IntervalSeconds"

while (-not (Test-Path -LiteralPath $StopFile)) {
    $listening = Test-ListenPort $Port
    $backendPid = Get-BackendPidFromFile
    $pidAlive = Test-ProcessAlive $backendPid

    if (-not $listening -and -not $pidAlive) {
        $secondsSinceStart = ([DateTime]::UtcNow - $lastStartAt).TotalSeconds
        if ($secondsSinceStart -lt $CrashCooldownSeconds) {
            Write-WatchdogLog "restart_delayed cooldown_remaining=$([Math]::Ceiling($CrashCooldownSeconds - $secondsSinceStart))"
            Start-Sleep -Seconds $IntervalSeconds
            continue
        }
        try {
            Start-BackendProcess | Out-Null
            $lastStartAt = [DateTime]::UtcNow
            Start-Sleep -Seconds $StartupWaitSeconds
        } catch {
            Write-WatchdogLog "backend_start_failed=$($_.Exception.Message)"
            Start-Sleep -Seconds $CrashCooldownSeconds
        }
        continue
    }

    if (-not $listening -and $pidAlive) {
        Write-WatchdogLog "backend_process_alive_waiting_for_port pid=$backendPid"
    }

    Start-Sleep -Seconds $IntervalSeconds
}

Write-WatchdogLog "watchdog_stopped stop_file=$StopFile"
