param(
    [string]$WslDistro = "HermesUbuntu",
    [string]$ProjectWslPath = "/mnt/d/SelfMadeTool/AutoRegister/gptimage",
    [int]$ProxyPort = 40080,
    [int]$FlareSolverrPort = 8191,
    [string]$OpenAIProbeUri = "https://auth.openai.com/",
    [string]$ResourceProbeUri = "https://api.tempmail.lol/",
    [int]$HealthySleepSeconds = 60,
    [int]$RestartSleepSeconds = 30
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunLogDir = Join-Path $ProjectRoot "data\runlogs"
New-Item -ItemType Directory -Path $RunLogDir -Force | Out-Null
$LogPath = Join-Path $RunLogDir "proxy-monitor.log"

function Write-MonitorLog([string]$Message) {
    $line = "{0} {1}" -f (Get-Date).ToString("o"), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
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
        [string[]]$ArgumentList = @()
    )

    $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = $FilePath
    $processInfo.Arguments = (($ArgumentList | ForEach-Object { ConvertTo-NativeArgument ([string]$_) }) -join " ")
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $processInfo
    try {
        if (-not $process.Start()) {
            throw "Failed to start $FilePath"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        if (-not [string]::IsNullOrWhiteSpace($stdout)) {
            Write-MonitorLog ("stdout={0}" -f ($stdout.TrimEnd() -replace "`r?`n", " | "))
        }
        if (-not [string]::IsNullOrWhiteSpace($stderr)) {
            Write-MonitorLog ("stderr={0}" -f ($stderr.TrimEnd() -replace "`r?`n", " | "))
        }
        return $process.ExitCode
    } finally {
        $process.Dispose()
    }
}

function Invoke-HiddenNativeOutput {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [int]$TimeoutMs = 120000,
        [bool]$LogOutput = $false
    )

    $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = $FilePath
    $processInfo.Arguments = (($ArgumentList | ForEach-Object { ConvertTo-NativeArgument ([string]$_) }) -join " ")
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $processInfo
    try {
        if (-not $process.Start()) {
            throw "Failed to start $FilePath"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $timedOut = -not $process.WaitForExit($TimeoutMs)
        if ($timedOut) {
            try { $process.Kill() } catch {}
            try { $process.WaitForExit(5000) | Out-Null } catch {}
        }
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        if ($LogOutput -and -not [string]::IsNullOrWhiteSpace($stdout)) {
            Write-MonitorLog ("stdout={0}" -f ($stdout.TrimEnd() -replace "`r?`n", " | "))
        }
        if ($LogOutput -and -not [string]::IsNullOrWhiteSpace($stderr)) {
            Write-MonitorLog ("stderr={0}" -f ($stderr.TrimEnd() -replace "`r?`n", " | "))
        }
        return [pscustomobject]@{
            ExitCode = $(if ($timedOut) { -1 } else { $process.ExitCode })
            Stdout = [string]$stdout
            Stderr = [string]$stderr
            TimedOut = [bool]$timedOut
        }
    } finally {
        $process.Dispose()
    }
}

function Get-HttpProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [int]$TimeoutMs = 10000,
        [string]$ProxyUrl = ""
    )
    try {
        $request = [System.Net.HttpWebRequest]::Create($Uri)
        $request.Method = "GET"
        $request.AllowAutoRedirect = $false
        $request.Timeout = $TimeoutMs
        $request.ReadWriteTimeout = $TimeoutMs
        $request.UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        if ([string]::IsNullOrWhiteSpace($ProxyUrl)) {
            $request.Proxy = $null
        } else {
            $request.Proxy = [System.Net.WebProxy]::new($ProxyUrl)
        }
        $response = $request.GetResponse()
        try {
            return [pscustomobject]@{ Code = [int]$response.StatusCode; Error = ""; Uri = $Uri }
        } finally {
            $response.Close()
        }
    } catch [System.Net.WebException] {
        $message = $_.Exception.Message
        if ($_.Exception.Response) {
            $response = $_.Exception.Response
            try {
                return [pscustomobject]@{ Code = [int]$response.StatusCode; Error = $message; Uri = $Uri }
            } finally {
                $response.Close()
            }
        }
        return [pscustomobject]@{ Code = 0; Error = $message; Uri = $Uri }
    } catch {
        return [pscustomobject]@{ Code = 0; Error = $_.Exception.Message; Uri = $Uri }
    }
}

function Get-HttpStatusCode([string]$Uri, [int]$TimeoutMs = 10000) {
    $probe = Get-HttpProbe -Uri $Uri -TimeoutMs $TimeoutMs
    return [int]$probe.Code
}

function Get-ProxiedHttpProbe([string]$Uri, [int]$ProxyPort, [int]$TimeoutMs = 15000) {
    return Get-HttpProbe -Uri $Uri -TimeoutMs $TimeoutMs -ProxyUrl "http://127.0.0.1:$ProxyPort"
}

function Test-ReachableHttpCode([int]$Code) {
    return ($Code -ge 200 -and $Code -lt 500)
}

function Format-ProbeError($Probe) {
    $errorText = [string]($Probe.Error)
    if ([string]::IsNullOrWhiteSpace($errorText)) {
        return ""
    }
    return ($errorText -replace "`r?`n", " " -replace "\s+", " ").Trim()
}

function Get-WarpCliStatus {
    $command = "docker exec chatgpt2api-warp-proxy warp-cli status 2>&1"
    $result = Invoke-HiddenNativeOutput `
        -FilePath "wsl.exe" `
        -ArgumentList @("-d", $WslDistro, "--", "bash", "-lc", $command) `
        -TimeoutMs 20000 `
        -LogOutput $false
    $text = (($result.Stdout + "`n" + $result.Stderr).Trim())
    $flat = ($text -replace "`r?`n", " | " -replace "\s+", " ").Trim()
    $hasStatus = $text -match "Status update:"
    return [pscustomobject]@{
        ExitCode = [int]$result.ExitCode
        Text = $text
        Summary = $(if ([string]::IsNullOrWhiteSpace($flat)) { "unknown" } else { $flat })
        HasStatus = [bool]$hasStatus
        Connected = [bool]($text -match "Status update:\s*Connected")
        Unstable = [bool]($text -match "Network:\s*unstable")
        TimedOut = [bool]$result.TimedOut
    }
}

function Get-WslProxyStackRestartReadiness {
    $script = 'test -d "$1" || exit 11; command -v docker >/dev/null 2>&1 || exit 12; docker info >/dev/null 2>&1 || exit 13; docker compose version >/dev/null 2>&1 || exit 14; printf READY'
    $result = Invoke-HiddenNativeOutput `
        -FilePath "wsl.exe" `
        -ArgumentList @("-d", $WslDistro, "--", "bash", "-lc", $script, "gptimage-probe", $ProjectWslPath) `
        -TimeoutMs 20000 `
        -LogOutput $false
    $reason = switch ([int]$result.ExitCode) {
        0 { "ready" }
        11 { "project_path_missing" }
        12 { "docker_command_missing" }
        13 { "docker_daemon_unavailable_safe_mode" }
        14 { "docker_compose_unavailable" }
        -1 { "preflight_timeout" }
        default { "preflight_exit_$($result.ExitCode)" }
    }
    $details = (($result.Stdout + "`n" + $result.Stderr).Trim() -replace "`r?`n", " | " -replace "\s+", " ").Trim()
    return [pscustomobject]@{
        Ready = [bool]([int]$result.ExitCode -eq 0)
        ExitCode = [int]$result.ExitCode
        Reason = [string]$reason
        Details = [string]$details
    }
}

Write-MonitorLog "proxy_monitor_started proxy=$ProxyPort flaresolverr=$FlareSolverrPort openai_probe=$OpenAIProbeUri resource_probe=$ResourceProbeUri"

while ($true) {
    $proxyRootProbe = Get-HttpProbe -Uri "http://127.0.0.1:$ProxyPort/" -TimeoutMs 10000
    $flareProbe = Get-HttpProbe -Uri "http://127.0.0.1:$FlareSolverrPort/" -TimeoutMs 10000
    $openAIProbe = Get-ProxiedHttpProbe -Uri $OpenAIProbeUri -ProxyPort $ProxyPort -TimeoutMs 15000
    $resourceProbe = Get-ProxiedHttpProbe -Uri $ResourceProbeUri -ProxyPort $ProxyPort -TimeoutMs 15000
    $warpStatus = Get-WarpCliStatus

    $proxyRootHealthy = ([int]$proxyRootProbe.Code -eq 400)
    $flareHealthy = ([int]$flareProbe.Code -ge 200 -and [int]$flareProbe.Code -lt 400)
    $openAIReachable = Test-ReachableHttpCode -Code ([int]$openAIProbe.Code)
    $resourceReachable = Test-ReachableHttpCode -Code ([int]$resourceProbe.Code)
    # Do not restart the proxy stack only because WARP reports
    # "Network: unstable". On some wired networks WARP can keep the SOCKS
    # proxy usable while reporting degraded health; restarting in that state
    # creates a self-inflicted reconnect loop and causes CONNECT 503 spikes.
    #
    # Restart only when WARP is unavailable/unknown, or when the actual
    # proxied probes below are not reachable.
    $warpKnownBad = (($warpStatus.HasStatus -and -not $warpStatus.Connected) -or $warpStatus.TimedOut)

    if ($proxyRootHealthy -and $flareHealthy -and $openAIReachable -and $resourceReachable -and -not $warpKnownBad) {
        Start-Sleep -Seconds $HealthySleepSeconds
        continue
    }

    Write-MonitorLog ("proxy_stack_unhealthy proxy_root={0} openai_proxy={1} resource_proxy={2} flaresolverr={3} warp=""{4}"" proxy_error=""{5}"" openai_error=""{6}"" resource_error=""{7}"" restarting=1" -f `
        [int]$proxyRootProbe.Code,
        [int]$openAIProbe.Code,
        [int]$resourceProbe.Code,
        [int]$flareProbe.Code,
        $warpStatus.Summary,
        (Format-ProbeError $proxyRootProbe),
        (Format-ProbeError $openAIProbe),
        (Format-ProbeError $resourceProbe))
    $restartReadiness = Get-WslProxyStackRestartReadiness
    if (-not $restartReadiness.Ready) {
        Write-MonitorLog ("proxy_stack_restart_skipped reason={0} exit={1} details=""{2}""" -f `
            $restartReadiness.Reason,
            $restartReadiness.ExitCode,
            ($restartReadiness.Details -replace '"', "'"))
        Start-Sleep -Seconds $RestartSleepSeconds
        continue
    }
    $wslScriptPath = "$ProjectWslPath/scripts/start_proxy_stack_wsl.sh"
    $exitCode = Invoke-HiddenNative `
        -FilePath "wsl.exe" `
        -ArgumentList @("-d", $WslDistro, "--", "bash", $wslScriptPath, $ProjectWslPath)
    Write-MonitorLog "proxy_stack_restart_exit=$exitCode"
    Start-Sleep -Seconds $RestartSleepSeconds
}
