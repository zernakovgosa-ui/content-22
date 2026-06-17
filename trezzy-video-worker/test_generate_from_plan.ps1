# TREZZY Content Factory - test /generate-from-plan
# Posts sample_plan.json as raw UTF-8 bytes. The worker plans + renders + packages.
# Reading raw bytes avoids PowerShell 5.1 code-page conversion that would corrupt Cyrillic.

$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8000"

# --- health probe ----------------------------------------------------------
try {
    $h = Invoke-RestMethod -Uri "$base/health" -Method Get -TimeoutSec 5
    if ($h.status -ne "ok") {
        Write-Host "Health check returned unexpected payload:" -ForegroundColor Yellow
        $h | ConvertTo-Json
        exit 2
    }
    Write-Host "health: $($h.status) ($($h.service))" -ForegroundColor Green
}
catch {
    Write-Host "Health check failed. Is the server running on $base ?" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

# --- POST /generate-from-plan ---------------------------------------------
$jsonPath = Join-Path $PSScriptRoot "sample_plan.json"
if (-not (Test-Path $jsonPath)) {
    Write-Host "Missing sample_plan.json next to this script." -ForegroundColor Red
    exit 1
}
$bodyBytes = [System.IO.File]::ReadAllBytes($jsonPath)

Write-Host ""
Write-Host "POST $base/generate-from-plan ..." -ForegroundColor Cyan
Write-Host "Rendering can take 60-120 seconds." -ForegroundColor DarkGray

try {
    $resp = Invoke-RestMethod `
        -Uri "$base/generate-from-plan" `
        -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $bodyBytes `
        -TimeoutSec 240

    Write-Host ""
    Write-Host "=== Response ===" -ForegroundColor Green
    $resp | ConvertTo-Json -Depth 6

    Write-Host ""
    if ($resp.plan) {
        Write-Host "Plan hook   : $($resp.plan.hook)"   -ForegroundColor Green
        Write-Host "Plan format : $($resp.plan.format)" -ForegroundColor Green
    }
    if ($resp.output_path) {
        Write-Host "Video       : $($resp.output_path)" -ForegroundColor Green
    }
    if ($resp.package_dir) {
        Write-Host "Package dir : $($resp.package_dir)" -ForegroundColor Green
    }
    if ($resp.duration_seconds) {
        Write-Host "Duration    : $($resp.duration_seconds) s" -ForegroundColor Green
    }

    if ($resp.output_path -and (Test-Path $resp.output_path)) {
        Write-Host ""
        Write-Host "Opening package folder..." -ForegroundColor DarkGray
        Start-Process explorer.exe "/select,`"$($resp.output_path)`""
    }
}
catch {
    Write-Host ""
    Write-Host "Request failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
        Write-Host $_.ErrorDetails.Message -ForegroundColor DarkRed
    }
}
