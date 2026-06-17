# TREZZY Content Factory - smoke test
# Calls POST /generate-from-plan and prints the job package paths.
# Worker + API must already be running (scripts\start_all.ps1).

$ErrorActionPreference = "Stop"

$payload = @{
    topic           = "Date night fragrance"
    format          = "date_night"
    product_name    = "TREZZY Date Night"
    target_audience = "men 25-35"
    platform        = "instagram"
    quantity        = 1
    render_mode     = "fast"
}

$body      = $payload | ConvertTo-Json -Depth 5
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)

Write-Host "Smoke test: POST http://127.0.0.1:8001/generate-from-plan" -ForegroundColor Cyan
Write-Host "(Video render takes 30-120 seconds)"

try {
    $res = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8001/generate-from-plan" `
        -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body $bodyBytes `
        -TimeoutSec 600

    Write-Host ""
    Write-Host "=== Response ===" -ForegroundColor Green
    $res | ConvertTo-Json -Depth 6

    foreach ($job in $res.jobs) {
        Write-Host ""
        Write-Host ("Job  : {0}" -f $job.job_id) -ForegroundColor Yellow
        Write-Host ("Stat : {0}" -f $job.status)
        if ($job.output_path) {
            Write-Host ("Mp4  : {0}" -f $job.output_path) -ForegroundColor Green
        }
        if ($job.package_dir) {
            Write-Host ("Pkg  : {0}" -f $job.package_dir)
            if (Test-Path $job.package_dir) {
                Get-ChildItem $job.package_dir | ForEach-Object {
                    Write-Host ("        - {0}" -f $_.Name) -ForegroundColor DarkGray
                }
            }
        }
        if ($job.error) {
            Write-Host ("Err  : {0}" -f $job.error) -ForegroundColor Red
        }
    }
} catch {
    Write-Host "Request failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
        Write-Host $_.ErrorDetails.Message -ForegroundColor DarkRed
    }
    exit 1
}
