param(
    [string]$SshHost = "panda",
    [int]$BatchSize = 20,
    [int]$MaxAccountsPerRun = 20,
    [switch]$ForceAll,
    [switch]$HiddenChild
)

$script = Join-Path $PSScriptRoot "sync_accounts_delta_to_panda.ps1"
if (-not (Test-Path -LiteralPath $script)) {
    throw "Delta sync script not found: $script"
}

if (([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) -and -not $HiddenChild) {
    $self = $PSCommandPath
    $powershell = (Get-Command powershell.exe -ErrorAction SilentlyContinue).Source
    if ($powershell) {
        $hiddenArgs = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden",
            "-File", $self,
            "-SshHost", $SshHost,
            "-BatchSize", $BatchSize,
            "-MaxAccountsPerRun", $MaxAccountsPerRun,
            "-HiddenChild"
        )
        if ($ForceAll) {
            $hiddenArgs += "-ForceAll"
        }
        $process = Start-Process -FilePath $powershell -ArgumentList $hiddenArgs -WindowStyle Hidden -Wait -PassThru
        exit $process.ExitCode
    }
}

$argsList = @(
    "-SshHost", $SshHost,
    "-BatchSize", $BatchSize,
    "-MaxAccountsPerRun", $MaxAccountsPerRun
)
if ($ForceAll) {
    $argsList += "-ForceAll"
}

& $script @argsList
