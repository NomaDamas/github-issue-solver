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
# Systemd may inherit a stale GH_TOKEN. Always prefer the current gh keyring
# token, and expose it to the app as a runtime fallback over DB-stored tokens.
unset GH_TOKEN 2>/dev/null || true
if command -v gh >/dev/null 2>&1; then
  export GIS_GH_TOKEN_FALLBACK="${GIS_GH_TOKEN_FALLBACK:-$(gh auth token 2>/dev/null || true)}"
fi

exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8787}"
