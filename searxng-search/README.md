# SearXNG

Self-hosted, no-key web search via a SearXNG meta-search instance. Point it at your own SearXNG URL for private, free web search and links.

**SearXNG** is a **search provider** — it implements the `personalclaw.sdk.search` `SearchProvider` contract against your self-hosted SearXNG instance and is selectable under Settings → Search.

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
**SearXNG** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/searxng-search"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | SearXNG Endpoint | Base URL of your SearXNG instance (e.g. https://searxng.example.com). The JSON API must be enabled. |
| `timeout_secs` | Request Timeout | Maximum seconds to wait for a search response. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
