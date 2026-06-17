# TREZZY Content Factory - test /plan
# Posts sample_plan.json as raw UTF-8 bytes and shows the returned plan.
# Plan generation is local + fast (<1s), so no long timeout is needed.

$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8000"

$jsonPath = Join-Path $PSScriptRoot "sample_plan.json"
if (-not (Test-Path $jsonPath)) {
    Write-Host "Missing sample_plan.json next to this script." -ForegroundColor Red
    exit 1
}
$bodyBytes = [System.IO.File]::ReadAllBytes($jsonPath)

Write-Host "POST $base/plan" -ForegroundColor Cyan
try {
    $resp = Invoke-RestMethod `
        -Uri "$base/plan" `
        -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $bodyBytes `
        -TimeoutSec 15

    Write-Host ""
    Write-Host "=== Plan ===" -ForegroundColor Green
    $resp | ConvertTo-Json -Depth 5

    Write-Host ""
    Write-Host "Format       : $($resp.format)"     -ForegroundColor Green
    Write-Host "Hook         : $($resp.hook)"       -ForegroundColor Green
    Write-Host "Script       : $($resp.script)"     -ForegroundColor Green
    Write-Host "Vibe tags    : $($resp.vibe_tags -join ', ')" -ForegroundColor Green
    Write-Host "CTA          : $($resp.cta)"        -ForegroundColor Green
    Write-Host "Caption      :" -ForegroundColor Green
    Write-Host $resp.caption
    Write-Host "Hashtags     : $($resp.hashtags -join ' ')" -ForegroundColor Green
}
catch {
    Write-Host "Request failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
        Write-Host $_.ErrorDetails.Message -ForegroundColor DarkRed
    }
}
