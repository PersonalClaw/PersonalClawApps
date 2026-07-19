# OpenAI Tool Servers

Connect OpenAI-compatible tool servers that expose tools via REST API.

**OpenAI Tool Servers** is a **tool provider** — it connects OpenAI-compatible REST tool servers and contributes their tools to the agent tool layer via `personalclaw.sdk.tool`.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_openai_tool_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.net`
- `personalclaw.sdk.tool`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**OpenAI Tool Servers** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/openai-tools"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | Endpoint URL | Base URL of the tool server (e.g. https://tools.example.com). |
| `api_key` | API Key | Optional bearer token for authentication. |
| `tool_filter` | Tool Filter | Comma-separated list of tool names to expose. Leave empty for all. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
