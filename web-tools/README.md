# Native Tools (Web)

General web primitives the agent can call — web_search (over the Search entity you bind in Settings → Search), web_fetch (SSRF-guarded page fetch + extraction), and web_extract (structured extraction). No key of its own — it uses the bound search provider's.

**Native Tools (Web)** is a **tool provider** — it contributes the general web primitives — `web_search`, `web_fetch`, `web_extract` — to the agent tool layer via `personalclaw.sdk.tool`.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.mcp`
- `personalclaw.sdk.net`
- `personalclaw.sdk.search`
- `personalclaw.sdk.security`
- `personalclaw.sdk.tool`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Native Tools (Web)** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/web-tools"}`.)

## Tools

- `web_search` — searches over the Search provider you bind in Settings → Search.
- `web_fetch` — SSRF-guarded page fetch + extraction.
- `web_extract` — structured extraction from a page.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
