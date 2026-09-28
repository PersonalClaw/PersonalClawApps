# Claude Code

Run Anthropic's Claude Code as an agent (acp:claude-code) via the Zed ACP adapter. It runs with a Claude config of its own that starts empty, so none of your auto-approve rules come along and every tool asks through PersonalClaw's approval gate. Sign in to Claude once for it; PersonalClaw stores no key. Enabling it installs its ACP adapter (@agentclientprotocol/claude-agent-acp) from npm into your PersonalClaw home when no copy is installed; a failed install says why on its card, with Retry.

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
| `acp_bin` | ACP Adapter Path | Optional absolute path to the claude-agent-acp adapter. Empty finds it: PATH → node-manager dirs → the copy PersonalClaw installed in its home when you enabled the app → npx @agentclientprotocol/claude-agent-acp. Equivalent to the CLAUDE_CODE_ACP_BIN env var. |

## Authentication

Claude Code signs itself in; PersonalClaw stores no API key. With isolated settings on (the default) that sign-in belongs to the isolated config, so sign in once for it: Settings → Providers → Claude Code → **Sign in** runs `claude /login` with `CLAUDE_CONFIG_DIR` pointed at it. Your own `~/.claude` login is not used, and is not touched.

Why: Claude's own permission engine auto-approves whatever your `permissions.allow` rules and `defaultMode` say. An empty config has none of them, so every tool call comes back to PersonalClaw's approval gate.

## Environment

Claude Code runs with none of the gateway's environment except what it needs to start, what this
app sets for it (`CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_EXECUTABLE`) and the variables it reads to pick
its provider, region and models. Each of these is passed when it is set in the gateway's
environment, so Claude Code on Amazon Bedrock or Google Vertex AI keeps the provider you chose:

| Variable | What Claude Code reads it for |
|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | Run on Amazon Bedrock |
| `CLAUDE_CODE_USE_VERTEX` | Run on Google Vertex AI |
| `AWS_PROFILE` | The AWS profile whose credentials Bedrock uses |
| `AWS_REGION`, `AWS_DEFAULT_REGION` | The Bedrock region |
| `ANTHROPIC_MODEL` | The model |
| `ANTHROPIC_SMALL_FAST_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION` | The fast model, and its Bedrock region |
| `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` | The model each alias names |
| `CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID` | The Vertex AI region and project |

No credential is passed: not an API key, an access key or a session token, even under one of these
names. Claude Code takes its keys from its own sign-in, or from the AWS or Google credential files
its profile names. To hand it one more variable, add its name under Settings → Security → Child
environment passthrough.

## Capability boundary

Running an agent over ACP is **not** the same as PersonalClaw's native runtime: some host
features are supplied by PersonalClaw on the CLI's behalf, and a few cannot work at all until the
CLI or the ACP protocol grows a surface for them. What is at parity, what is host-compensated and
what is a constraint is written down per provider — with the CLI and adapter versions each verdict
was measured against — in [the ACP parity statement](https://github.com/PersonalClaw/PersonalClaw/blob/main/docs/agents/acp-parity.md).

## Network

Runs Claude Code, which reaches Anthropic's service with the account it signs in to; its ACP adapter is installed from the npm registry when this computer doesn't have it.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
