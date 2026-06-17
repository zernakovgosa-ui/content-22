# Starts trezzy-video-worker (video rendering) on 127.0.0.1:8000

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$worker = Join-Path $root "trezzy-video-worker"
$venvPy = Join-Path $worker ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Write-Host "venv not found, creating: $venvPy" -ForegroundColor Yellow
    Push-Location $worker
    try {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }

        & $venvPy -m pip install --upgrade pip
        $req = Join-Path $worker "requirements.txt"
        if (Test-Path $req) {
            & $venvPy -m pip install -r $req
        } else {
            Write-Host "requirements.txt not found in trezzy-video-worker - skipping pip install." -ForegroundColor Yellow
        }
    } finally {
        Pop-Location
    }
}

Write-Host "Starting TREZZY Video Worker on http://127.0.0.1:8000 ..." -ForegroundColor Cyan
Push-Location $worker
try {
    & $venvPy main.py
} finally {
    Pop-Location
}
