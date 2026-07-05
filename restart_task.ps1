#Requires -Version 5.1
<#
  Restarts the tg_run scheduler task so a code change is picked up.

  Why not a plain Stop + Start: Stop-ScheduledTask kills only the process the
  task launched directly (supervisor.py). The bot.py child that supervisor
  spawned is orphaned and keeps polling; starting the task again would bring up
  a SECOND bot.py and Telegram would answer 409 Conflict. So we stop the task,
  then explicitly kill any leftover supervisor.py / bot.py of THIS project, wait
  for them to die, and only then start the task again.

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

# Match this project's supervisor/bot pythonw processes. bot.py runs with a full
# path in its command line; supervisor.py runs relative to the task's working
# directory, so it is matched by script name.
$botRe = [regex]::Escape($BotPath)
function Get-BotProcs {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match $botRe -or $_.CommandLine -match '\bsupervisor\.py\b' }
}

Write-Host "Stopping task '$TaskName'..."
Stop-ScheduledTask -TaskName $TaskName

# Kill whatever survived (the orphaned bot.py, and supervisor.py if still alive).
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
Write-Host "Old process tree stopped." -ForegroundColor Green

Write-Host "Starting task '$TaskName'..."
Start-ScheduledTask -TaskName $TaskName

# Verify exactly one bot.py came up.
Start-Sleep -Seconds 3
$bot = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match $botRe })
if ($bot.Count -eq 1) {
    Write-Host "Bot restarted (pid $($bot[0].ProcessId)). Logs: $(Join-Path $ProjectDir 'bot.log')" -ForegroundColor Green
} elseif ($bot.Count -eq 0) {
    Write-Warning "No bot.py process yet - check bot.log."
} else {
    Write-Warning "$($bot.Count) bot.py processes running - possible 409 Conflict. Check bot.log."
}
