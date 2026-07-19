# Wikipedia Search

Keyless encyclopedic search over Wikipedia: ranked articles with intro-extract snippets. No API key — uses the public MediaWiki API. Links-only (web_fetch an article to read its full body). Best for factual/reference lookups.

**Wikipedia Search** is a **search provider** — it implements the `personalclaw.sdk.search` `SearchProvider` contract (search + optional fetch) and is selectable under Settings → Search.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (provider type + `implementation`; Tier-2 apps carry no `native` flag — that's Tier-1-only).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.search`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Wikipedia Search** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/wikipedia-search"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `lang` | Language | Wikipedia language edition to search (e.g. en, de, fr, ja). |
| `timeout_secs` | Request Timeout | Maximum seconds to wait for a search response. |

## License

MIT — see `LICENSE`.
