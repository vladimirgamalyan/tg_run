#Requires -Version 5.1
<#
  Restarts the tg_run scheduler task so a code change is picked up.

  The task launches bot.py directly, so Stop-ScheduledTask kills the bot process
  itself. As a safety net we then kill any leftover bot.py of THIS project and
  wait for it to die before starting the task again: two polling clients on one
  token would make Telegram answer 409 Conflict.

  Run (from the project folder):
    powershell -ExecutionPolicy Bypass -File .\restart_task.ps1
#>

$ErrorActionPreference = 'Stop'

$TaskName   = 'tg_run'
$ProjectDir = $PSScriptRoot
$BotPath    = Join-Path $ProjectDir 'bot.py'

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Error "Task '$TaskName' not found. Install it first: .\install_task.ps1"
}

# Match this project's bot process by its full script path. The scheduler task
# passes the full path to bot.py (see install_task.ps1), so this matches the
# autostarted bot; python.exe is included to also catch full-path manual runs.
# A manual `uv run bot.py` uses a relative path and is NOT matched - do not
# keep one running alongside the task (409 Conflict).
$botRe = [regex]::Escape($BotPath)
function Get-BotProcs {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match $botRe }
}

Write-Host "Stopping task '$TaskName'..."
Stop-ScheduledTask -TaskName $TaskName

# Kill whatever survived the task stop.
$deadline = (Get-Date).AddSeconds(10)
do {
    Get-BotProcs | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 400
    $alive = @(Get-BotProcs)
} while ($alive.Count -and (Get-Date) -lt $deadline)

if ($alive.Count) {
    $alive | Select-Object ProcessId, CommandLine | Format-Table -AutoSize -Wrap
    Write-Error "Some processes did not exit; not starting the task to avoid a 409 Conflict."
}
Write-Host "Old bot process stopped." -ForegroundColor Green

Write-Host "Starting task '$TaskName'..."
Start-ScheduledTask -TaskName $TaskName

# Verify exactly one bot.py came up.
Start-Sleep -Seconds 3
$bot = @(Get-BotProcs)
if ($bot.Count -eq 1) {
    Write-Host "Bot restarted (pid $($bot[0].ProcessId)). Logs: $(Join-Path $ProjectDir 'bot.log')" -ForegroundColor Green
} elseif ($bot.Count -eq 0) {
    Write-Warning "No bot.py process yet - check bot.log."
} else {
    Write-Warning "$($bot.Count) bot.py processes running - possible 409 Conflict. Check bot.log."
}
