# DuckDuckGo

Keyless, zero-config web search — no API key and no self-hosted instance. The out-of-box default so web_search works for everyone; bind a higher-quality provider (Tavily/Exa/…) per use-case in Search to upgrade. Links + snippets only.

**DuckDuckGo** is a **search provider** — it implements the `personalclaw.sdk.search` `SearchProvider` contract and is selectable under Settings → Search. Keyless — the out-of-box default.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.net`
- `personalclaw.sdk.search`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**DuckDuckGo** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/duckduckgo-search"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `timeout_secs` | Request Timeout | Maximum seconds to wait for a search response. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
