param(
    [string]$SshHost = "panda",
    [string]$LocalAccountsPath = "",
    [string]$RemoteDir = "/root/gptimage",
    [string]$StatePath = "",
    [int]$BatchSize = 20,
    [int]$MaxAccountsPerRun = 20,
    [switch]$InitializeOnly,
    [switch]$ForceAll
)

$ErrorActionPreference = "Stop"

# SSH connect/keepalive options so scp/ssh can't hang the run indefinitely
$SshOpts = @('-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2')

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($LocalAccountsPath)) {
    $snapshotPath = Join-Path $env:TEMP ("accounts-storage-snapshot.{0}.json" -f ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()))
    $exportScript = Join-Path $PSScriptRoot "export_accounts_snapshot.py"
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }
    if (Test-Path -LiteralPath $exportScript) {
        & $python $exportScript --output $snapshotPath | Write-Host
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $snapshotPath)) {
            $LocalAccountsPath = $snapshotPath
        } else {
            throw "Failed to export current storage snapshot; refusing to fall back to stale data\accounts.json"
        }
    }
    if ([string]::IsNullOrWhiteSpace($LocalAccountsPath)) {
        $LocalAccountsPath = Join-Path $ProjectRoot "data\accounts.json"
    }
}
if ([string]::IsNullOrWhiteSpace($StatePath)) {
    $StatePath = Join-Path $ProjectRoot "data\panda-sync-delta-state.json"
}

function Get-Sha256Hex([string]$Value) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") })
    } finally {
        $sha.Dispose()
    }
}

function Get-FileSha256Hex([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            return -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString("x2") })
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha.Dispose()
    }
}

function Write-State([string[]]$TokenHashes, [string]$LocalHash, [int]$LocalCount, [string]$Mode) {
    $dir = Split-Path -Parent $StatePath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    @{
        synced_token_hashes = @($TokenHashes | Sort-Object -Unique)
        last_success_sha256 = $LocalHash
        last_success_at = (Get-Date).ToString("o")
        local_count = $LocalCount
        mode = $Mode
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

function ConvertTo-NativeArgument([string]$Argument) {
    if ($null -eq $Argument) { return '""' }
    $value = [string]$Argument
    if ($value.Length -eq 0) { return '""' }
    if ($value -notmatch '[\s"]') { return $value }

    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashCount = 0
    foreach ($ch in $value.ToCharArray()) {
        if ($ch -eq [char]92) {
            $backslashCount += 1
            continue
        }
        if ($ch -eq [char]34) {
            if ($backslashCount -gt 0) {
                [void]$builder.Append(('\' * ($backslashCount * 2)))
            }
            [void]$builder.Append('\"')
            $backslashCount = 0
            continue
        }
        if ($backslashCount -gt 0) {
            [void]$builder.Append(('\' * $backslashCount))
            $backslashCount = 0
        }
        [void]$builder.Append($ch)
    }
    if ($backslashCount -gt 0) {
        [void]$builder.Append(('\' * ($backslashCount * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-HiddenNative {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$StandardInput
    )

    $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = $FilePath
    $processInfo.Arguments = (($ArgumentList | ForEach-Object { ConvertTo-NativeArgument ([string]$_) }) -join " ")
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    if ($PSBoundParameters.ContainsKey("StandardInput")) {
        $processInfo.RedirectStandardInput = $true
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $processInfo
    try {
        if (-not $process.Start()) {
            throw "Failed to start $FilePath"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($processInfo.RedirectStandardInput) {
            $process.StandardInput.Write($StandardInput)
            $process.StandardInput.Close()
        }
        $process.WaitForExit()
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        if (-not [string]::IsNullOrWhiteSpace($stdout)) {
            Write-Host ($stdout.TrimEnd())
        }
        if (-not [string]::IsNullOrWhiteSpace($stderr)) {
            Write-Host ($stderr.TrimEnd())
        }
        return $process.ExitCode
    } finally {
        $process.Dispose()
    }
}

if ($BatchSize -lt 1) { throw "BatchSize must be >= 1" }
if ($MaxAccountsPerRun -lt 1) { throw "MaxAccountsPerRun must be >= 1" }
if (-not (Test-Path -LiteralPath $LocalAccountsPath)) {
    throw "Local accounts file not found: $LocalAccountsPath"
}

$raw = Get-Content -LiteralPath $LocalAccountsPath -Raw -Encoding UTF8
$accounts = $raw | ConvertFrom-Json
if ($null -eq $accounts) { throw "accounts.json is empty" }
if (-not ($accounts -is [System.Array])) { $accounts = @($accounts) }
if ($accounts.Count -le 0) { throw "accounts.json is empty" }

$localHash = Get-FileSha256Hex $LocalAccountsPath
$localBytes = (Get-Item -LiteralPath $LocalAccountsPath).Length
$tokenHashes = New-Object 'System.Collections.Generic.List[string]'
$byHash = @{}
foreach ($account in $accounts) {
    $token = [string]$account.access_token
    if ([string]::IsNullOrWhiteSpace($token)) { continue }
    $fingerprint = Get-Sha256Hex $token
    $tokenHashes.Add($fingerprint)
    if (-not $byHash.ContainsKey($fingerprint)) {
        $byHash[$fingerprint] = $account
    }
}

Write-Host "local_count=$($accounts.Count) token_count=$($byHash.Count) local_bytes=$localBytes local_sha256=$localHash"

if ($InitializeOnly) {
    Write-State -TokenHashes $tokenHashes.ToArray() -LocalHash $localHash -LocalCount $accounts.Count -Mode "initialize"
    Write-Host "initialized_state=$StatePath"
    exit 0
}

$synced = @{}
if ((-not $ForceAll) -and (Test-Path -LiteralPath $StatePath)) {
    try {
        $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($hash in @($state.synced_token_hashes)) {
            if ($hash) { $synced[[string]$hash] = $true }
        }
    } catch {
        $synced = @{}
    }
}

$pending = New-Object 'System.Collections.Generic.List[object]'
foreach ($hash in $byHash.Keys) {
    if ($ForceAll -or (-not $synced.ContainsKey($hash))) {
        $pending.Add($byHash[$hash])
    }
}

if ($pending.Count -eq 0) {
    Write-State -TokenHashes $tokenHashes.ToArray() -LocalHash $localHash -LocalCount $accounts.Count -Mode "skip-no-new-token"
    Write-Host "skip=no_new_tokens"
    exit 0
}

Write-Host "pending_new_accounts=$($pending.Count) batch_size=$BatchSize"

if ($pending.Count -gt $MaxAccountsPerRun) {
    $pending = [System.Collections.Generic.List[object]]::new([object[]]@($pending[0..($MaxAccountsPerRun - 1)]))
    Write-Host "throttled_this_run=$($pending.Count) max_accounts_per_run=$MaxAccountsPerRun"
}

$allSynced = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($hash in $synced.Keys) { [void]$allSynced.Add($hash) }

for ($offset = 0; $offset -lt $pending.Count; $offset += $BatchSize) {
    $end = [Math]::Min($offset + $BatchSize - 1, $pending.Count - 1)
    $batch = @($pending[$offset..$end])
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $payloadPath = Join-Path $env:TEMP "accounts-delta.$stamp.$offset.json"
    $gzipPath = "$payloadPath.gz"
    $b64Path = "$gzipPath.b64"
    $remoteB64 = "/tmp/accounts-delta.$stamp.$offset.json.gz.b64"
    $remoteGz = "/tmp/accounts-delta.$stamp.$offset.json.gz"
    $remoteJson = "/tmp/accounts-delta.$stamp.$offset.json"
    $remoteResp = "/tmp/accounts-delta.$stamp.$offset.response.json"

    try {
        @{ accounts = @($batch) } |
            ConvertTo-Json -Depth 100 -Compress |
            Set-Content -LiteralPath $payloadPath -Encoding UTF8

        $source = [System.IO.File]::OpenRead($payloadPath)
        try {
            $target = [System.IO.File]::Create($gzipPath)
            try {
                $gzip = [System.IO.Compression.GzipStream]::new($target, [System.IO.Compression.CompressionLevel]::Optimal)
                try {
                    $source.CopyTo($gzip)
                } finally {
                    $gzip.Dispose()
                }
            } finally {
                $target.Dispose()
            }
        } finally {
            $source.Dispose()
        }

        [System.IO.File]::WriteAllText(
            $b64Path,
            [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($gzipPath)),
            [System.Text.Encoding]::ASCII
        )

        $scpExitCode = Invoke-HiddenNative `
            -FilePath "scp.exe" `
            -ArgumentList (@("-C") + $SshOpts + @($b64Path, "${SshHost}:$remoteB64"))
        if ($scpExitCode -ne 0) {
            throw "Delta upload failed with exit code $scpExitCode"
        }

        $remoteScript = @'
set -euo pipefail
export REMOTE_DIR='__REMOTE_DIR__'
export REMOTE_B64='__REMOTE_B64__'
export REMOTE_GZ='__REMOTE_GZ__'
export REMOTE_JSON='__REMOTE_JSON__'
export REMOTE_RESP='__REMOTE_RESP__'
cd "$REMOTE_DIR"
python3 - <<'PY'
import base64, gzip, json, os
from pathlib import Path

b64 = Path(os.environ["REMOTE_B64"])
gz = Path(os.environ["REMOTE_GZ"])
payload = Path(os.environ["REMOTE_JSON"])
gz.write_bytes(base64.b64decode(b64.read_text(encoding="ascii").strip()))
payload.write_bytes(gzip.decompress(gz.read_bytes()))
data = json.loads(payload.read_text(encoding="utf-8-sig"))
accounts = data.get("accounts")
if not isinstance(accounts, list) or not accounts:
    raise SystemExit("payload has no accounts")
print(f"remote_delta_count={len(accounts)}")
PY
AUTH_KEY="$(python3 - <<'PY'
import json
print(str(json.load(open("config.json", encoding="utf-8")).get("auth-key") or ""))
PY
)"
if [ -z "$AUTH_KEY" ]; then
  echo "missing auth-key in config.json" >&2
  exit 1
fi
HTTP_CODE="$(curl -sS -o "$REMOTE_RESP" -w '%{http_code}' \
  'http://127.0.0.1:8012/api/accounts/import-batch?include_items=false' \
  -H "Authorization: Bearer $AUTH_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary "@$REMOTE_JSON")"
if [ "$HTTP_CODE" -lt 200 ] || [ "$HTTP_CODE" -ge 300 ]; then
  echo "api_http_code=$HTTP_CODE" >&2
  head -c 500 "$REMOTE_RESP" >&2 || true
  exit 1
fi
python3 - <<'PY'
import json, os
resp = json.load(open(os.environ["REMOTE_RESP"], encoding="utf-8"))
print(
    "api_added={added} api_skipped={skipped} api_updated={updated}".format(
        added=resp.get("added"),
        skipped=resp.get("skipped"),
        updated=resp.get("updated"),
    )
)
PY
rm -f "$REMOTE_B64" "$REMOTE_GZ" "$REMOTE_JSON" "$REMOTE_RESP"
'@
        $remoteScript = $remoteScript.Replace("__REMOTE_DIR__", $RemoteDir)
        $remoteScript = $remoteScript.Replace("__REMOTE_B64__", $remoteB64)
        $remoteScript = $remoteScript.Replace("__REMOTE_GZ__", $remoteGz)
        $remoteScript = $remoteScript.Replace("__REMOTE_JSON__", $remoteJson)
        $remoteScript = $remoteScript.Replace("__REMOTE_RESP__", $remoteResp)
        $remoteScript = $remoteScript -replace "`r", ""
        $sshExitCode = Invoke-HiddenNative `
            -FilePath "ssh.exe" `
            -ArgumentList (@($SshOpts) + @($SshHost, "bash -s")) `
            -StandardInput $remoteScript
        if ($sshExitCode -ne 0) {
            throw "Remote delta import failed with exit code $sshExitCode"
        }

        foreach ($account in $batch) {
            $token = [string]$account.access_token
            if (-not [string]::IsNullOrWhiteSpace($token)) {
                [void]$allSynced.Add((Get-Sha256Hex $token))
            }
        }
        $allSyncedArray = foreach ($hash in $allSynced) { [string]$hash }
        Write-State -TokenHashes $allSyncedArray -LocalHash $localHash -LocalCount $accounts.Count -Mode "delta"
    } finally {
        Remove-Item -LiteralPath $payloadPath, $gzipPath, $b64Path -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "sync_complete=delta synced_new=$($pending.Count)"
