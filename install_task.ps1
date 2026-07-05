#Requires -Version 5.1
<#
  Registers the tg_run bot in Windows Task Scheduler.

  Why the scheduler and not a service: the bot opens VISIBLE Windows Terminal
  windows, and those appear only in an interactive user session. A service
  (session 0) would launch them invisibly. Therefore:
    - an "At log on" trigger for the current user;
    - LogonType Interactive ("run only when the user is logged on");
    - a scheduler RestartCount (3x, 1 min) kept only as a fallback: it does not
      fire on this system, so the supervisor does the real restarting (see below);
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
# The task launches the SUPERVISOR, which in turn keeps bot.py alive and
# restarts it on crash. The scheduler's built-in "Restart on failure" does not
# fire on this system even on a non-zero exit code, so the supervisor does the
# restarting.
$Script     = 'supervisor.py'
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
if (-not (Test-Path (Join-Path $ProjectDir $Script))) {
    Write-Error "$Script not found in $ProjectDir"
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument $Script -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $Account

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

# Idempotency: if the task already exists - recreate it.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
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
