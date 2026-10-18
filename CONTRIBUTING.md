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
  `tests` CI job. The job runs each bundle in an environment holding exactly its
  declared `dependencies.pythonDependencies`; undeclared vendor SDKs remain absent, so
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
uv pip install --python .venv/bin/python "personalclaw @ git+https://github.com/PersonalClaw/PersonalClaw@$(cat core.lock)" pytest==9.1.1 pytest-asyncio==1.4.0 pytest-xdist==3.8.0
. .venv/bin/activate
./scripts/test-bundles <bundle>
```

The runner starts `python` from your `PATH`, or the interpreter `PYTHON` names
(`PYTHON=.venv/bin/python ./scripts/test-bundles <bundle>` needs no activation).
It runs each bundle in an environment of its own under `.bundle-venvs/`, holding core,
the pytest pins and exactly the Python dependencies the bundle's `app.json` declares,
installed from the bundle lock with every file checked against its hash. So a bundle
never sees a package another bundle declares. Bundles that declare the same
dependencies share one environment; one that declares none gets the base environment.
An environment is built once and reused while its key holds: the core commit, the exact
requirements, the Python version and ABI, the OS, the architecture, the uv version and
the index. It is built under a lock and renamed into place only when complete. It is
reused only while it still holds what its receipt recorded and its interpreter is still
the Python its key names (a `uv python upgrade` re-points the folder it links through);
otherwise it is built again. One checkout runs one bundle runner at a time: a second
run (or a second gate's `bundles` part) in the same checkout refuses at start and names
the first; to validate two things at once, use a second checkout. Each run removes the
folders a stopped build left and environments no run has used for seven days.
A bundle whose tests install or remove a package fails the run by name: when several
bundles shared the environment while it changed, each is run again alone to find which.
Those runs alone only diagnose: a bundle's result is always its first run's.
Each bundle's name is printed when it starts, and each build names the bundles waiting
on it, so a hang names what it is in. A full run that finds no tested bundle exits 2.

- `--jobs N` runs N bundles at once (default: half the CPUs, at most 8; CI passes 4).
  Each still runs in its own pytest process, and its output prints as one block.
- `--changed <base>` runs only the bundles whose folder differs from `<base>`
  (`git diff <base>...HEAD` and your uncommitted changes; a rename counts as both its old
  and its new path). A change outside the bundles
  that can change every bundle's run (`core.lock`, `scripts/`, `apps_testkit/`,
  `conftest.py`, `pytest.ini`, `.github/tests/`, the bundle lock, a workflow, ...) runs
  every bundle, and so does an unknown base, a shallow clone or any git error. Docs,
  root `*.md` files and the GitHub templates select no bundle. Every bundle whose
  `app.json` claims `quality.tested` runs whatever changed, and the selection's reason
  names them: the quality job judges that claim from this run's results only.
- `--results DIR` writes `DIR/results.json` (with each bundle's JUnit beside it). Its
  `apps` list, `{"app", "outcome": "pass" | "fail", "junit"}` per bundle that finished, is
  what `python -m personalclaw.apps.quality . --results DIR` reads instead of running any
  suite again. A claimant with a `fail`, or with no entry, fails that check.
- `--list` prints the selection and why, and runs nothing.

With no bundle argument it runs every tested bundle, matching the CI job.

**The bundle lock.** `.github/bundle-lock/<platform>/` (`linux-x86_64` for CI,
`darwin-arm64` for a Mac) holds a generated `pyproject.toml` and its `uv.lock`: core at
the `core.lock` commit, the pytest pins, and one extra per bundle that declares
`pythonDependencies`. After you change a bundle's `pythonDependencies` or `core.lock`, run
`python scripts/bundle_lock.py write` and commit both platforms' files; the
`bundle-lock` static check fails until you do, and the runner refuses a bundle whose
declared dependencies the lock does not hold. Each platform has its own lock because one
lock listing both dropped `sherpa-onnx-core` from `diarization-onnx`'s environment. When
two bundles' ranges conflict, declare the pair in `CONFLICTS` in
`scripts/bundle_lock.py` (uv's conflicting extras); never drop a bundle from the lock.
No other platform has a lock yet; there, rely on CI.

The whole gate CI runs is one command, from the same environment:

```bash
make gate            # or: python scripts/gate.py
```

It runs every static check in `.github/scripts/` (each is a row of its own: `boundary`,
`manifest-validate`, `ui-bundles`, ...), then `.github/tests`, then every bundle's suite,
and keeps going past a red. `make static`, `make rails` and `make bundles` run one part, as
each CI job does; the gate passes `--jobs`, `--changed` and `--bundle` on to the runner,
and `make gate GATE_ARGS="--jobs 4"` passes them through make. Every run writes a receipt
(the apps and core commits, the commit `core.lock` pins and whether the installed core is
it, the toolchain, the bundles it chose and why, and each bundle environment's key and
inputs) to `.local/scratch/gate/receipt.json`. One results folder holds one run: a second
gate started while one is running there refuses, and `--results DIR` runs it in another
folder. The bundle environments always hold the locked core; the weekly canary alone
passes `--float`, which resolves each bundle's declared ranges afresh against the core it
tests, and its result is never a pull request's verdict. A local run against another core warns and
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

### Supply chain

CI's inputs are pinned, and every job holds only the GitHub token permissions it needs.
`.github/tests/test_supply_chain.py` holds both (it runs in the gate's `rails` part, so
`make gate` and CI's `tests` job), and `.github/supply-chain.toml` holds its Node
end-of-life table, the audited write steps and the exceptions.

How to pin:

- **An action:** `uses: owner/action@<40-character commit SHA> # vX.Y.Z`, the SHA of
  that release tag. Dependabot's weekly `github-actions` update moves the SHA and its
  comment together.
- **An image** (`container:`, `services:`, `docker://`, a Dockerfile `FROM`):
  `name@sha256:<digest>` for one platform, with the platform beside it (`# linux/amd64`,
  or `--platform=` on a `FROM`). An image named through an `ARG` is judged by the
  `ARG`'s default. None exists today.
- **A tool a `run:` line installs:** an exact version (`pytest==9.1.1`,
  `npm install -g name@1.2.3`), or from a lock (`npm ci`, `-r` with
  `--require-hashes`). Core comes from `"$CORE_SPEC"`, which `core.lock` pins; any other
  variable is a version the check cannot read. The check sees `run:` lines only, not an
  install a script runs. The bundle runner's installs are a script's, and it holds them
  to the same bar itself: every package from the bundle lock with its hash
  (`--require-hashes`), and core from its locked commit; a package the lock holds without
  a hash is refused. Only the canary's `--float` run resolves declared ranges afresh.
- **A setup step:** an exact `x.y.z` or a version file, never `check-latest`. Every
  Python a `uv` command names (`--python 3.12.15`) is exact too.
- **Node:** `.nvmrc` is an exact release, on a line `node_end_of_life` lists and has not
  passed, that meets every locked package's `engines.node`. When the repository moves to
  a new line, add it to the table and leave the old lines in place.
- **A `package.json` CI installs from** (each app's `ui/`) has
  `packageManager: npm@<exact>`.

Least privilege:

- Every job declares its permissions, or its workflow does at the top. No `write-all`,
  and a write at the top must reach a job that uses it.
- Every write permission a job holds, and every `actions/checkout` that keeps its
  credentials, is an `[[audited_write]]` entry: the workflow, the job, the permission
  (or `persist-credentials`), the steps that use it and why. Today's one entry is the
  core-main canary's `report` job, which holds `issues: write` for its tracking issue.
  Every other checkout sets `persist-credentials: false`.
- A token goes in the `env:` of the steps that call the API, never a job's or a
  workflow's.

Every job sets `timeout-minutes`, so a hung step frees its runner instead of holding it
for hours. Size a new job's cap at about twice its p95 over its retained runs, never below
its longest run, rounded up to the next 5 minutes (at least 5); a job with no runs yet takes
its nearest sibling's cap, or 30. A job that calls a reusable workflow (`uses:` at job
level) carries its callee's caps and sets none. `.github/tests/test_every_job_has_a_timeout.py`
holds this in the gate's `rails` part.

To add an exception, when a pin cannot hold yet, add an `[[exception]]` entry with:

- `file`: the file the violation names;
- `reference`: its reference, as the check prints it;
- `scope`: the rule id it waives;
- `owner`;
- `reason`: why, and what ends it;
- `expires`: a date at most 30 days out.

To renew one, set a new `expires` and update its `reason`. The check fails on an
exception that has expired, is more than 30 days out, matches no violation, or is
missing a field. The policy file's own rules are never waived. No exception is in force
today: the quality checks in CI and in the canary read the bundle results their gate
wrote, so neither installs an app's dependency ranges any more.

Not here yet: the repository security settings, the scheduled pin-refresh pull requests
from a GitHub App and the weekly SHA-to-tag check, dependency review and CodeQL in the
merge verdict, and the identities a release records. Nothing refreshes the pytest pins in
the workflows until Dependabot's `uv` ecosystem watches them.

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
