param(
    [string]$BackendHost = "127.0.0.1",
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000,
    [string]$WslDistro = "HermesUbuntu",
    [string]$ProjectWslPath = "/mnt/d/SelfMadeTool/AutoRegister/gptimage",
    [switch]$KeepProxyStack
)

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunLogDir = Join-Path $ProjectRoot "data\runlogs"
New-Item -ItemType Directory -Path $RunLogDir -Force | Out-Null

function Stop-ListenPort([int]$Port) {
    $conns = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    foreach ($conn in $conns) {
        $ownerPid = [int]$conn.OwningProcess
        if ($ownerPid -le 0) { continue }
        try {
            Stop-Process -Id $ownerPid -Force -ErrorAction Stop
            Write-Host "stopped_listen_port=$Port pid=$ownerPid"
        } catch {
            Write-Warning "stop_listen_port_failed port=$Port pid=$ownerPid err=$($_.Exception.Message)"
        }
    }
}

function Stop-ProcessesByCommand([string]$Pattern, [string]$Label) {
    $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.CommandLine -and
            ($_.CommandLine -match $Pattern)
        })
    foreach ($proc in $procs) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Host "stopped_$Label pid=$($proc.ProcessId)"
        } catch {
            Write-Warning "stop_${Label}_failed pid=$($proc.ProcessId) err=$($_.Exception.Message)"
        }
    }
}

# 1) Signal watchdogs to exit cleanly (avoid immediate restart)
foreach ($name in @("backend-watchdog.stop", "frontend-watchdog.stop")) {
    $stopFile = Join-Path $RunLogDir $name
    Set-Content -LiteralPath $stopFile -Value (Get-Date -Format "o") -Encoding ASCII
    Write-Host "stop_file_written=$stopFile"
}

Start-Sleep -Seconds 2

# 2) Force-stop remaining local processes
Stop-ProcessesByCommand '(?i)watch_backend\.ps1' "backend_watchdog"
Stop-ProcessesByCommand '(?i)watch_frontend\.ps1' "frontend_watchdog"
Stop-ProcessesByCommand '(?i)warp_health_monitor\.ps1' "proxy_monitor"
Stop-ProcessesByCommand '(?i)warp_health_monitor\.cmd(?:\s|$)' "proxy_monitor_cmd"
Stop-ProcessesByCommand '(?i)host_proxy_forwarder\.py' "host_proxy_forwarder"
Stop-ProcessesByCommand '(?i)gptimage-wsl-keepalive' "wsl_keepalive"

Stop-ListenPort $BackendPort
Stop-ListenPort $FrontendPort
# Windows 兜底转发也可能占 40080，一并释放
Stop-ListenPort 40080

# uvicorn / python leftover by pid file
$backendPidFile = Join-Path $RunLogDir "backend.pid"
if (Test-Path -LiteralPath $backendPidFile) {
    try {
        $backendPid = [int]((Get-Content -LiteralPath $backendPidFile -Raw).Trim())
        if ($backendPid -gt 0 -and (Get-Process -Id $backendPid -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $backendPid -Force -ErrorAction SilentlyContinue
            Write-Host "stopped_backend_pidfile pid=$backendPid"
        }
    } catch {}
    Remove-Item -LiteralPath $backendPidFile -Force -ErrorAction SilentlyContinue
}

# 3) WSL docker proxy stack
if (-not $KeepProxyStack) {
    $wslExe = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if ($null -eq $wslExe) {
        Write-Warning "wsl_not_found skip_proxy_stack_stop"
    } else {
        Write-Host "stopping_proxy_stack distro=$WslDistro"
        & $wslExe.Source -d $WslDistro -- bash -lc "cd '$ProjectWslPath' && docker compose -f docker-compose.warp.yml down"
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "proxy_stack_stop_exit=$LASTEXITCODE"
        } else {
            Write-Host "proxy_stack_stopped"
        }
    }
} else {
    Write-Host "proxy_stack_kept"
}

Start-Sleep -Seconds 2

# 4) Status summary
function Port-Open([int]$Port) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}

$backendOpen = Port-Open $BackendPort
$proxyOpen = Port-Open 40080
Write-Host "status backend_8000=$backendOpen proxy_40080=$proxyOpen"

if ($backendOpen) {
    throw "backend still listening on $BackendPort"
}
if (-not $KeepProxyStack -and ($proxyOpen -or $flareOpen)) {
    # 再清一次兜底转发 / 残留监听
    Stop-ProcessesByCommand '(?i)host_proxy_forwarder\.py' "host_proxy_forwarder_retry"
    Stop-ListenPort 40080
        Start-Sleep -Seconds 1
    $proxyOpen = Port-Open 40080
        Write-Host "status_retry proxy_40080=$proxyOpen"
    if ($proxyOpen -or $flareOpen) {
        throw "proxy ports still listening after stop: proxy_40080=$proxyOpen"
    }
}

Write-Host "local_stack_stopped"
