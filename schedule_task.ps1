<#
.SYNOPSIS
    Registers (or removes) a Windows Task Scheduler task that runs DailyLog.py
    every morning on its own -- no terminal or Python process has to stay open.

.DESCRIPTION
    Task Scheduler launches `py.exe DailyLog.py` once a day at the given time,
    with this project folder as its working directory (so AIAPIKEYS.env,
    EmailAlerts.env, ai_settings.json, predictions.db and daily_logs/ all
    resolve as they do when you run it by hand).

    This registers a per-user task that runs only while you're logged in --
    the normal case for a personal machine that's usually on. If you also
    want it to run while logged off, open Task Scheduler (taskschd.msc),
    find the task, Properties > General > "Run whether user is logged on or
    not", and enter your password when prompted -- that step needs an
    interactive password prompt this script can't automate.

.PARAMETER Time
    Time of day to run, 24-hour HH:mm. Default 09:00 -- late enough that
    Kalshi has settled every game from the previous night across US time
    zones.

.PARAMETER ProjectDir
    Working directory for the task. Defaults to this script's own folder.

.PARAMETER Uninstall
    Remove the scheduled task instead of creating it.

.EXAMPLE
    .\schedule_task.ps1
    .\schedule_task.ps1 -Time "07:30"
    .\schedule_task.ps1 -Uninstall
#>

param(
    [string]$Time = "09:00",
    [string]$ProjectDir = $PSScriptRoot,
    [switch]$Uninstall
)

$TaskName = "KalshiNBA-DailyLog"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName' (if it existed)."
    exit 0
}

# `py.exe` (the Python launcher) resolves to the real interpreter even when
# `python.exe` / `python3.exe` are just Microsoft Store alias stubs that do
# nothing but prompt to install Python when run outside an interactive shell.
$PyCommand = Get-Command py -ErrorAction SilentlyContinue
if (-not $PyCommand) {
    throw "Could not find 'py' on PATH. Install Python, or edit this script to point at your python.exe directly."
}

$Action = New-ScheduledTaskAction -Execute $PyCommand.Source -Argument "DailyLog.py" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal `
    -Description "Runs DailyLog.py every morning: settles yesterday's NBA games and logs today's odds/AI predictions." `
    -Force | Out-Null

Write-Host "Scheduled '$TaskName' to run DailyLog.py daily at $Time from $ProjectDir."
Write-Host "View or edit it in Task Scheduler (taskschd.msc) under Task Scheduler Library."
Write-Host "Rerun this script with -Uninstall to remove it."
