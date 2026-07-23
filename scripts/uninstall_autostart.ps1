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
    foreach ($ext in @(".cmd", ".vbs", ".lnk")) {
        $startupItem = Join-Path $startupDir ($TaskName + $ext)
        if (Test-Path -LiteralPath $startupItem) {
            Remove-Item -LiteralPath $startupItem -Force
            Write-Host "autostart_removed=startup_folder path=$startupItem"
        }
    }
}

if ($null -eq $task) {
    Write-Host "autostart_checked task=$TaskName"
}
