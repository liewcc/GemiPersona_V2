[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13

$workDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $workDir

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  GemiPersona V2 -- Setup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ── 1. Check Python ───────────────────────────────────────────
Write-Host "`n[1/2] Checking Python..." -ForegroundColor Yellow
try {
    $pyVer = & python --version 2>&1
    Write-Host "  Found: $pyVer" -ForegroundColor Green
} catch {
    Write-Error "Python not found. Install Python 3.10+ from https://python.org"
    exit 1
}

# ── 2. Engine venv ────────────────────────────────────────────
Write-Host "`n[2/2] Engine venv (Playwright + FastAPI)..." -ForegroundColor Yellow
$engineVenv = Join-Path $workDir "Gemi_Engine_V2\.venv"
if (-not (Test-Path $engineVenv)) {
    & python -m venv $engineVenv
}
& "$engineVenv\Scripts\python.exe" -m pip install -r "Gemi_Engine_V2\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip install (engine) failed"; exit 1 }
& "$engineVenv\Scripts\python.exe" -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Write-Error "playwright install failed"; exit 1 }
Write-Host "  Done." -ForegroundColor Green

# ── Done ──────────────────────────────────────────────────────
Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  Setup complete." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
