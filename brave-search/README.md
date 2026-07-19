# Brave Search

Cheap, broad web search: ranked links with descriptions + extra snippets and a recency filter. Links-only (no answer or page content — web_fetch a result to read it). API key from search.brave.com.

**Brave Search** is a **search provider** — it implements the `personalclaw.sdk.search` `SearchProvider` contract (search + optional fetch) and is selectable under Settings → Search.

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
**Brave Search** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/brave-search"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Brave API Key | Your Brave Search API subscription token (search.brave.com). Leave empty to fall back to the BRAVE_API_KEY environment variable. |
| `timeout_secs` | Request Timeout | Maximum seconds to wait for a search response. |

## License

MIT — see `LICENSE`.
