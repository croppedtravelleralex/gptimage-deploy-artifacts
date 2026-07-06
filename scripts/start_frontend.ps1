param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 3000,
    [switch]$NoWatchdog
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$WebRoot = Join-Path $ProjectRoot "web"
$RunLogDir = Join-Path $ProjectRoot "data\runlogs"
New-Item -ItemType Directory -Path $RunLogDir -Force | Out-Null
$WatchdogScript = Join-Path $PSScriptRoot "watch_frontend.ps1"
$WatchdogLog = Join-Path $RunLogDir "frontend-watchdog.log"
$WatchdogStdout = Join-Path $RunLogDir "frontend-watchdog.out.log"
$WatchdogStderr = Join-Path $RunLogDir "frontend-watchdog.err.log"

function Test-ListenPort([int]$PortToCheck) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort $PortToCheck -State Listen -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}

if (-not $NoWatchdog) {
    $existingWatchdog = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' OR Name = 'pwsh.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.Contains("watch_frontend.ps1") -and
            $_.CommandLine.Contains("-Port $Port")
        } |
        Select-Object -First 1
    if ($existingWatchdog) {
        Write-Host "frontend_watchdog_already_running pid=$($existingWatchdog.ProcessId)"
        exit 0
    }

    Remove-Item -LiteralPath (Join-Path $RunLogDir "frontend-watchdog.stop") -Force -ErrorAction SilentlyContinue
    $watchdog = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden",
            "-File", $WatchdogScript,
            "-HostName", $HostName,
            "-Port", [string]$Port
        ) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $WatchdogStdout `
        -RedirectStandardError $WatchdogStderr `
        -PassThru
    Write-Host "frontend_watchdog_started pid=$($watchdog.Id) url=http://$HostName`:$Port"
    Write-Host "frontend_watchdog_log=$WatchdogLog"
    exit 0
}

if (Test-ListenPort $Port) {
    Write-Host "frontend_already_running=http://$HostName`:$Port"
    exit 0
}

$nodeExe = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
if ([string]::IsNullOrWhiteSpace($nodeExe)) {
    throw "node.exe is missing from PATH."
}

$nextCli = Join-Path $WebRoot "node_modules\next\dist\bin\next"
if (-not (Test-Path -LiteralPath $nextCli)) {
    throw "Next CLI is missing. Run npm install in $WebRoot first."
}

$stdout = Join-Path $RunLogDir "frontend.out.log"
$stderr = Join-Path $RunLogDir "frontend.err.log"

$process = Start-Process `
    -FilePath $nodeExe `
    -ArgumentList @($nextCli, "dev", "--webpack", "-H", $HostName, "-p", [string]$Port) `
    -WorkingDirectory $WebRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

Write-Host "frontend_started pid=$($process.Id) url=http://$HostName`:$Port"
Write-Host "frontend_logs=$stdout"
