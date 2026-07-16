param(
    [switch]$SkipIfPresent
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$WebRoot = Join-Path $ProjectRoot "web"
$OutputDir = Join-Path $WebRoot "out"
$WebDistDir = Join-Path $ProjectRoot "web_dist"
$ProjectRootFull = [System.IO.Path]::GetFullPath($ProjectRoot)
$OutputDirFull = [System.IO.Path]::GetFullPath($OutputDir)
$WebDistDirFull = [System.IO.Path]::GetFullPath($WebDistDir)

function Assert-ChildPath([string]$PathToCheck, [string]$BasePath, [string]$Label) {
    $full = [System.IO.Path]::GetFullPath($PathToCheck)
    $base = [System.IO.Path]::GetFullPath($BasePath).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label resolved outside project root: $full"
    }
}

Assert-ChildPath $OutputDirFull $ProjectRootFull "OutputDir"
Assert-ChildPath $WebDistDirFull $ProjectRootFull "WebDistDir"

if ($SkipIfPresent -and (Test-Path -LiteralPath (Join-Path $WebDistDir "index.html"))) {
    Write-Host "web_dist_present=$WebDistDir"
    exit 0
}

if (-not (Test-Path -LiteralPath $WebRoot)) {
    throw "web directory is missing: $WebRoot"
}

if (-not (Test-Path -LiteralPath (Join-Path $WebRoot "node_modules\next\dist\bin\next"))) {
    throw "Next CLI is missing. Run npm install in $WebRoot first."
}

Push-Location $WebRoot
try {
    & npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "frontend build failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath (Join-Path $OutputDir "index.html"))) {
    throw "Next static output is missing: $OutputDir"
}

$BackupDir = $null
if (Test-Path -LiteralPath $WebDistDir) {
    $BackupDir = Join-Path $ProjectRoot ("web_dist.backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    Assert-ChildPath $BackupDir $ProjectRootFull "BackupDir"
    Move-Item -LiteralPath $WebDistDir -Destination $BackupDir
}

New-Item -ItemType Directory -Path $WebDistDir -Force | Out-Null
Copy-Item -Path (Join-Path $OutputDir "*") -Destination $WebDistDir -Recurse -Force

# Build manifest for version drift checks (plan.md P1 / E02)
$gitCommit = ""
try {
    $gitCommit = (& git -C $ProjectRoot rev-parse --short HEAD 2>$null | Out-String).Trim()
} catch {}
$accountsHtml = Join-Path $WebDistDir "accounts\index.html"
$chunkHint = ""
if (Test-Path -LiteralPath $accountsHtml) {
    $html = Get-Content -LiteralPath $accountsHtml -Raw -ErrorAction SilentlyContinue
    if ($html -match '(/_next/static/[^"\s]+\.js)') {
        $chunkHint = $Matches[1]
    }
}
function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
$manifest = [ordered]@{
    built_at         = (Get-Date).ToUniversalTime().ToString("o")
    git_commit       = $gitCommit
    project_root     = $ProjectRoot
    accounts_chunk   = $chunkHint
    index_html_sha256 = Get-FileSha256 (Join-Path $WebDistDir "index.html")
    accounts_html_sha256 = Get-FileSha256 $accountsHtml
}
$manifestPath = Join-Path $WebDistDir "web_dist-manifest.json"
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding utf8
Write-Host "web_dist_manifest=$manifestPath"

if ($BackupDir) {
    Write-Host "web_dist_backup=$BackupDir"
}
Write-Host "web_dist_updated=$WebDistDir"
