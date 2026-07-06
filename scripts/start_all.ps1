param(
    [switch]$BackendOnly,
    [switch]$WithFrontend,
    [switch]$SkipStaticFrontendBuild,
    [switch]$SkipProxyStack,
    [switch]$SkipProxyMonitor,
    [switch]$RequireProxyStack,
    [string]$BackendHost = "127.0.0.1",
    [int]$BackendPort = 8000,
    [string]$FrontendHost = "127.0.0.1",
    [int]$FrontendPort = 3000,
    [string]$WslDistro = "HermesUbuntu",
    [string]$ProjectWslPath = "/mnt/d/SelfMadeTool/AutoRegister/gptimage",
    [int]$ProxyPort = 40080,
    [int]$FlareSolverrPort = 8191
)

$ErrorActionPreference = "Stop"

if (-not $SkipProxyStack) {
    try {
        & (Join-Path $PSScriptRoot "start_proxy_stack.ps1") `
            -WslDistro $WslDistro `
            -ProjectWslPath $ProjectWslPath `
            -ProxyPort $ProxyPort `
            -FlareSolverrPort $FlareSolverrPort
    } catch {
        if ($RequireProxyStack) {
            throw
        }
        Write-Warning "proxy_stack_start_failed=$($_.Exception.Message)"
    }
} else {
    Write-Host "proxy_stack_skipped"
}

if (-not $SkipProxyMonitor) {
    & (Join-Path $PSScriptRoot "start_proxy_monitor.ps1")
} else {
    Write-Host "proxy_monitor_skipped"
}

& (Join-Path $PSScriptRoot "start_backend.ps1") -HostName $BackendHost -Port $BackendPort

if ($WithFrontend -and -not $BackendOnly) {
    & (Join-Path $PSScriptRoot "start_frontend.ps1") -HostName $FrontendHost -Port $FrontendPort
} else {
    if (-not $SkipStaticFrontendBuild) {
        & (Join-Path $PSScriptRoot "build_static_frontend.ps1") -SkipIfPresent
    }
    Write-Host "frontend_dev_skipped=backend_serves_web_dist"
}

Write-Host "local_dashboard=http://$BackendHost`:$BackendPort"
