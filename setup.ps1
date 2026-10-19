# Setup script for Windows (PowerShell). Installs project dependencies
# (Python pip packages, Node npm packages, Playwright browser binaries) and
# prepares a starter .env file. Does NOT install Python or Node themselves
# - see USER_GUIDE.md for why. Safe to re-run (idempotent, non-destructive).

$ErrorActionPreference = "Stop"

$RequiredPythonMajor = 3
$RequiredPythonMinor = 10
$RequiredNodeMajor = 18
$RequiredNodeMinor = 18
$RequiredNodePatch = 0

function Fail($msg) {
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

Write-Host "==> Checking Python version..."
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Fail "python not found on PATH. Install Python >= 3.10 (3.11 recommended) from https://www.python.org/downloads/"
}

$pyVersionOutput = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "Could not determine Python version. Install Python >= 3.10 from https://www.python.org/downloads/"
}
$pyParts = $pyVersionOutput.Trim().Split(".")
$pyMajor = [int]$pyParts[0]
$pyMinor = [int]$pyParts[1]
if ($pyMajor -lt $RequiredPythonMajor -or ($pyMajor -eq $RequiredPythonMajor -and $pyMinor -lt $RequiredPythonMinor)) {
    Fail "Python $pyVersionOutput found, but >= 3.10 is required. Install a newer Python from https://www.python.org/downloads/"
}
Write-Host "    Python $pyVersionOutput OK"

Write-Host "==> Checking Node.js version..."
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCmd) {
    Fail "node not found on PATH. Install Node.js >= 18.18.0 (20 LTS recommended) from https://nodejs.org/"
}

$nodeVersionRaw = (& node --version).Trim()
if ($LASTEXITCODE -ne 0) {
    Fail "Could not determine Node.js version. Install Node.js >= 18.18.0 from https://nodejs.org/"
}
$nodeVersion = $nodeVersionRaw.TrimStart("v")
$nodeParts = $nodeVersion.Split(".")
$nodeMajor = [int]$nodeParts[0]
$nodeMinor = [int]$nodeParts[1]
$nodePatch = [int]$nodeParts[2]

$nodeOk = $true
if ($nodeMajor -lt $RequiredNodeMajor) {
    $nodeOk = $false
} elseif ($nodeMajor -eq $RequiredNodeMajor) {
    if ($nodeMinor -lt $RequiredNodeMinor) {
        $nodeOk = $false
    } elseif ($nodeMinor -eq $RequiredNodeMinor -and $nodePatch -lt $RequiredNodePatch) {
        $nodeOk = $false
    }
}
if (-not $nodeOk) {
    Fail "Node.js $nodeVersion found, but >= 18.18.0 is required. Install a newer Node.js from https://nodejs.org/"
}
Write-Host "    Node.js v$nodeVersion OK"

# Install into a project virtualenv, not into whatever Python is on PATH: a global
# install pollutes site-packages and breaks the next project that needs other versions.
# Already inside a virtualenv? Then that one is used as-is.
if ($env:VIRTUAL_ENV) {
    $venvPython = "python"
    Write-Host "==> Using the active virtualenv ($env:VIRTUAL_ENV)."
} else {
    if (-not (Test-Path ".venv")) {
        Write-Host "==> Creating the virtualenv (.venv)..."
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { Fail "Could not create .venv." }
    } else {
        Write-Host "==> Reusing the existing virtualenv (.venv)."
    }
    $venvPython = (Resolve-Path ".venv\Scripts\python.exe").Path
}

Write-Host "==> Installing Python dependencies (pip install -e .[dev])..."
& $venvPython -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { Fail "pip install failed." }
Write-Host "    Python dependencies installed."

# There is no committed Playwright repo to install into any more — the
# pipeline scaffolds one per profile (`qa-pipeline scaffold`), and its Node
# dependencies are installed there, after it exists. Playwright's BROWSER
# binaries are machine-wide, though, so they are worth fetching now: it is
# the slowest step of a first run and it needs no project at all.
Write-Host "==> Installing Playwright browser binaries (machine-wide)..."
& $venvPython -m playwright install --with-deps
if ($LASTEXITCODE -ne 0) { Fail "playwright install failed." }
Write-Host "    Playwright browsers installed."

Write-Host "==> Checking for .env file..."
if (Test-Path ".env") {
    Write-Host "    .env already exists - leaving it untouched."
} elseif (Test-Path ".env.example") {
    Copy-Item ".env.example" ".env"
    Write-Host "    Created .env from .env.example - EDIT IT with your real values before running the pipeline."
} else {
    Write-Host "    WARNING: .env.example not found; skipping .env creation."
}

$envStatus = if (Test-Path ".env") { "present" } else { "MISSING - please create one" }

Write-Host ""
Write-Host "================================================================"
Write-Host " Setup complete."
Write-Host "   - Python deps:      installed (pip install -e .[dev])"
Write-Host "   - Node deps:        deferred (run ""npm install"" in the workspace after scaffolding)"
Write-Host "   - Playwright:       browser binaries installed"
Write-Host "   - .env:             $envStatus"
Write-Host ""
Write-Host " Next: activate the virtualenv in each new terminal:  .\.venv\Scripts\Activate.ps1"
Write-Host "       fill in .env, then: qa-pipeline doctor"
Write-Host "       qa-pipeline init-profile <app>; qa-pipeline scaffold --profile <app>"
Write-Host "================================================================"

# A last look at what is still missing (keys, URL, browsers). Informational: setup
# itself succeeded, so a problem here does not fail the script.
Write-Host ""
& $venvPython -m qa_pipeline.cli doctor
exit 0
