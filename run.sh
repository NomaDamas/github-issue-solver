#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Load local environment overrides if present (PORT, GIS_INITIAL_*, etc.).
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8787}"
