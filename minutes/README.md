# Minutes

Tie recordings, videos, notes and docs into one meeting; watch it cohesively on a synced timeline with speaker-attributed transcripts. Tag participants, generate multiple minutes/summaries from templates, and consolidate dates, action items, follow-ups and decisions — then turn action items into a task list under a project.

**Minutes** is a **backend + UI app** — it ships its own backend subprocess and contributes a Minutes page to the sidebar.

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
**Minutes** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Backend + UI

- `backend/server.py` — the app's own API, launched as a subprocess and reached through the gateway proxy (`/apps/minutes/api/*`).
- `ui/` — the contributed Minutes page (route `/apps/minutes`), a synced timeline over recordings, transcripts, notes and docs.
- Declared permissions: core `api` paths (knowledge/lexicon/projects/tasks), `events`, `storage`, and `agent` at the `text` tier: generating minutes and extracting items hands the model the meeting's notes and extracted text the page gathered, and nothing else, with no tools.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
