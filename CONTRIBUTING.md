# Contributing to PersonalClawApps

This repository holds the **first-party app bundles** for PersonalClaw and is the
**community front door** — the place to contribute a new provider, tool, channel,
or capability without touching the high-doctrine core. Reviews here aim to be
faster than in core; the bar is the SDK contract, not core's full doctrine.

Agents should read [AGENTS.md](AGENTS.md) for the compressed version.

## What an app is

A directory with an `app.json` manifest (required: `name` kebab-case, `version`
semver, `displayName`, `description`) plus its implementation. An app extends a
PersonalClaw gateway through the platform's typed provider contracts.

## The front-door bar

A PR that adds or changes an app must meet all of:

- **SDK-only imports.** Import core **only** via `personalclaw.sdk.*` — never deep
  internals. The `boundary` CI job rejects anything else. Need a symbol that
  isn't exposed? Open a core issue to promote it to the SDK; don't reach around.
- **Declare the minimum permissions.** The Store shows an app's declared
  permissions as the install-consent surface — request only what the app needs.
- **Declare runtime dependencies in the manifest.** Vendor SDKs go in
  `dependencies.pythonDependencies` (the app-install pipeline installs them);
  don't assume core ships them.
- **Ship tests.** `test_provider.py` / `test_server.py` that pass under the
  `tests` CI job. The job installs each bundle's declared
  `dependencies.pythonDependencies`; undeclared vendor SDKs remain absent, so
  model apps must continue to stub SDKs they deliberately do not declare.
- **Ship a `README.md` and a `LICENSE`** in the app directory.
- **Manifest completeness.** `manifest-validate` parses every `app.json` against
  core's real `AppManifest` parser and requires a stable round-trip.

Run one bundle locally with the same dependency posture as CI:

```bash
./scripts/test-bundles <bundle>
```

The runner installs that bundle's declared Python dependencies into the active
Python environment with `uv`, then invokes pytest. With no bundle argument it
runs every tested bundle, matching the CI job.

Full contract: [`docs/app-creation-guide.md`](docs/app-creation-guide.md) and
[`docs/platform-architecture.md`](docs/platform-architecture.md).

## Validate as a user

Add your app directory as a local Store source, install it in a real gateway, and
drive it in the UI before opening the PR. From a shell an install is a review and then
an install that carries the review's `consent` digest
([docs/third-party-install.md](docs/third-party-install.md#installing-from-a-shell)).
Push edits to a running gateway with `POST /api/apps/<name>/update {source}`; one that
changes what the app gets needs the same review and digest.

## DCO

Every commit must carry a `Signed-off-by` trailer (`git commit -s`) — the same
[Developer Certificate of Origin](https://developercertificate.org/) as core,
enforced by CI. It certifies you can submit the change under the project's MIT
license, without a CLA.

## Git hygiene

- Branch off `main`: `feature-<slug>` / `bugfix-<slug>` / `improvement-<slug>`,
  one concern per branch.
- One conceptual commit per branch (amend + `git push --force-with-lease`);
  `main` is never force-pushed.
- Owner is the sole author/committer — no agent co-author or session trailers.

## License

By contributing you agree your contributions are licensed under the project's
[MIT License](LICENSE).
