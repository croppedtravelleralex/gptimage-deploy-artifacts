param(
    [switch]$WithFrontend,
    [string]$TaskName = "GPTImageLocal",
    [string]$BackendHost = "127.0.0.1",
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000
)

$ErrorActionPreference = "Stop"

$startScript = Join-Path $PSScriptRoot "start_all.ps1"
$arguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden",
    "-File", ('"{0}"' -f $startScript),
    "-BackendHost", $BackendHost,
    "-BackendPort", [string]$BackendPort
)

if ($WithFrontend) {
    $arguments += @("-WithFrontend", "-FrontendPort", [string]$FrontendPort)
} else {
    $arguments += "-BackendOnly"
}

$taskInstalled = $false
try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($arguments -join " ")
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force | Out-Null
    $taskInstalled = $true
} catch {
    Write-Host "scheduled_task_install_failed=$($_.Exception.Message)"
}

if ($taskInstalled) {
    Write-Host "autostart_installed=scheduled_task task=$TaskName with_frontend=$([bool]$WithFrontend)"
    exit 0
}

$startupDir = [Environment]::GetFolderPath("Startup")
if ([string]::IsNullOrWhiteSpace($startupDir)) {
    throw "Cannot resolve current user Startup folder."
}

$startupVbs = Join-Path $startupDir "$TaskName.vbs"
$startupCmd = Join-Path $startupDir "$TaskName.cmd"
$commandLine = 'powershell.exe {0}' -f ($arguments -join " ")
$escapedCommandLine = $commandLine.Replace('"', '""')
@(
    'Set shell = CreateObject("Wscript.Shell")',
    ('shell.Run "{0}", 0, False' -f $escapedCommandLine)
) | Set-Content -LiteralPath $startupVbs -Encoding ASCII

Remove-Item -LiteralPath $startupCmd -Force -ErrorAction SilentlyContinue

Write-Host "autostart_installed=startup_folder path=$startupVbs with_frontend=$([bool]$WithFrontend)"
