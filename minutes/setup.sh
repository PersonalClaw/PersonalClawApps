#!/usr/bin/env bash
# Minutes app setup — build the UI bundle if missing. App data (meetings.db, user
# templates) lives in PERSONALCLAW_APP_DATA_DIR and is never touched here.
set -e
cd "$(dirname "$0")"
if [ ! -f ui/dist/index.mjs ] && [ -d ui ]; then
  echo "Building Minutes UI bundle…"
  (cd ui && npm install --silent && npx vite build) || echo "UI build skipped (npm/vite unavailable)"
fi
echo "Minutes app setup complete."
