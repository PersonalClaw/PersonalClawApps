# OpenAI Codex

Run the OpenAI Codex CLI as an agent (acp:codex) via the Zed ACP adapter. Codex manages its own configuration and authentication; every tool call it asks about routes through PersonalClaw's host approval gate. Enabling it installs its ACP adapter (@agentclientprotocol/codex-acp) from npm into your PersonalClaw home when no copy is installed; a failed install says why on its card, with Retry.

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
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `model` | Default Model | Optional. Leave empty to use the Codex CLI's own current default (recommended). The adapter advertises the live model set for selection; set this only to pin a specific model. |
| `acp_bin` | ACP Adapter Path | Optional absolute path to the codex-acp adapter. Empty finds it: PATH → node-manager dirs → the copy PersonalClaw installed in its home when you enabled the app → npx @agentclientprotocol/codex-acp. Equivalent to the CODEX_ACP_BIN env var. |

## Authentication

The Codex CLI manages its own configuration and login. Every tool call it asks about routes through PersonalClaw's host approval gate; what its own rules allow, it runs without asking.

## Environment

Codex runs with none of the gateway's environment except what it needs to start, the Codex
binary this app resolves for it (`CODEX_PATH`) and `CODEX_HOME`, the folder whose `config.toml`
names the provider and model Codex uses. `CODEX_HOME` is passed when it is set in the gateway's
environment, so a Codex configured there keeps its provider.

No credential is passed: not an API key or a token. Codex keeps its sign-in in that folder. To
hand it one more variable, add its name under Settings → Security → Child environment
passthrough.

## What it starts and installs

Install consent names each of these before anything installs (the manifest's `launches` and
`dependencies.npmPackages`):

- It starts the `codex` program installed on this machine, as you and outside PersonalClaw,
  with your own Codex sign-in, settings and auto-approve rules: what those rules allow, it does
  without asking here first.
- Switching it on installs the npm package `@agentclientprotocol/codex-acp` into your
  PersonalClaw folder (`acp-adapters`) when no copy is on this machine.

## Capability boundary

ACP providers are not at native parity, and the differences are documented rather than implied.
See [the ACP parity statement](https://github.com/PersonalClaw/PersonalClaw/blob/main/docs/agents/acp-parity.md) for what is at parity, what PersonalClaw compensates for on
codex's behalf, and what is a protocol or CLI constraint — each with the verified version it was
measured against.

## Network

Runs Codex, which reaches OpenAI's service with the account it signs in to; its ACP adapter is installed from the npm registry when this computer doesn't have it.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
