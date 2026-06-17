# TREZZY Content Factory - test request
# Loads sample_request.json as raw UTF-8 bytes and posts it to the worker.
# Reading raw bytes avoids any PowerShell string-encoding conversion (cp1251 / cp1252)
# that would otherwise corrupt Cyrillic text on Windows PowerShell 5.1.

$ErrorActionPreference = "Stop"

$jsonPath = Join-Path $PSScriptRoot "sample_request.json"
if (-not (Test-Path $jsonPath)) {
    Write-Host "Missing sample_request.json next to this script." -ForegroundColor Red
    exit 1
}

$bodyBytes = [System.IO.File]::ReadAllBytes($jsonPath)

Write-Host "Sending request to http://127.0.0.1:8000/generate ..." -ForegroundColor Cyan
Write-Host "This can take 30-90 seconds on first run." -ForegroundColor DarkGray

try {
    $response = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8000/generate" `
        -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $bodyBytes

    Write-Host ""
    Write-Host "=== Response ===" -ForegroundColor Green
    $response | ConvertTo-Json -Depth 5

    if ($response.output_path -and (Test-Path $response.output_path)) {
        Write-Host ""
        Write-Host "Video saved at: $($response.output_path)" -ForegroundColor Green
        Write-Host "Opening folder..." -ForegroundColor DarkGray
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
