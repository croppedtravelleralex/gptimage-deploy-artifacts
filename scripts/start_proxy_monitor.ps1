param()

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$MonitorScript = Join-Path $PSScriptRoot "warp_health_monitor.ps1"
if (-not (Test-Path -LiteralPath $MonitorScript)) {
    throw "proxy monitor not found: $MonitorScript"
}

$existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.ProcessId -ne $PID -and
        ($_.CommandLine -like "*warp_health_monitor.ps1*" -or $_.CommandLine -like "*warp_health_monitor.cmd*")
    } |
    Select-Object -First 1

if ($existing) {
    Write-Host "proxy_monitor_already_running pid=$($existing.ProcessId)"
    exit 0
}

$process = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$MonitorScript`"") `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -PassThru

Write-Host "proxy_monitor_started pid=$($process.Id)"
