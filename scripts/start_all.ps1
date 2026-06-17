# TREZZY Content Factory - start everything
# 1) Worker (rendering) -> 127.0.0.1:8000
# 2) API + Dashboard    -> 127.0.0.1:8001
# Opens the dashboard in your browser. Close the service windows to stop them.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$workerScript = Join-Path $root "scripts\start_worker.ps1"
$apiScript    = Join-Path $root "scripts\start_api.ps1"

Write-Host "Starting worker window ..." -ForegroundColor Cyan
Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoLogo","-NoExit","-ExecutionPolicy","Bypass","-File",$workerScript)

Write-Host "Starting api + dashboard window ..." -ForegroundColor Cyan
Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoLogo","-NoExit","-ExecutionPolicy","Bypass","-File",$apiScript)

Write-Host "Dashboard: http://127.0.0.1:8001/" -ForegroundColor Yellow
Write-Host "Waiting 4 seconds, then opening browser ..."
Start-Sleep -Seconds 4
Start-Process "http://127.0.0.1:8001/"
