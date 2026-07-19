#!/usr/bin/env bash
# Growth Tracker setup — build the UI bundle if missing + seed nothing (rubric default
# ships in code; user data lives in PERSONALCLAW_APP_DATA_DIR, never clobbered here).
set -e
cd "$(dirname "$0")"
if [ ! -f ui/dist/index.mjs ] && [ -d ui ]; then
  echo "Building Growth UI bundle…"
  (cd ui && npm install --silent && npx vite build) || echo "UI build skipped (npm/vite unavailable)"
fi
echo "Growth Tracker setup complete."
