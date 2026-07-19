# Exa

Neural/semantic web search: embeddings-ranked results with relevant highlights and extracted content, plus single-URL content retrieval. Good for discovering non-obvious sources. API key from exa.ai.

**Exa** is a **search provider** — it implements the `personalclaw.sdk.search` `SearchProvider` contract (search + optional fetch) and is selectable under Settings → Search.

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
**Exa** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/exa-search"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Exa API Key | Your Exa API key (exa.ai). Leave empty to fall back to the EXA_API_KEY environment variable. |
| `timeout_secs` | Request Timeout | Maximum seconds to wait for a search/contents response. |

## License

MIT — see `LICENSE`.
