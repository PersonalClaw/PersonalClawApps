# Tavily

Agent-optimized web search: a synthesized answer, scored results, optional extracted page content, and single-URL extraction. Best out-of-box quality; free-tier API key.

**Tavily** is a **search provider** — it implements the `personalclaw.sdk.search` `SearchProvider` contract (search + optional fetch) and is selectable under Settings → Search.

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
**Tavily** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/tavily-search"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Tavily API Key | Your Tavily API key (tavily.com). Leave empty to fall back to the TAVILY_API_KEY environment variable. |
| `timeout_secs` | Request Timeout | Maximum seconds to wait for a search/extract response. |

## License

MIT — see `LICENSE`.
