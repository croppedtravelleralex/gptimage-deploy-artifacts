param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 3000,
    [int]$IntervalSeconds = 10,
    [int]$CrashCooldownSeconds = 30,
    [int]$StartupWaitSeconds = 20,
    [string]$StopFile = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$WebRoot = Join-Path $ProjectRoot "web"
$RunLogDir = Join-Path $ProjectRoot "data\runlogs"
New-Item -ItemType Directory -Path $RunLogDir -Force | Out-Null

$WatchdogLog = Join-Path $RunLogDir "frontend-watchdog.log"
$FrontendStdout = Join-Path $RunLogDir "frontend.out.log"
$FrontendStderr = Join-Path $RunLogDir "frontend.err.log"
$PidFile = Join-Path $RunLogDir "frontend.pid"

if ([string]::IsNullOrWhiteSpace($StopFile)) {
    $StopFile = Join-Path $RunLogDir "frontend-watchdog.stop"
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

function Get-FrontendPidFromFile {
    try {
        if (-not (Test-Path -LiteralPath $PidFile)) {
            return 0
        }
        return [int]((Get-Content -LiteralPath $PidFile -Raw).Trim())
    } catch {
        return 0
    }
}

function Start-FrontendProcess {
    $nodeExe = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
    if ([string]::IsNullOrWhiteSpace($nodeExe)) {
        throw "node.exe is missing from PATH."
    }

    $nextCli = Join-Path $WebRoot "node_modules\next\dist\bin\next"
    if (-not (Test-Path -LiteralPath $nextCli)) {
        throw "Next CLI is missing. Run npm install in $WebRoot first."
    }

    $process = Start-Process `
        -FilePath $nodeExe `
        -ArgumentList @($nextCli, "dev", "--webpack", "-H", $HostName, "-p", [string]$Port) `
        -WorkingDirectory $WebRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $FrontendStdout `
        -RedirectStandardError $FrontendStderr `
        -PassThru

    Set-Content -LiteralPath $PidFile -Value ([string]$process.Id) -Encoding ASCII
    Write-WatchdogLog "frontend_started pid=$($process.Id) url=http://$HostName`:$Port"
    return $process
}

$IntervalSeconds = [Math]::Max(5, $IntervalSeconds)
$CrashCooldownSeconds = [Math]::Max(10, $CrashCooldownSeconds)
$StartupWaitSeconds = [Math]::Max(5, $StartupWaitSeconds)
$lastStartAt = [DateTime]::MinValue

Write-WatchdogLog "watchdog_started port=$Port interval=$IntervalSeconds"

while (-not (Test-Path -LiteralPath $StopFile)) {
    $listening = Test-ListenPort $Port
    $frontendPid = Get-FrontendPidFromFile
    $pidAlive = Test-ProcessAlive $frontendPid

    if (-not $listening -and -not $pidAlive) {
        $secondsSinceStart = ([DateTime]::UtcNow - $lastStartAt).TotalSeconds
        if ($secondsSinceStart -lt $CrashCooldownSeconds) {
            Write-WatchdogLog "restart_delayed cooldown_remaining=$([Math]::Ceiling($CrashCooldownSeconds - $secondsSinceStart))"
            Start-Sleep -Seconds $IntervalSeconds
            continue
        }
        try {
            Start-FrontendProcess | Out-Null
            $lastStartAt = [DateTime]::UtcNow
            Start-Sleep -Seconds $StartupWaitSeconds
        } catch {
            Write-WatchdogLog "frontend_start_failed=$($_.Exception.Message)"
            Start-Sleep -Seconds $CrashCooldownSeconds
        }
        continue
    }

    if (-not $listening -and $pidAlive) {
        Write-WatchdogLog "frontend_process_alive_waiting_for_port pid=$frontendPid"
    }

    Start-Sleep -Seconds $IntervalSeconds
}

Write-WatchdogLog "watchdog_stopped stop_file=$StopFile"
