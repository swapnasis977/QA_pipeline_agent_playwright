#!/usr/bin/env bash
# Setup script for macOS/Linux. Installs project dependencies (Python pip
# packages, Node npm packages, Playwright browser binaries) and prepares a
# starter .env file. Does NOT install Python or Node themselves — see
# USER_GUIDE.md for why. Safe to re-run (idempotent, non-destructive).

set -e

REQUIRED_PYTHON_MAJOR=3
REQUIRED_PYTHON_MINOR=10
REQUIRED_NODE_MAJOR=18
REQUIRED_NODE_MINOR=18
REQUIRED_NODE_PATCH=0

echo "==> Checking Python version..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH."
    echo "Install Python >= 3.10 (3.11 recommended) from https://www.python.org/downloads/"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt "$REQUIRED_PYTHON_MAJOR" ] || { [ "$PY_MAJOR" -eq "$REQUIRED_PYTHON_MAJOR" ] && [ "$PY_MINOR" -lt "$REQUIRED_PYTHON_MINOR" ]; }; then
    echo "ERROR: Python $PY_VERSION found, but >= 3.10 is required."
    echo "Install a newer Python from https://www.python.org/downloads/"
    exit 1
fi
echo "    Python $PY_VERSION OK"

echo "==> Checking Node.js version..."
if ! command -v node >/dev/null 2>&1; then
    echo "ERROR: node not found on PATH."
    echo "Install Node.js >= 18.18.0 (20 LTS recommended) from https://nodejs.org/"
    exit 1
fi

NODE_VERSION=$(node --version | sed 's/^v//')
NODE_MAJOR=$(echo "$NODE_VERSION" | cut -d. -f1)
NODE_MINOR=$(echo "$NODE_VERSION" | cut -d. -f2)
NODE_PATCH=$(echo "$NODE_VERSION" | cut -d. -f3)
NODE_OK=1
if [ "$NODE_MAJOR" -lt "$REQUIRED_NODE_MAJOR" ]; then
    NODE_OK=0
elif [ "$NODE_MAJOR" -eq "$REQUIRED_NODE_MAJOR" ]; then
    if [ "$NODE_MINOR" -lt "$REQUIRED_NODE_MINOR" ]; then
        NODE_OK=0
    elif [ "$NODE_MINOR" -eq "$REQUIRED_NODE_MINOR" ] && [ "$NODE_PATCH" -lt "$REQUIRED_NODE_PATCH" ]; then
        NODE_OK=0
    fi
fi
if [ "$NODE_OK" -eq 0 ]; then
    echo "ERROR: Node.js $NODE_VERSION found, but >= 18.18.0 is required."
    echo "Install a newer Node.js from https://nodejs.org/"
    exit 1
fi
echo "    Node.js v$NODE_VERSION OK"

# Install into a project virtualenv, not into whatever Python is on PATH: a global
# install pollutes site-packages and breaks the next project that needs other versions.
# Already inside a virtualenv? Then that one is used as-is.
if [ -n "${VIRTUAL_ENV:-}" ]; then
    echo "==> Using the active virtualenv ($VIRTUAL_ENV)."
else
    if [ ! -d ".venv" ]; then
        echo "==> Creating the virtualenv (.venv)..."
        python3 -m venv .venv || { echo "ERROR: could not create .venv"; exit 1; }
    else
        echo "==> Reusing the existing virtualenv (.venv)."
    fi
    # shellcheck disable=SC1091
    . .venv/bin/activate
fi

echo "==> Installing Python dependencies (pip install -e .[dev])..."
python -m pip install -e ".[dev]"
echo "    Python dependencies installed."

# There is no committed Playwright repo to install into any more — the
# pipeline scaffolds one per profile (`qa-pipeline scaffold`), and its Node
# dependencies are installed there, after it exists. Playwright's BROWSER
# binaries are machine-wide, though, so they are worth fetching now: it is
# the slowest step of a first run and it needs no project at all.
echo "==> Installing Playwright browser binaries (machine-wide)..."
python -m playwright install --with-deps
echo "    Playwright browsers installed."

echo "==> Checking for .env file..."
if [ -f ".env" ]; then
    echo "    .env already exists — leaving it untouched."
else
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "    Created .env from .env.example — EDIT IT with your real values before running the pipeline."
    else
        echo "    WARNING: .env.example not found; skipping .env creation."
    fi
fi

echo ""
echo "================================================================"
echo " Setup complete."
echo "   - Python deps:      installed (pip install -e .[dev])"
echo "   - Node deps:        deferred (run \"npm install\" in the workspace after scaffolding)"
echo "   - Playwright:       browser binaries installed"
echo "   - .env:             $( [ -f .env ] && echo 'present' || echo 'MISSING - please create one' )"
echo ""
echo " Next: activate the virtualenv in each new terminal:  source .venv/bin/activate"
echo "       fill in .env, then: qa-pipeline doctor"
echo "       qa-pipeline init-profile <app> && qa-pipeline scaffold --profile <app>"
echo "================================================================"

# A last look at what is still missing (keys, URL, browsers). Informational: setup
# itself succeeded, so a problem here does not fail the script.
echo ""
python -m qa_pipeline.cli doctor || true
