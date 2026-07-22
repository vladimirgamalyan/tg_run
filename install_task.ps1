#Requires -Version 5.1
<#
  Registers the tg_run bot in Windows Task Scheduler.

  Why the scheduler and not a service: the bot opens VISIBLE Windows Terminal
  windows, and those appear only in an interactive user session. A service
  (session 0) would launch them invisibly. Therefore:
    - an "At log on" trigger for the current user, REPEATING every 10 minutes
      indefinitely: this is the self-heal. With MultipleInstances=IgnoreNew a
      repetition while the bot is running is a no-op; if it has died the next
      repetition brings it back within 10 minutes. The logon trigger alone is
      not enough - waking from sleep does not create a logon event, so a bot
      killed during a sleep/resume cycle would otherwise stay dead until the
      next reboot (this actually happened: ~21 h of downtime);
    - LogonType Interactive ("run only when the user is logged on");
    - a scheduler RestartCount (3x, 1 min) kept as a secondary fallback, though
      it does not reliably fire on this system;
    - no run-time limit; console window hidden (pythonw.exe).

  Run (from the project folder):
    powershell -ExecutionPolicy Bypass -File .\install_task.ps1

  Administrator rights are usually NOT required (the task runs in the current
  user's context). If Register-ScheduledTask fails - run it from a PowerShell
  opened "as administrator".
#>

$ErrorActionPreference = 'Stop'

$TaskName   = 'tg_run'
$ProjectDir = $PSScriptRoot
$VenvCfg    = Join-Path $ProjectDir '.venv\pyvenv.cfg'
# The task launches bot.py directly. There is no supervisor: the bot runs
# reliably on its own, and the scheduler's built-in "Restart on failure" (set
# below) is the only, best-effort restart mechanism.
$Script     = 'bot.py'
$Account    = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $VenvCfg)) {
    Write-Error "$VenvCfg not found. Create the environment first: uv sync"
}
# We launch the GUI interpreter of the base Python (pythonw.exe from the home
# venv), NOT the venv .venv\Scripts\pythonw.exe: that one still creates a
# console window on Win11. The base pythonw (subsystem=GUI) creates no window;
# the venv packages are wired up by the bot itself (site.addsitedir in bot.py).
# $PyHome, not $HOME - the latter is reserved in PowerShell.
$PyHome  = ((Get-Content $VenvCfg | Where-Object { $_ -match '^\s*home\s*=' }) -replace '^\s*home\s*=\s*','').Trim()
$Pythonw = Join-Path $PyHome 'pythonw.exe'

if (-not (Test-Path $Pythonw)) {
    Write-Error "Base interpreter not found: $Pythonw"
}
$ScriptPath = Join-Path $ProjectDir $Script
if (-not (Test-Path $ScriptPath)) {
    Write-Error "$Script not found in $ProjectDir"
}

# Full path to bot.py, not a relative one: restart_task.ps1 finds the bot
# process by matching this path in the process command line.
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$ScriptPath`" --hidden" -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $Account
# Repeat every 10 minutes forever. Waking from sleep does not fire the logon
# trigger, so without this a bot that died during a sleep/resume cycle would
# stay dead until the next reboot. MultipleInstances=IgnoreNew (the default)
# makes a repetition a no-op while the bot is already running; StartWhenAvailable
# (in the settings below) runs a repetition missed during sleep on wake.
# Duration is intentionally omitted: an empty RepetitionDuration means
# "Indefinitely" to Task Scheduler. [TimeSpan]::MaxValue serializes to a value
# the scheduler rejects as out of range, so do NOT set it.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10)).Repetition

$principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType Interactive -RunLevel Limited

# RestartCount/RestartInterval - that "restart a limited number of times".
# ExecutionTimeLimit = 0 -> no time limit (the bot runs continuously).
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

# Idempotency: if the task already exists - stop and recreate it. Stop first:
# Unregister alone leaves an already-running bot process alive, and starting a
# second instance would hit Telegram 409 Conflict.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'tg_run - Telegram bot that launches Claude Code' | Out-Null

Write-Host "Task '$TaskName' registered." -ForegroundColor Green
Write-Host "Starting it now (without waiting for re-login)..."
Start-ScheduledTask -TaskName $TaskName
Write-Host "Done. Logs: $(Join-Path $ProjectDir 'bot.log')"
Write-Host "Note: do not keep a second bot instance running manually (Telegram will return 409 Conflict)."
