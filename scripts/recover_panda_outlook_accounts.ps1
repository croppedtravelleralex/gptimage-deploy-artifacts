[CmdletBinding()]
param(
    [string]$CredentialsPath = (Join-Path $HOME 'Downloads\tokens_2026-07-09.txt'),
    [string[]]$Email = @(),
    [ValidateRange(0, 1000)]
    [int]$Limit = 0,
    [switch]$DryRun,
    [switch]$NoRestart,
    [string]$LocalProxyPath = '',
    [string]$SshAlias = 'panda',
    [string]$RemoteRoot = '/root/gptimage',
    [string]$RemoteProxyFile = '/root/gptimage/data/runlogs/webshare_good_csrf_200.secret.txt',
    [string]$LocalReportDir = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-LastExitCode {
    param([string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE"
    }
}

function Assert-SafeRemotePath {
    param(
        [string]$Value,
        [string]$Name
    )
    if ($Value -notmatch '^/[A-Za-z0-9._/-]+$' -or $Value.Contains('..')) {
        throw "$Name contains unsupported characters: $Value"
    }
}

function Invoke-RemoteHealth {
    param(
        [string]$HostAlias,
        [int]$Attempts = 30
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $healthText = & ssh $HostAlias 'curl -fsS http://127.0.0.1:8012/health?format=json' 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($healthText -join "`n"))) {
            try {
                $health = ($healthText -join "`n") | ConvertFrom-Json
                if ($health.healthy) {
                    return $health
                }
            } catch {
                # 服务可能刚重启，继续轮询。
            }
        }
        Start-Sleep -Seconds 2
    }
    throw 'Panda health did not become ready after restart'
}

if ($SshAlias -notmatch '^[A-Za-z0-9_.@-]+$') {
    throw "SshAlias contains unsupported characters: $SshAlias"
}
Assert-SafeRemotePath -Value $RemoteRoot -Name 'RemoteRoot'
Assert-SafeRemotePath -Value $RemoteProxyFile -Name 'RemoteProxyFile'

$projectRoot = Split-Path -Parent $PSScriptRoot
$enginePath = Join-Path $PSScriptRoot 'recover_panda_outlook_accounts.py'
if (-not (Test-Path -LiteralPath $enginePath -PathType Leaf)) {
    throw "Recovery engine not found: $enginePath"
}
$CredentialsPath = (Resolve-Path -LiteralPath $CredentialsPath).Path
if (-not (Test-Path -LiteralPath $CredentialsPath -PathType Leaf)) {
    throw "Outlook credentials file not found: $CredentialsPath"
}
if (-not [string]::IsNullOrWhiteSpace($LocalProxyPath)) {
    $LocalProxyPath = (Resolve-Path -LiteralPath $LocalProxyPath).Path
    if (-not (Test-Path -LiteralPath $LocalProxyPath -PathType Leaf)) {
        throw "Webshare proxy file not found: $LocalProxyPath"
    }
}

$normalizedEmails = @(
    $Email |
        ForEach-Object { ([string]$_).Trim().ToLowerInvariant() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique
)
foreach ($item in $normalizedEmails) {
    if ($item -notmatch '^[^\s@]+@[^\s@]+\.[^\s@]+$') {
        throw "Invalid target email: $item"
    }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if ([string]::IsNullOrWhiteSpace($LocalReportDir)) {
    $LocalReportDir = Join-Path $projectRoot "reports\panda-outlook-manual-recovery-$stamp"
}
$LocalReportDir = [System.IO.Path]::GetFullPath($LocalReportDir)
New-Item -ItemType Directory -Force -Path $LocalReportDir | Out-Null

$remoteScript = "$RemoteRoot/scripts/recover_panda_outlook_accounts.py"
$remoteCredentials = "$RemoteRoot/data/runlogs/panda-outlook-recovery-$stamp.credentials.secret.txt"
$remoteTargets = "$RemoteRoot/data/runlogs/panda-outlook-recovery-$stamp.targets.secret.txt"
$remoteRunProxy = $RemoteProxyFile
$uploadedRunProxy = $false
if (-not [string]::IsNullOrWhiteSpace($LocalProxyPath)) {
    $remoteRunProxy = "$RemoteRoot/data/runlogs/panda-outlook-recovery-$stamp.webshare.secret.txt"
    $uploadedRunProxy = $true
}
$remoteReport = "$RemoteRoot/reports/panda-outlook-manual-recovery-$stamp"
$remoteBackup = "$RemoteRoot/backups/panda-outlook-manual-recovery-before-$stamp"
Assert-SafeRemotePath -Value $remoteScript -Name 'remoteScript'
Assert-SafeRemotePath -Value $remoteCredentials -Name 'remoteCredentials'
Assert-SafeRemotePath -Value $remoteTargets -Name 'remoteTargets'
Assert-SafeRemotePath -Value $remoteRunProxy -Name 'remoteRunProxy'
Assert-SafeRemotePath -Value $remoteReport -Name 'remoteReport'
Assert-SafeRemotePath -Value $remoteBackup -Name 'remoteBackup'

$targetTemp = $null
$summary = $null
$rows = @()
try {
    Write-Host "[1/6] 部署恢复引擎并上传本次 Outlook secret"
    & ssh $SshAlias "mkdir -p $RemoteRoot/scripts $RemoteRoot/data/runlogs $remoteReport $remoteBackup"
    Assert-LastExitCode 'Prepare remote directories'
    & scp -- $enginePath "${SshAlias}:$remoteScript"
    Assert-LastExitCode 'Upload recovery engine'
    & scp -- $CredentialsPath "${SshAlias}:$remoteCredentials"
    Assert-LastExitCode 'Upload Outlook credentials'

    if ($uploadedRunProxy) {
        & scp -- $LocalProxyPath "${SshAlias}:$remoteRunProxy"
        Assert-LastExitCode 'Upload Webshare proxies'
    }
    if ($normalizedEmails.Count -gt 0) {
        $targetTemp = Join-Path $env:TEMP "panda-outlook-recovery-$stamp.targets.txt"
        $normalizedEmails | Set-Content -LiteralPath $targetTemp -Encoding utf8
        & scp -- $targetTemp "${SshAlias}:$remoteTargets"
        Assert-LastExitCode 'Upload recovery targets'
    }

    & ssh $SshAlias "chmod 755 $remoteScript && chmod 600 $remoteCredentials $remoteRunProxy"
    Assert-LastExitCode 'Protect remote recovery files'
    if ($normalizedEmails.Count -gt 0) {
        & ssh $SshAlias "chmod 600 $remoteTargets"
        Assert-LastExitCode 'Protect remote target file'
    }

    Write-Host "[2/6] 运行恢复链：RT OTP -> 新登录 -> 新 token -> 去旧 fp -> Webshare 验证"
    $remoteArguments = @(
        "cd $RemoteRoot && PYTHONPATH=$RemoteRoot .venv/bin/python $remoteScript",
        "--root $RemoteRoot",
        "--credentials-file $remoteCredentials",
        "--proxy-file $remoteRunProxy",
        "--report-dir $remoteReport",
        "--backup-dir $remoteBackup"
    )
    if ($normalizedEmails.Count -gt 0) {
        $remoteArguments += "--target-file $remoteTargets"
    }
    if ($Limit -gt 0) {
        $remoteArguments += "--limit $Limit"
    }
    if ($DryRun) {
        $remoteArguments += '--dry-run'
    }
    $remoteCommand = $remoteArguments -join ' '
    & ssh $SshAlias $remoteCommand
    Assert-LastExitCode 'Run Panda Outlook recovery'

    Write-Host "[3/6] 拉取脱敏报告"
    & scp -- "${SshAlias}:$remoteReport/summary.json" (Join-Path $LocalReportDir 'summary.json')
    Assert-LastExitCode 'Download recovery summary'
    & scp -- "${SshAlias}:$remoteReport/rows.json" (Join-Path $LocalReportDir 'rows.json')
    Assert-LastExitCode 'Download recovery rows'
    $summary = Get-Content -LiteralPath (Join-Path $LocalReportDir 'summary.json') -Raw -Encoding utf8 | ConvertFrom-Json
    $rowsValue = Get-Content -LiteralPath (Join-Path $LocalReportDir 'rows.json') -Raw -Encoding utf8 | ConvertFrom-Json
    if ($null -ne $rowsValue) {
        $rows = @($rowsValue)
    }

    if ($DryRun) {
        Write-Host "[4/6] Dry-run 完成：selected=$($summary.selected), missing_credentials=$(@($summary.missing_credentials).Count)"
    } elseif ([int]$summary.restored -gt 0 -and -not $NoRestart) {
        Write-Host "[4/6] 重启 Panda app，让主进程加载新 token"
        & ssh $SshAlias "cd $RemoteRoot && docker compose -f docker-compose.panda.yml restart app"
        Assert-LastExitCode 'Restart Panda app'

        Write-Host "[5/6] 等待 health 恢复并核对调度面"
        $health = Invoke-RemoteHealth -HostAlias $SshAlias
        $accountHealth = $health.accounts
        Write-Host (
            'health: total={0}, schedulable={1}, dispatchable={2}, rejected={3}, quota={4}' -f
            $accountHealth.total,
            $accountHealth.schedulable,
            $accountHealth.dispatchable_candidate_count,
            $accountHealth.panda_rejected_count,
            $accountHealth.verified_total_quota
        )
    } elseif (-not $DryRun) {
        Write-Host '[4/6] 没有成功写入的新 token，跳过重启'
    }

    if (-not $DryRun) {
        $fpInherited = @($rows | Where-Object { $_.ok -and $_.old_fp_inherited }).Count
        if ($fpInherited -gt 0) {
            throw "$fpInherited restored account(s) still contain an inherited fp"
        }
        Write-Host (
            '[6/6] 完成：selected={0}, restored={1}, schedulable={2}, failed={3}, report={4}' -f
            $summary.selected,
            $summary.restored,
            $summary.schedulable,
            $summary.failed,
            $LocalReportDir
        )
        if ([int]$summary.failed -gt 0) {
            throw "$($summary.failed) account recovery item(s) failed; inspect $LocalReportDir\rows.json"
        }
    }
} finally {
    Write-Host '清理 Panda 本次 Outlook/目标/临时代理 secret'
    $cleanup = @($remoteCredentials, $remoteTargets)
    if ($uploadedRunProxy) {
        $cleanup += $remoteRunProxy
    }
    $cleanupCommand = 'rm -f ' + ($cleanup -join ' ')
    & ssh $SshAlias $cleanupCommand 2>$null | Out-Null
    if ($null -ne $targetTemp -and (Test-Path -LiteralPath $targetTemp)) {
        Remove-Item -LiteralPath $targetTemp -Force
    }
}
