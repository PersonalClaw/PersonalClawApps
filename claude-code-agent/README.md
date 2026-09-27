# Claude Code

Run Anthropic's Claude Code as an agent (acp:claude-code) via the Zed ACP adapter. It runs with a Claude config of its own that starts empty, so none of your auto-approve rules come along and every tool asks through PersonalClaw's approval gate. Sign in to Claude once for it; PersonalClaw stores no key.

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
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `isolated_config` | Isolated Claude settings | On by default. Claude runs with `CLAUDE_CONFIG_DIR` set to `<PersonalClaw home>/cc-config`, which starts with an empty `settings.json`: nothing is copied from your `~/.claude`, or from a `CLAUDE_CONFIG_DIR` you set. Off: Claude uses your own `~/.claude`, auto-approve rules included. |
| `model` | Default Model | Optional. Leave empty to use the Claude CLI's own current default (recommended). The Claude adapter advertises the live model set for selection; set this only to pin a specific model. |
| `acp_bin` | ACP Adapter Path | Optional absolute path to the claude-code-acp adapter. Empty auto-resolves: PATH → node-manager dirs → npx @zed-industries/claude-code-acp. Equivalent to the CLAUDE_CODE_ACP_BIN env var. |

## Authentication

Claude Code signs itself in; PersonalClaw stores no API key. With isolated settings on (the default) that sign-in belongs to the isolated config, so sign in once for it: Settings → Providers → Claude Code → **Sign in** runs `claude /login` with `CLAUDE_CONFIG_DIR` pointed at it. Your own `~/.claude` login is not used, and is not touched.

Why: Claude's own permission engine auto-approves whatever your `permissions.allow` rules and `defaultMode` say. An empty config has none of them, so every tool call comes back to PersonalClaw's approval gate.

## Capability boundary

Running an agent over ACP is **not** the same as PersonalClaw's native runtime: some host
features are supplied by PersonalClaw on the CLI's behalf, and a few cannot work at all until the
CLI or the ACP protocol grows a surface for them. What is at parity, what is host-compensated and
what is a constraint is written down per provider — with the CLI and adapter versions each verdict
was measured against — in [the ACP parity statement](https://github.com/PersonalClaw/PersonalClaw/blob/main/docs/agents/acp-parity.md).

## License

MIT — see the apps repo [LICENSE](../LICENSE).
