# Growth Tracker

Turn your real work — chat sessions, projects you ran, tasks you closed, and notes — into evidenced growth artifacts. Track growth areas, score against a customizable rubric, and generate a shareable accomplishment doc that cites its evidence.

**Growth Tracker** is a **backend + UI app** — it ships its own backend subprocess and contributes a Growth dashboard page to the sidebar.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `backend/server.py` — the app's backend (subprocess behind the gateway proxy).
- `ui/` — the contributed UI: its sources in `ui/src/`, and the built page in
  `ui/bundle/index.mjs`. The bundle is committed, so installing the app copies it and never
  needs Node or npm. After editing `ui/src/`, run `npm ci && npm run build` in `ui/` and
  commit the bundle; CI rebuilds it and fails when the committed file differs.
- `test_server.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- (UI-side: `@personalclaw/app-sdk` — the frontend SDK)

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Growth Tracker** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Backend + UI

- `backend/server.py` — the app's own API, launched as a subprocess and reached through the gateway proxy (`/apps/growth/api/*`).
- `ui/` — the contributed Growth page (route `/apps/growth`).
- Declared permissions: core `api` paths (projects/tasks/knowledge), `events`, `storage`, and `agent` at the `text` tier (drafting an artifact from its evidence and writing a digest hand the model the evidence and artifacts the page sends, with no tools).

### No scheduled capture

Growth no longer declares a daily `daily-capture` job, or the `cron` permission for one. A
scheduled job runs its agent at its app's agent tier, and at Growth's, `text`, the agent is
handed the job's message and nothing else, with no tools: it could neither read your day nor
file an artifact. The job also did none of its work before: the memory-history file it was told
to read is no longer where it looked, and its read-only agent was refused the call that files an
artifact. Widening Growth to the `tools` tier and to your memory, only to keep a job, would grant
far more than the page needs. Capture from the **Sources** tab instead: it offers your completed
projects and closed tasks as candidates, and drafts an artifact from the one you pick at the
`text` tier.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
