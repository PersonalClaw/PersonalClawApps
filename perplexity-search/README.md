# Perplexity Sonar

Answer-first web search: the Sonar models answer a query directly from live web results and return the cited sources. Best when you want a synthesized answer with citations, not just links. API key from perplexity.ai.

**Perplexity Sonar** is a **search provider** — it implements the `personalclaw.sdk.search` `SearchProvider` contract (search + optional fetch) and is selectable under Settings → Search.

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
**Perplexity Sonar** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/perplexity-search"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Perplexity API Key | Your Perplexity API key (perplexity.ai). Leave empty to fall back to the PERPLEXITY_API_KEY environment variable. |
| `timeout_secs` | Request Timeout | Maximum seconds to wait for a Sonar response (answer synthesis can take longer than a plain search). |

## License

MIT — see `LICENSE`.
