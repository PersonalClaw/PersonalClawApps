# MCP Tool Servers

Connect Model Context Protocol (MCP) servers to provide tools via stdio or SSE transport.

**MCP Tool Servers** is a **tool provider** — it connects external MCP servers (stdio or SSE) and contributes their tools to the agent tool layer via `personalclaw.sdk.tool`.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_mcp_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.mcp`
- `personalclaw.sdk.tool`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**MCP Tool Servers** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/mcp-tools"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `transport` | Transport | How to connect to the MCP server. |
| `command` | Command | Command to start the MCP server (stdio transport). |
| `args` | Arguments | Space-separated arguments for the command. |
| `endpoint` | SSE Endpoint | URL for SSE transport (e.g. http://localhost:8080/sse). |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
