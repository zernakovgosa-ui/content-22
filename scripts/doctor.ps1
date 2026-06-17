# TREZZY Content Factory - doctor.ps1
# Reports environment readiness without changing anything.

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot

function Tick($ok) {
    if ($ok) { return "OK " }
    else     { return "XX " }
}

function Get-VerOrMissing($cmdName, $args) {
    $cmd = Get-Command $cmdName -ErrorAction SilentlyContinue
    if (-not $cmd) { return "(missing)" }
    try {
        $out = & $cmdName $args 2>&1 | Select-Object -First 1
        return [string]$out
    } catch {
        return "(error)"
    }
}

Write-Host ""
Write-Host "============================================"
Write-Host "  TREZZY Content Factory - environment doctor"
Write-Host "============================================"
Write-Host ""
Write-Host "Project root : $root"
Write-Host ""

# --- Tooling ---
$py     = Get-Command python -ErrorAction SilentlyContinue
$pip    = Get-Command pip     -ErrorAction SilentlyContinue
$node   = Get-Command node    -ErrorAction SilentlyContinue
$npm    = Get-Command npm     -ErrorAction SilentlyContinue
$git    = Get-Command git     -ErrorAction SilentlyContinue
$ffmpeg = Get-Command ffmpeg  -ErrorAction SilentlyContinue

$pyVer     = if ($py)     { ((& python --version 2>&1) | Select-Object -First 1 | Out-String).Trim() } else { "(missing)" }
$pipVer    = if ($pip)    { ((& pip --version    2>&1) | Select-Object -First 1 | Out-String).Trim() } else { "(missing)" }
$nodeVer   = if ($node)   { ((& node --version   2>&1) | Select-Object -First 1 | Out-String).Trim() } else { "(missing)" }
$npmVer    = if ($npm)    { ((& npm --version    2>&1) | Select-Object -First 1 | Out-String).Trim() } else { "(missing)" }
$gitVer    = if ($git)    { ((& git --version    2>&1) | Select-Object -First 1 | Out-String).Trim() } else { "(missing)" }
$ffmpegVer = if ($ffmpeg) { ((& ffmpeg -version  2>&1) | Select-Object -First 1 | Out-String).Trim() } else { "(missing - install: winget install Gyan.FFmpeg)" }

Write-Host (" [{0}] Python   : {1}" -f (Tick ([bool]$py)),     $pyVer)
Write-Host (" [{0}] pip      : {1}" -f (Tick ([bool]$pip)),    $pipVer)
Write-Host (" [{0}] Node     : {1}" -f (Tick ([bool]$node)),   $nodeVer)
Write-Host (" [{0}] npm      : {1}" -f (Tick ([bool]$npm)),    $npmVer)
Write-Host (" [{0}] Git      : {1}" -f (Tick ([bool]$git)),    $gitVer)
Write-Host (" [{0}] ffmpeg   : {1}" -f (Tick ([bool]$ffmpeg)), $ffmpegVer)

Write-Host ""

# --- Project folders ---
$folders = @(
    "apps",
    "packages",
    "data",
    "output",
    "scripts",
    "trezzy-video-worker"
)
foreach ($f in $folders) {
    $full = Join-Path $root $f
    Write-Host (" [{0}] folder   : {1}" -f (Tick (Test-Path $full)), $f)
}

Write-Host ""

# --- Required files ---
$paths = @(
    "apps\api\main.py",
    "apps\dashboard\index.html",
    "packages\agents\__init__.py",
    "packages\video\worker_client.py",
    "packages\integrations\n8n_adapter.py",
    "packages\shared\schemas.py",
    "trezzy-video-worker\main.py",
    "trezzy-video-worker\content_brain.py",
    "data\accounts.json",
    "data\content_jobs.json",
    "data\stats.json",
    "data\products.json",
    "data\settings.json",
    ".env.example"
)
foreach ($p in $paths) {
    $full = Join-Path $root $p
    Write-Host (" [{0}] file     : {1}" -f (Tick (Test-Path $full)), $p)
}

Write-Host ""

# --- Worker venv ---
$venv = Join-Path $root "trezzy-video-worker\.venv\Scripts\python.exe"
Write-Host (" [{0}] worker venv : {1}" -f (Tick (Test-Path $venv)), $venv)

# --- Output dir ---
$out = Join-Path $root "output\jobs"
if (-not (Test-Path $out)) {
    New-Item -ItemType Directory -Force -Path $out | Out-Null
}
Write-Host (" [OK ] output/jobs  : {0}" -f $out)

Write-Host ""

# --- Services reachable? ---
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2 -ErrorAction Stop
    Write-Host (" [OK ] worker live : http://127.0.0.1:8000 - {0}" -f $h.status) -ForegroundColor Green
} catch {
    Write-Host " [   ] worker live : not running (ok if not started yet)" -ForegroundColor DarkYellow
}

try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:8001/health" -TimeoutSec 2 -ErrorAction Stop
    Write-Host (" [OK ] api live    : http://127.0.0.1:8001 - {0}" -f $h.status) -ForegroundColor Green
} catch {
    Write-Host " [   ] api live    : not running (ok if not started yet)" -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "Done. Fix any [XX] entries before running start_all.ps1."
Write-Host ""
