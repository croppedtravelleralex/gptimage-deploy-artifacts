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

if ($BackupDir) {
    Write-Host "web_dist_backup=$BackupDir"
}
Write-Host "web_dist_updated=$WebDistDir"
