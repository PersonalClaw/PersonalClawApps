<!--
Thanks for the PR. Fill in the sections below. See CONTRIBUTING.md / AGENTS.md for
the front-door bar. Every commit must be signed off (DCO): `git commit -s`.
-->

## What changed

<!-- New app, or a change to an existing one. Which app(s)? -->

## App-bar checklist

<!-- Tick what applies; a new/changed app should meet all of these. -->

- [ ] Imports core **only** via `personalclaw.sdk.*` (boundary job passes)
- [ ] Declares the **minimum** permissions in `app.json`
- [ ] Declares runtime deps in `dependencies.pythonDependencies` (if any)
- [ ] Ships `test_provider.py` / `test_server.py` that pass without vendor SDKs
- [ ] Has a `README.md` and `LICENSE` in the app directory
- [ ] `manifest-validate` passes (parses + round-trips against core's parser)

## What you validated as a user

<!-- Installed it in a real gateway from a local source and drove it in the UI. -->

## Docs touched

<!-- App README, and any docs/ change. "none" if genuinely none. -->
