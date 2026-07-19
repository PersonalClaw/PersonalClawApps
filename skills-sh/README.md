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
any other app. (Or `POST /api/apps {"source": ".../apps/skills-sh"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | API Key | Skills.sh API key for search and install. Get one at skills.sh/settings. Without this, search uses npx CLI (slower) and install may fail. |

## License

MIT — see `LICENSE`.
