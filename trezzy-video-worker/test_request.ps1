# TREZZY Content Factory - test request
# Posts a realistic perfume content payload (hook + script + vibe_tags + cta + caption + hashtags).
# Body is streamed as raw UTF-8 bytes from sample_request.json to avoid PowerShell 5.1
# code-page conversion that would corrupt Cyrillic.

$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8000"

# --- 1. Health probe --------------------------------------------------------
Write-Host "GET $base/health" -ForegroundColor DarkGray
try {
    $health = Invoke-RestMethod -Uri "$base/health" -Method Get -TimeoutSec 5
    if ($health.status -ne "ok") {
        Write-Host "Health check returned unexpected payload:" -ForegroundColor Yellow
        $health | ConvertTo-Json
        exit 2
    }
    Write-Host "  health: $($health.status) ($($health.service))" -ForegroundColor Green
}
catch {
    Write-Host "Health check failed. Is the server running on $base ?" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

# --- 2. POST /generate ------------------------------------------------------
$jsonPath = Join-Path $PSScriptRoot "sample_request.json"
if (-not (Test-Path $jsonPath)) {
    Write-Host "Missing sample_request.json next to this script." -ForegroundColor Red
    exit 1
}
$bodyBytes = [System.IO.File]::ReadAllBytes($jsonPath)

Write-Host ""
Write-Host "POST $base/generate ..." -ForegroundColor Cyan
Write-Host "Rendering can take 60-120 seconds." -ForegroundColor DarkGray

try {
    $response = Invoke-RestMethod `
        -Uri "$base/generate" `
        -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $bodyBytes

    Write-Host ""
    Write-Host "=== Response ===" -ForegroundColor Green
    $response | ConvertTo-Json -Depth 5

    Write-Host ""
    if ($response.output_path) {
        Write-Host "Video        : $($response.output_path)" -ForegroundColor Green
    }
    if ($response.package_dir) {
        Write-Host "Package dir  : $($response.package_dir)" -ForegroundColor Green
    }
    if ($response.duration_seconds) {
        Write-Host "Duration     : $($response.duration_seconds) s" -ForegroundColor Green
    }
    if ($response.hashtags) {
        Write-Host "Hashtags     : $($response.hashtags -join ' ')" -ForegroundColor Green
    }

    if ($response.output_path -and (Test-Path $response.output_path)) {
        Write-Host ""
        Write-Host "Opening package folder..." -ForegroundColor DarkGray
        Start-Process explorer.exe "/select,`"$($response.output_path)`""
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
