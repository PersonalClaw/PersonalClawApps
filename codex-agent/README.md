# OpenAI Codex

Run the OpenAI Codex CLI as an agent (acp:codex) via the Zed ACP adapter. Codex manages its own configuration and authentication; every tool call routes through PersonalClaw's host approval gate.

**OpenAI Codex** is an **ACP agent bundle** — it registers an `acp:codex` agent via `personalclaw.sdk.acp` and appears in the Agents list.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.acp`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**OpenAI Codex** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/codex-agent"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `model` | Default Model | Optional. Leave empty to use the Codex CLI's own current default (recommended). The adapter advertises the live model set for selection; set this only to pin a specific model. |
| `acp_bin` | ACP Adapter Path | Optional absolute path to the codex-acp adapter. Empty auto-resolves: PATH → node-manager dirs → npx @zed-industries/codex-acp. Equivalent to the CODEX_ACP_BIN env var. |

## Authentication

The Codex CLI manages its own configuration and login. Every tool call routes through PersonalClaw's host approval gate.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
