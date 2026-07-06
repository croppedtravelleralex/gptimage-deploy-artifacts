param(
    [string]$WslDistro = "HermesUbuntu",
    [string]$ProjectWslPath = "/mnt/d/SelfMadeTool/AutoRegister/gptimage",
    [int]$ProxyPort = 40080,
    [int]$FlareSolverrPort = 8191,
    [int]$TimeoutSeconds = 120,
    [switch]$AllowDockerStart
)

$ErrorActionPreference = "Stop"

function Test-ListenPort([int]$PortToCheck) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect("127.0.0.1", $PortToCheck, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(1000)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-ListenPort([int]$PortToCheck, [int]$Timeout) {
    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $Timeout))
    while ((Get-Date) -lt $deadline) {
        if (Test-ListenPort $PortToCheck) {
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Invoke-WslPreflight([string]$Script) {
    $output = & $wslExe.Source -d $WslDistro -- bash -lc $Script "gptimage-probe" $ProjectWslPath 2>&1
    return [pscustomobject]@{
        ExitCode = [int]$LASTEXITCODE
        Output = (($output | Out-String).Trim())
    }
}

function Get-WslPreflightFailure([int]$ExitCode) {
    switch ($ExitCode) {
        11 { return "project path '$ProjectWslPath' is not available in WSL" }
        12 { return "docker command is not available in WSL" }
        13 { return "docker daemon is not available in WSL; safe mode will not auto-start dockerd" }
        14 { return "docker compose plugin is not available in WSL" }
        default { return "WSL preflight failed with exit code $ExitCode" }
    }
}

if ((Test-ListenPort $ProxyPort) -and (Test-ListenPort $FlareSolverrPort)) {
    Write-Host "proxy_stack_already_running=proxy:$ProxyPort flaresolverr:$FlareSolverrPort"
    exit 0
}

$wslExe = Get-Command wsl.exe -ErrorAction SilentlyContinue
if (-not $wslExe) {
    throw "wsl.exe not found; cannot start local proxy stack."
}

$projectProbe = Invoke-WslPreflight 'test -d "$1" || exit 11; printf WSL_OK'
if ($projectProbe.ExitCode -ne 0) {
    throw "$(Get-WslPreflightFailure $projectProbe.ExitCode): $($projectProbe.Output)"
}

$dockerProbeScript = if ($AllowDockerStart) {
    'command -v docker >/dev/null 2>&1 || exit 12; docker compose version >/dev/null 2>&1 || exit 14; if docker info >/dev/null 2>&1; then printf DOCKER_OK; else printf DOCKER_START_ALLOWED; fi'
} else {
    'command -v docker >/dev/null 2>&1 || exit 12; docker info >/dev/null 2>&1 || exit 13; docker compose version >/dev/null 2>&1 || exit 14; printf DOCKER_OK'
}
$dockerProbe = Invoke-WslPreflight $dockerProbeScript
if ($dockerProbe.ExitCode -ne 0) {
    throw "$(Get-WslPreflightFailure $dockerProbe.ExitCode): $($dockerProbe.Output)"
}

$wslScriptPath = "$ProjectWslPath/scripts/start_proxy_stack_wsl.sh"
if ($AllowDockerStart) {
    # Windows environment variables are not reliably inherited by WSL child
    # processes in all launch contexts. Pass the flag inside WSL explicitly.
    & $wslExe.Source -d $WslDistro -- env GPTIMAGE_WSL_ALLOW_DOCKER_START=true bash $wslScriptPath $ProjectWslPath
} else {
    & $wslExe.Source -d $WslDistro -- bash $wslScriptPath $ProjectWslPath
}
if ($LASTEXITCODE -ne 0) {
    throw "proxy stack start failed in WSL distro '$WslDistro' with exit code $LASTEXITCODE"
}

$proxyReady = Wait-ListenPort $ProxyPort $TimeoutSeconds
$flareReady = Wait-ListenPort $FlareSolverrPort $TimeoutSeconds
if (-not ($proxyReady -and $flareReady)) {
    throw "proxy stack ports not ready: proxy:$ProxyPort=$proxyReady flaresolverr:$FlareSolverrPort=$flareReady"
}

Write-Host "proxy_stack_started=proxy:$ProxyPort flaresolverr:$FlareSolverrPort"
