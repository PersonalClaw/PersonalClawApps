# Contributing to PersonalClawApps

This repository holds the **first-party app bundles** for PersonalClaw and is the
**community front door** — the place to contribute a new provider, tool, channel,
or capability without touching the high-doctrine core. Reviews here aim to be
faster than in core; the bar is the SDK contract, not core's full doctrine.

Agents should read [AGENTS.md](AGENTS.md) for the compressed version.

## What an app is

A directory with an `app.json` manifest (required: `name` kebab-case, `version`
`MAJOR.MINOR.PATCH`, `displayName`, `description`) plus its implementation. An app extends a
PersonalClaw gateway through the platform's typed provider contracts.

## The front-door bar

A PR that adds or changes an app must meet all of:

- **SDK-only imports.** Import core **only** via `personalclaw.sdk.*` — never deep
  internals. CI's `boundary` check rejects anything else. Need a symbol that
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
- **Nothing published names what is private.** This repository is public, and so
  is everything that reaches it: code, comments, tests, fixtures, READMEs, docs,
  commit messages and PR text. Never write an employer's or any organisation's
  internal names, hosts, tools, documents, people or ticket ids into any of it,
  nor a reference to private planning (a plan's name, a work-item id, a ledger
  row, a ruling). Describe the behaviour and its reason in product terms instead:
  "validate at the boundary", not the title of the document that taught it.
  Fixtures use invented names and the domains RFC 2606 reserves (`example.com`,
  `.test`, `.invalid`), never a real person, account, workspace or id, and a
  binary fixture carries no identity metadata (no author, company or account in a
  document's or an image's properties). This is a practice, not a check: a list
  of names to catch, even as digests, would publish every name on it and still
  never be complete, so none exists and none should be added. Re-read your diff
  for it before you commit.

Run one bundle locally with the same dependency posture as CI, from a Python
environment that holds core and pytest, as the CI `tests` job builds it. CI tests
against one core commit, the one `core.lock` at the repository root pins (one line,
a full 40-character commit), so a re-run of an old CI run tests the same core.
Install that core:

```bash
uv venv --python 3.12.15 .venv
uv pip install --python .venv/bin/python "personalclaw @ git+https://github.com/PersonalClaw/PersonalClaw@$(cat core.lock)" pytest pytest-asyncio pytest-xdist
. .venv/bin/activate
./scripts/test-bundles <bundle>
```

The runner starts `python` from your `PATH`, or the interpreter `PYTHON` names
(`PYTHON=.venv/bin/python ./scripts/test-bundles <bundle>` needs no activation).
It installs that bundle's declared Python dependencies into that environment
with `uv`, then invokes pytest. With no bundle argument it runs every tested
bundle, matching the CI job.

The whole gate CI runs is one command, from the same environment:

```bash
make gate            # or: python scripts/gate.py
```

It runs every static check in `.github/scripts/` (each is a row of its own: `boundary`,
`manifest-validate`, `ui-bundles`, ...), then `.github/tests`, then every bundle's suite,
and keeps going past a red. `make static`, `make rails` and `make bundles` run one part, as
each CI job does. Every run writes a receipt (the apps and core commits, the commit
`core.lock` pins and whether the installed core is it, the toolchain and the bundles it
chose) to `.local/scratch/gate/receipt.json`. A local run against another core warns and
goes on, so you can work against a core that is not pushed yet; `--core <sha>` refuses to
run against any other core, and CI passes it the locked commit.

### Core main and the `core.lock` bump

Core main's own drift does not turn an apps pull request red: the weekly
`core-main-canary.yml` workflow runs every core-dependent gate (every static check, the
repository's tests, every bundle's suite and the quality declarations) against core
main, and opens one issue titled "Apps fail against core main" when that fails, or
comments on the open one. A green run closes it. It runs on a schedule and on dispatch
only, so its result never counts as a pull request's verdict. Dispatch it with `core`
set to a branch, tag or full commit to test another core.

`core-lock-bump.yml` runs daily and on dispatch, and writes to its job summary the
commit `core.lock` pins and core main's head. A bump reaches `core.lock` only as a pull
request that changes nothing else and merges on its own CI. Opening and updating that
one pull request is deferred until the pin-refresh GitHub App it needs exists: so are
its fixed branch, and how the App's commit passes the DCO check. Until then the
workflow reports the commit it would pin and opens nothing, and a maintainer bumps the
pin by hand in a pull request like any other.

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
