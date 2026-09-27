#!/usr/bin/env bash
# ==============================================================================
# R20 Quantum Trader - Quick Start Script
# ==============================================================================

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "❌ Error: account-isolation .venv is missing; run deploy/install.sh first."
    exit 1
fi

echo "🚀 [R20 Quantum Trader] Initializing system environment..."

# 1. Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: python3 is not installed."
    exit 1
fi

# 2. Check or create .env
if [ ! -f .env ]; then
    if [ -f env.example ]; then
        echo "📝 Creating .env from env.example..."
        cp env.example .env
    else
        echo "⚠️ Warning: env.example not found, please configure .env manually."
    fi
fi

# 3. Create required runtime directories
mkdir -p data logs backups

# 3.1 Initialize default instrument pool if not present (prevents untrusted pool blocking entry)
if [ ! -f "data/instrument_pool.json" ]; then
    echo "📋 Initializing default instrument pool..."
    if [ -x "$PYTHON_BIN" ]; then
        "$PYTHON_BIN" -c "from scripts.instrument_pool import save_instruments, DEFAULT_INSTRUMENTS; save_instruments(DEFAULT_INSTRUMENTS)" 2>/dev/null || true
    fi
fi

# 4. Ensure frontend dependencies exist, then build when the production bundle is absent.
# A checked-in dist directory must not hide missing Vue dependencies from tests or
# development commands. `npm ci` uses package-lock.json for reproducibility.
if [ ! -d "frontend/node_modules" ] || [ ! -d "frontend/dist" ]; then
    echo "📦 Preparing Vue 3 frontend dependencies..."
    if command -v npm &> /dev/null; then
        cd frontend
        npm ci
        npm run build
        cd "$ROOT_DIR"
    else
        echo "⚠️ Warning: npm is not installed. Please run 'cd frontend && npm ci && npm run build'."
    fi
fi

# 5. Start Backend Engine
echo "✨ Launching R20 Quantum Trader on http://${DASHBOARD_HOST:-127.0.0.1}:${DASHBOARD_PORT:-8080} ..."
export R20_GATEWAY_EMBEDDED="${R20_GATEWAY_EMBEDDED:-1}"
exec "$PYTHON_BIN" -m uvicorn r20_backend.app:app --host "${DASHBOARD_HOST:-127.0.0.1}" --port "${DASHBOARD_PORT:-8080}"
