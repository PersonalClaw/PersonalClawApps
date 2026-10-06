# Skills.sh Marketplace

Browse and install community skills from skills.sh. Search, preview, and manage external skill packages.

**Skills.sh Marketplace** is a **skills-marketplace source** — it implements the `personalclaw.sdk.skill` `SkillsMarketplace` contract (read-only `search` + `fetch`) and appears in Skills → Browse.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (provider type + `implementation`; Tier-2 apps carry no `native` flag — that's Tier-1-only).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.skill`
- `personalclaw.sdk.settings`
- `personalclaw.sdk.credentials`
- `personalclaw.sdk.util`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Skills.sh Marketplace** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | API Key | Skills.sh API key for search and install. Get one at skills.sh/settings. Without one, each search runs the skills command line through npx, which downloads and runs the skills npm package, and showing or installing a skill clones its repository from GitHub. |

## What it starts

Install consent names these before anything installs (the manifest's `launches` and `writes`).
Both run only without a skills.sh API key, and a search also when the skills.sh API fails:

- Each search starts `npx`, as you and outside PersonalClaw. npx fetches the newest version of
  the npm package `skills` from the npm registry, unless npm already holds it, and runs
  `skills find` with what you typed; npm runs the install scripts of that package and of every
  package it depends on. It reaches `registry.npmjs.org` and `skills.sh`, runs with your own
  npm sign-in and settings (`~/.npmrc`), and keeps what it downloads in npm's cache
  (`~/.npm`).
- Showing or installing a skill starts `git`, which clones the skill's repository from
  `github.com` into a temporary folder, removed once its files are read, with your own git
  sign-in and settings.

With an API key, search and fetch go to the skills.sh API from the app's own code instead.

## Network

Reaches `skills.sh`, clones a skill's repository from `github.com`, and runs the `skills` CLI through `npx`, which fetches it from the npm registry. A request to the skills.sh API is checked first by PersonalClaw's egress guard under your **Settings → Security → Network egress** rules, and sent through it, each redirect included, so it is refused when you put `skills.sh` (or a host it sends the request on to) on Denied hosts, and when the check itself cannot run. A search refused there is not tried again through the `skills` CLI.

## License

MIT — see `LICENSE`.
