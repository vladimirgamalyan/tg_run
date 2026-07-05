#Requires -Version 5.1
<#
  Stops and removes the tg_run scheduler task.
  Run:  powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1
#>

$ErrorActionPreference = 'Stop'
$TaskName = 'tg_run'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' removed." -ForegroundColor Green
} else {
    Write-Host "Task '$TaskName' not found - nothing to remove."
}
