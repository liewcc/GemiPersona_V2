[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13

$workDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $workDir

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  GemiPersona V2 -- Setup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ── 1. Portable Python ───────────────────────────────────────────
Write-Host "`n[1/4] Portable Python..." -ForegroundColor Yellow
$pythonDir = Join-Path $workDir ".python_venv"
$pythonUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
$pythonZip = Join-Path $workDir "python-portable.zip"

if (-not (Test-Path $pythonDir)) {
    Write-Host "  Downloading Python 3.11.9..." -ForegroundColor Yellow
    try {
        Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonZip -UserAgent "Mozilla/5.0" -UseBasicParsing
        New-Item -ItemType Directory -Path $pythonDir -Force | Out-Null
        Expand-Archive -Path $pythonZip -DestinationPath $pythonDir -Force
        Remove-Item $pythonZip -Force
        
        # Enable site-packages and add relative paths for conductor and engine
        $pthFile = Get-ChildItem -Path $pythonDir -Filter "*._pth" | Select-Object -First 1
        if ($pthFile) {
            $pthContent = @(
                "python311.zip",
                ".",
                "..",
                "../conductor",
                "../Gemi_Engine_V2",
                "",
                "# Uncomment to run site.main() automatically",
                "import site"
            )
            Set-Content -Path $pthFile.FullName -Value $pthContent -Encoding utf8
        }
        
        # Download and install pip
        Write-Host "  Installing pip..." -ForegroundColor Yellow
        $pipUrl = "https://bootstrap.pypa.io/get-pip.py"
        $pipScript = Join-Path $workDir "get-pip.py"
        Invoke-WebRequest -Uri $pipUrl -OutFile $pipScript -UserAgent "Mozilla/5.0" -UseBasicParsing
        & "$pythonDir\python.exe" $pipScript --no-warn-script-location --quiet
        Remove-Item $pipScript -Force
        
        Write-Host "  Portable Python configured." -ForegroundColor Green
    } catch {
        if (Test-Path $pythonZip) { Remove-Item $pythonZip -Force }
        if (Test-Path $pythonDir) { Remove-Item $pythonDir -Recurse -Force }
        Write-Error "Failed to set up Portable Python: $_"; exit 1
    }
} else {
    Write-Host "  .python_venv already exists, skipping download." -ForegroundColor Green
}

# ── 2. Python Dependencies & Playwright ───────────────────────
Write-Host "`n[2/4] Python dependencies and Playwright..." -ForegroundColor Yellow
Write-Host "  Installing pip packages..." -ForegroundColor Yellow
& "$pythonDir\python.exe" -m pip install -r "Gemi_Engine_V2\requirements.txt" -r "conductor\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }

Write-Host "  Installing local Playwright Chromium..." -ForegroundColor Yellow
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $workDir ".playwright_browsers"
& "$pythonDir\python.exe" -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Write-Error "playwright install failed"; exit 1 }
Write-Host "  Done." -ForegroundColor Green

# ── 3. Portable Node.js (mirrors Gemi_MCP_V2; no system Node needed) ──
Write-Host "`n[3/4] Portable Node.js..." -ForegroundColor Yellow
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

# ── 4. Electron UI deps ───────────────────────────────────────
Write-Host "`n[4/4] Electron UI (npm install in app\)..." -ForegroundColor Yellow
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
