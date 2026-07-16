#Requires -Version 5.1
<#
.SYNOPSIS
  ACC-010 / 审计相关变更：仅在本地 compile、pytest、前端 build。

.NOTES
  - 禁止在 Panda 上 docker build / 编译前端
  - 禁止本脚本 scp 业务代码或 restart 生产容器
  - 验收通过后：本地 git push → Panda `git pull`（或按仓库约定的 GitHub 同步流程）再 restart
#>
param(
    [switch]$SkipFrontendBuild
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "== 1) Local py_compile =="
python -m py_compile `
    services/account_fingerprint.py `
    services/openai_backend_api.py `
    services/account_service.py `
    services/image_task_service.py `
    services/account_workload_policy.py `
    scripts/panda_readonly_deep_audit.py
if ($LASTEXITCODE -ne 0) { throw "py_compile failed" }

Write-Host "== 2) Targeted pytest =="
# ACC-010 / fingerprint / transport 核心；image_task 全量另跑以便隔离偶发时序 flake
python -m pytest -q `
    test/test_account_fingerprint_and_proxy_pick.py `
    test/test_account_workload_policy.py `
    test/test_openai_backend_transport_isolation.py `
    test/test_openai_backend_session_close.py `
    test/test_image_sync_admission_eta.py `
    test/test_account_image_capabilities.py `
    --tb=line
if ($LASTEXITCODE -ne 0) { throw "pytest core failed" }

python -m pytest -q test/test_image_task_service.py --tb=line
if ($LASTEXITCODE -ne 0) {
    Write-Host "image_task_service failed once; retry once for timing flake"
    python -m pytest -q test/test_image_task_service.py --tb=line
    if ($LASTEXITCODE -ne 0) { throw "pytest image_task_service failed" }
}

if (-not $SkipFrontendBuild) {
    Write-Host "== 3) Local frontend build -> web_dist =="
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_static_frontend.ps1")
    if ($LASTEXITCODE -ne 0) { throw "frontend build failed" }
} else {
    Write-Host "== 3) Frontend build skipped =="
}

Write-Host ""
Write-Host "LOCAL BUILD OK."
Write-Host "Next (manual, after review):"
Write-Host "  1) git add / commit / push to GitHub"
Write-Host "  2) ssh panda -> cd /root/gptimage -> git pull (约定分支)"
Write-Host "  3) 仅在需要时: docker compose -f docker-compose.panda.yml up -d --force-recreate app"
Write-Host "Do NOT scp services from this script. Do NOT build on panda."
