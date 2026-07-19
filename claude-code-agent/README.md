# Claude Code

Run Anthropic's Claude Code as an agent (acp:claude-code) via the Zed ACP adapter. Claude self-authenticates with your `claude` login; PersonalClaw stores no key. Spawned Claude is hardened: an isolated CLAUDE_CONFIG_DIR strips inherited auto-approve permissions so every tool routes through the host approval gate.

**Claude Code** is an **ACP agent bundle** — it registers an `acp:claude-code` agent via `personalclaw.sdk.acp` and appears in the Agents list.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.acp`
- `personalclaw.sdk.util`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Claude Code** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/claude-code-agent"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `model` | Default Model | Optional. Leave empty to use the Claude CLI's own current default (recommended). The Claude adapter advertises the live model set for selection; set this only to pin a specific model. |
| `acp_bin` | ACP Adapter Path | Optional absolute path to the claude-code-acp adapter. Empty auto-resolves: PATH → node-manager dirs → npx @zed-industries/claude-code-acp. Equivalent to the CLAUDE_CODE_ACP_BIN env var. |

## Authentication

Claude Code self-authenticates with your existing `claude` login — PersonalClaw stores no API key. The spawned agent is hardened (isolated session; tool calls route through PersonalClaw's approval gate).

## License

MIT — see the apps repo [LICENSE](../LICENSE).
