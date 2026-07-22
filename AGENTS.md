# AGENTS.md — brief for coding agents

You are contributing to **PersonalClawApps**: the first-party **app bundles** for
PersonalClaw and the community front door. This is the newcomer ramp — the SDK
contract is the bar, not core's full doctrine. Long form: [CONTRIBUTING.md](CONTRIBUTING.md).

## What an app is

A directory with an `app.json` manifest (`name` kebab-case, `version` semver,
`displayName`, `description` required) plus its implementation, extending a
PersonalClaw gateway via the platform's typed provider contracts. Full contract:
`docs/app-creation-guide.md`, `docs/platform-architecture.md`.

## The hard rules (CI enforces these)

- **SDK boundary:** import core **only** via `personalclaw.sdk.*`. The `boundary`
  job fails on any deeper import. Missing symbol → open a core issue to promote
  it; never reach around.
- **Manifest validity:** `manifest-validate` parses every `app.json` against
  core's real `AppManifest` and requires a stable round-trip.
- **Tests without vendor SDKs:** the `tests` job installs core but **no** vendor
  SDKs — model apps stub theirs. A test importing a real vendor SDK fails here.
  Declare genuine runtime deps in `dependencies.pythonDependencies`.

## Per-app deliverables

`test_provider.py` / `test_server.py`, a `README.md`, a `LICENSE`, and the
**minimum** permission declaration (it's the install-consent surface).

## Validate as a user

Add the app dir as a local Store source, install it in a real gateway, drive it
in the UI. Push edits to a running gateway via
`POST /api/apps/<name>/update {source, confirm:true}`.

## Git / PR rules

- Branch off `main`: `feature-<slug>` / `bugfix-<slug>` / `improvement-<slug>`,
  one concern per branch.
- One conceptual commit per branch (amend + `git push --force-with-lease`);
  `main` is never force-pushed.
- **DCO required:** `git commit -s` on every commit (CI enforces it).
- Owner is the sole author/committer — no agent co-author or session trailers.
- The PR template's app-bar checklist is the contract.

## What gets your PR rejected

- Importing core outside `personalclaw.sdk.*`.
- An `app.json` that fails validation or over-declares permissions.
- A missing/failing `test_provider.py`, or a test that needs a real vendor SDK.
- No app `README`/`LICENSE`.
- Unsigned commits (missing DCO), agent-authored commits, or a force-push to `main`.
- A platform-level change — that belongs in the core repo, not here.
