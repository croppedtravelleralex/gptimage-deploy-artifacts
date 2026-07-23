param(
    [string]$Email = "mthomas4jl6@yumail.co",
    [string]$TaskName = "GPTImageSignalAccountProbe",
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -ErrorAction Stop).Source
$script = Join-Path $PSScriptRoot "probe_signal_account.py"
if (-not (Test-Path -LiteralPath $script)) {
    throw "probe script missing: $script"
}

$argLine = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"Set-Location -LiteralPath '$repo'; & '$python' '$script' --email '$Email'`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argLine
$start = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration ([TimeSpan]::FromDays(3650))
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Write-Host "signal_probe_task_installed=$TaskName interval_min=$IntervalMinutes email=$Email start=$($start.ToString('s'))"
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Format-List TaskName, LastRunTime, NextRunTime, LastTaskResult
