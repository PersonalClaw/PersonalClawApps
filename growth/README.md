# Growth Tracker

Turn your real work — chat sessions, projects you ran, tasks you closed, and notes — into evidenced growth artifacts. Track growth areas, score against a customizable rubric, and generate a shareable accomplishment doc that cites its evidence.

**Growth Tracker** is a **backend + UI app** — it ships its own backend subprocess and contributes a Growth dashboard page to the sidebar.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `backend/server.py` — the app's backend (subprocess behind the gateway proxy).
- `ui/` — the contributed UI (built to `ui/dist/index.mjs` by `setup.sh` on install).
- `setup.sh` — the `onInstall`/`onUpdate` hook (builds the UI bundle if missing).
- `test_server.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- (UI-side: `@personalclaw/app-sdk` — the frontend SDK)

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Growth Tracker** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/growth"}`.)

## Backend + UI

- `backend/server.py` — the app's own API, launched as a subprocess and reached through the gateway proxy (`/apps/growth/api/*`).
- `ui/` — the contributed Growth page (route `/apps/growth`).
- A daily `daily-capture` cron (18:03) scans your recent work and files growth artifacts per `DAILY_CAPTURE.md`.
- Declared permissions: core `api` paths (projects/tasks/knowledge), `events`, `storage`, `agent`, `cron`.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
