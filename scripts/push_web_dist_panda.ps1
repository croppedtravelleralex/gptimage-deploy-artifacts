param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Tarball = Join-Path $env:TEMP "web_dist-deploy.tgz"

if (-not $SkipBuild) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_static_frontend.ps1")
}

Push-Location (Join-Path $ProjectRoot "web_dist")
try {
    if (Test-Path -LiteralPath $Tarball) { Remove-Item -LiteralPath $Tarball -Force }
    & tar -czf $Tarball .
} finally {
    Pop-Location
}

scp $Tarball panda:/tmp/web_dist-deploy.tgz
scp (Join-Path $PSScriptRoot "deploy_web_dist_panda.sh") panda:/root/gptimage/scripts/deploy_web_dist_panda.sh
ssh panda "chmod +x /root/gptimage/scripts/deploy_web_dist_panda.sh && /root/gptimage/scripts/deploy_web_dist_panda.sh /tmp/web_dist-deploy.tgz"
Remove-Item -LiteralPath $Tarball -Force -ErrorAction SilentlyContinue
