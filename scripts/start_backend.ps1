param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$NoWatchdog
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunLogDir = Join-Path $ProjectRoot "data\runlogs"
New-Item -ItemType Directory -Path $RunLogDir -Force | Out-Null
$WatchdogScript = Join-Path $PSScriptRoot "watch_backend.ps1"
$WatchdogLog = Join-Path $RunLogDir "backend-watchdog.log"
$WatchdogStdout = Join-Path $RunLogDir "backend-watchdog.out.log"
$WatchdogStderr = Join-Path $RunLogDir "backend-watchdog.err.log"

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
            $_.CommandLine.Contains("watch_backend.ps1") -and
            $_.CommandLine.Contains("-Port $Port")
        } |
        Select-Object -First 1
    if ($existingWatchdog) {
        Write-Host "backend_watchdog_already_running pid=$($existingWatchdog.ProcessId)"
        exit 0
    }

    Remove-Item -LiteralPath (Join-Path $RunLogDir "backend-watchdog.stop") -Force -ErrorAction SilentlyContinue
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
    Write-Host "backend_watchdog_started pid=$($watchdog.Id) url=http://$HostName`:$Port"
    Write-Host "backend_watchdog_log=$WatchdogLog"
    exit 0
}

if (Test-ListenPort $Port) {
    Write-Host "backend_already_running=http://$HostName`:$Port"
    exit 0
}

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
$stdout = Join-Path $RunLogDir "backend.out.log"
$stderr = Join-Path $RunLogDir "backend.err.log"

$process = Start-Process `
    -FilePath $filePath `
    -ArgumentList $arguments `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

Write-Host "backend_started pid=$($process.Id) url=http://$HostName`:$Port"
Write-Host "backend_logs=$stdout"
