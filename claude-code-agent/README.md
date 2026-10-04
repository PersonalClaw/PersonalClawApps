# Claude Code

Run Anthropic's Claude Code as an agent (acp:claude-code) via the Zed ACP adapter. It runs with a Claude config of its own that starts empty, and loads nothing from the folder it works in (no .claude settings, .mcp.json servers, CLAUDE.md or skills), so no auto-approve rule of yours or of a repository's comes along: apart from the file reads and read-only commands Claude Code allows itself, every tool asks through PersonalClaw's approval gate. Sign in to Claude once for it; PersonalClaw stores no key. Enabling it installs its ACP adapter (@agentclientprotocol/claude-agent-acp) from npm into your PersonalClaw home when no copy is installed; a failed install says why on its card, with Retry.

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
| `isolated_config` | Isolated Claude settings | On by default. Claude runs with `CLAUDE_CONFIG_DIR` set to `<PersonalClaw home>/cc-config`, which starts with an empty `settings.json`: nothing is copied from your `~/.claude`, or from a `CLAUDE_CONFIG_DIR` you set. And each session loads nothing from the folder it works in: no `.claude/settings.json`, `.claude/settings.local.json`, `.mcp.json`, `CLAUDE.md` or skills. Off: Claude uses your own `~/.claude`, auto-approve rules included, and the folder's own Claude settings. |
| `model` | Default Model | Optional. Leave empty to use the Claude CLI's own current default (recommended). The Claude adapter advertises the live model set for selection; set this only to pin a specific model. |
| `acp_bin` | ACP Adapter Path | Optional absolute path to the claude-agent-acp adapter. Empty finds it: PATH → node-manager dirs → the copy PersonalClaw installed in its home when you enabled the app → npx @agentclientprotocol/claude-agent-acp. Equivalent to the CLAUDE_CODE_ACP_BIN env var. |

## Authentication

Claude Code signs itself in; PersonalClaw stores no API key. With isolated settings on (the default) that sign-in belongs to the isolated config, so sign in once for it: Settings → Providers → Claude Code → **Sign in** runs `claude /login` with `CLAUDE_CONFIG_DIR` pointed at it. Your own `~/.claude` login is not used, and is not touched.

Why: Claude's own permission engine auto-approves whatever the `permissions.allow` rules it loads say, and it loads them from two kinds of place: your own config, and the folder it works in. A repository's `.claude/settings.json` and `.claude/settings.local.json` can add rules of their own, hooks (commands Claude Code runs at its events) and an `env` block, and its `.mcp.json` names MCP servers to start. Claude Code shows no trust prompt for a folder when it runs under the Agent SDK, as it does here, so none of that waits for you to accept it (Claude Code's permissions documentation, "What runs before you trust a folder").

So isolation has two halves. `CLAUDE_CONFIG_DIR` points your own scope at the empty config. And every session asks the ACP adapter for Claude Code's `user` setting source alone (`_meta.claudeCode.options.settingSources`, the Agent SDK's `settingSources`, which is `--setting-sources user` on Claude Code's command line): the folder's settings files, `.mcp.json` servers, `CLAUDE.md` and skills are not loaded, which is what Claude Code's documentation says to do for a repository you did not write. With isolation on, Claude therefore loads nothing from the folder it works in, and apart from the file reads and read-only commands Claude Code allows itself, every tool call comes back to PersonalClaw's approval gate. What still applies is your organization's managed Claude Code settings, if this machine has any, and whatever you add to `cc-config` yourself.

One setting is read apart from the rest. The ACP adapter starts each session in the permission
mode the settings it can see name (`permissions.defaultMode`, such as `acceptEdits`, which
approves file edits without asking), and it reads every source for that, the folder's included,
whatever a session asks Claude Code to load. PersonalClaw sets each session to the mode that
asks before its first turn, with isolation on or off, so a `defaultMode` of yours or of a
repository's does not carry into a chat. The one exception is an unattended run that your
approval settings let approve its own calls.

Claude can still open the folder's files, a `CLAUDE.md` among them, as it opens any other: what isolation takes away is the loading of one at the start of every session. To have the folder's `CLAUDE.md`, skills and settings load as they do in your terminal, turn **Isolated Claude settings** off.

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

## What it starts and installs

Install consent names each of these before anything installs (the manifest's `launches`,
`writes` and `dependencies.npmPackages`):

- It starts the `claude` program installed on this machine, as you and outside PersonalClaw.
  While **Isolated Claude settings** is off, Claude runs with your own Claude sign-in, settings
  and auto-approve rules, and with the Claude settings of the folder it works in, which can add
  rules of their own and commands for it to run; what those rules allow it does without asking
  here first.
- While that setting is on (the default), Claude runs with `cc-config` in your PersonalClaw
  folder, a config of its own that starts empty and keeps the sign-in you make for this app,
  and loads nothing from the folder it works in.
- Switching it on installs the npm package `@agentclientprotocol/claude-agent-acp` into your
  PersonalClaw folder (`acp-adapters`) when no copy is on this machine.

## When its context fills

Claude Code compacts its own conversation when its context fills: a summary takes the place of
its older turns, and the session goes on, as it does in a terminal. Its automatic compaction is
on unless you turned it off in Claude Code's own settings. This app tells PersonalClaw so
(`compacts_itself`), and PersonalClaw leaves the session to it at **Settings → Chat →
Auto-compact threshold**: the chat keeps Claude Code's session, with the results of its earlier
tool calls, instead of restarting it.

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
