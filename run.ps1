$workDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$nodeDir = Join-Path $workDir ".node_venv"

if (-not (Test-Path "$nodeDir\node.exe")) {
    Write-Error "Portable Node.js not found. Run setup.bat first."
    exit 1
}

# Prepend portable Node to PATH for this session only (no system pollution).
$env:PATH = "$nodeDir;" + $env:PATH

# Launch the Electron UI. main.js spawns the conductor (18101), which in turn
# spawns and supervises the engine (18100). Nothing else to start here.
Set-Location (Join-Path $workDir "app")
& "$nodeDir\npm.cmd" start
