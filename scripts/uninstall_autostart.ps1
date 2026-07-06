param(
    [string]$TaskName = "GPTImageLocal"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "autostart_removed=scheduled_task task=$TaskName"
}

$startupDir = [Environment]::GetFolderPath("Startup")
if (-not [string]::IsNullOrWhiteSpace($startupDir)) {
    $startupCmd = Join-Path $startupDir "$TaskName.cmd"
    if (Test-Path -LiteralPath $startupCmd) {
        Remove-Item -LiteralPath $startupCmd -Force
        Write-Host "autostart_removed=startup_folder path=$startupCmd"
    }
}

if ($null -eq $task) {
    Write-Host "autostart_checked task=$TaskName"
}
