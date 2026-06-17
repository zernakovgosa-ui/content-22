# Starts API + Dashboard (apps/api) on 127.0.0.1:8001
# Uses the same venv as the worker - it already has fastapi/uvicorn/pydantic/dotenv.

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$worker = Join-Path $root "trezzy-video-worker"
$venvPy = Join-Path $worker ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Write-Host "venv not found. Run scripts\start_worker.ps1 once to create it." -ForegroundColor Red
    exit 1
}

# API injects REPO_ROOT into sys.path and reads .env from the project root.
$env:PYTHONPATH = $root
if (-not $env:API_HOST) { $env:API_HOST = "127.0.0.1" }
if (-not $env:API_PORT) { $env:API_PORT = "8001" }

Write-Host ("Starting TREZZY API + Dashboard on http://{0}:{1} ..." -f $env:API_HOST, $env:API_PORT) -ForegroundColor Cyan
Push-Location $root
try {
    & $venvPy -m uvicorn apps.api.main:app --host $env:API_HOST --port $env:API_PORT
} finally {
    Pop-Location
}
