[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13

$workDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $workDir

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  GemiPersona V2 -- Setup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ── 1. Check Python ───────────────────────────────────────────
Write-Host "`n[1/4] Checking Python..." -ForegroundColor Yellow
try {
    $pyVer = & python --version 2>&1
    Write-Host "  Found: $pyVer" -ForegroundColor Green
} catch {
    Write-Error "Python not found. Install Python 3.10+ from https://python.org"
    exit 1
}

# ── 2. Engine venv ────────────────────────────────────────────
Write-Host "`n[2/4] Engine venv (Playwright + FastAPI)..." -ForegroundColor Yellow
$engineVenv = Join-Path $workDir "Gemi_Engine_V2\.venv"
if (-not (Test-Path $engineVenv)) {
    & python -m venv $engineVenv
}
& "$engineVenv\Scripts\python.exe" -m pip install -r "Gemi_Engine_V2\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip install (engine) failed"; exit 1 }
& "$engineVenv\Scripts\python.exe" -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Write-Error "playwright install failed"; exit 1 }
Write-Host "  Done." -ForegroundColor Green

# ── 3. Conductor venv (root .venv) ────────────────────────────
Write-Host "`n[3/4] Conductor venv (FastAPI + image processing)..." -ForegroundColor Yellow
$conductorVenv = Join-Path $workDir ".venv"
if (-not (Test-Path $conductorVenv)) {
    & python -m venv $conductorVenv
}
& "$conductorVenv\Scripts\python.exe" -m pip install -r "conductor\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip install (conductor) failed"; exit 1 }
Write-Host "  Done." -ForegroundColor Green

# ── 4. Portable Node.js (mirrors Gemi_MCP_V2; no system Node needed) ──
Write-Host "`n[4/5] Portable Node.js..." -ForegroundColor Yellow
$nodeDir = Join-Path $workDir ".node_venv"
$nodeUrl = "https://nodejs.org/dist/v20.11.1/node-v20.11.1-win-x64.zip"
$nodeZip = Join-Path $workDir "node-portable.zip"

if (-not (Test-Path $nodeDir)) {
    Write-Host "  Downloading Node.js v20.11.1..." -ForegroundColor Yellow
    try {
        Invoke-WebRequest -Uri $nodeUrl -OutFile $nodeZip -UserAgent "Mozilla/5.0" -UseBasicParsing
        $tempDir = Join-Path $workDir ".node_temp"
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
        Expand-Archive -Path $nodeZip -DestinationPath $tempDir -Force
        $inner = Get-ChildItem -Path $tempDir -Directory | Select-Object -First 1
        Move-Item -Path $inner.FullName -Destination $nodeDir -Force
        Remove-Item $nodeZip -Force
        Remove-Item $tempDir -Recurse -Force
        Write-Host "  Node.js extracted to .node_venv" -ForegroundColor Green
    } catch {
        if (Test-Path $nodeZip) { Remove-Item $nodeZip -Force }
        Write-Error "Failed to download Node.js: $_"; exit 1
    }
} else {
    Write-Host "  .node_venv already exists, skipping download." -ForegroundColor Green
}
$env:PATH = "$nodeDir;" + $env:PATH
$nodeVer = & "$nodeDir\node.exe" --version
$npmVer  = & "$nodeDir\npm.cmd" --version
Write-Host "  node $nodeVer  /  npm $npmVer" -ForegroundColor Green

# ── 5. Electron UI deps ───────────────────────────────────────
Write-Host "`n[5/5] Electron UI (npm install in app\)..." -ForegroundColor Yellow
Push-Location (Join-Path $workDir "app")
& "$nodeDir\npm.cmd" install
$npmExit = $LASTEXITCODE
Pop-Location
if ($npmExit -ne 0) { Write-Error "npm install failed"; exit 1 }
Write-Host "  Done." -ForegroundColor Green

# ── Default: hide CLI window on startup (first install only) ──
# ponytail: "data\ missing" is the first-install heuristic; the flag is
# toggled later via System Config, so re-running setup must not resurrect it.
$dataDir = Join-Path $workDir "data"
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
    Set-Content -Path (Join-Path $dataDir "hide_cli.flag") -Value "true" -Encoding ascii
    Write-Host "`n  Hide-CLI-on-startup enabled by default (start via run.vbs)." -ForegroundColor Green
}

# ── Done ──────────────────────────────────────────────────────
Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  Setup complete." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
