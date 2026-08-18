# Slack Channel

Slack workspace integration. Monitor channels, respond to mentions, and interact via Slack.

**Slack Channel** is a **channel-transport provider** — it implements the `personalclaw.sdk.channel` `ChannelTransportProvider` contract and shows up under the messaging channels.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (provider type + `implementation`; Tier-2 apps carry no `native` flag — that's Tier-1-only).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.channel`
- `(vendored Slack client — app-local)`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Slack Channel** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/slack-channel"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `bot_token` | Bot Token | Slack Bot User OAuth Token (xoxb-...). |
| `app_token` | App Token | Slack App-Level Token for Socket Mode (xapp-...). |

## The live-writes kill switch

A `chat.postMessage` is a live, outward write a whole workspace sees — and is
notified about — before any undo could run, so this transport honors the platform's
process-wide `PERSONALCLAW_DISABLE_LIVE_WRITES` switch, the same one core applies to
non-GET egress and local-model deletion.

With the switch set, `send()` transmits nothing and returns a **typed refusal**
(`SendRefused`) instead. It is falsy, so every existing "did it send?" caller keeps
reading "not delivered" unchanged, but a caller that cares can tell a suppressed write
from a failed one with `isinstance(result, SendRefused)` — the two demand opposite
responses, and a bare `False` would conflate them.

Parsing follows the platform's fail-safe rule exactly: an **absent** variable allows
writes (the switch is opt-in), an explicit `0`/`false`/`no`/`off` turns the guard off,
and **any other present value — including a typo — turns it on**.

## Slack app setup

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**,
   and paste `slack-manifest.yaml` (replace `{{USERNAME}}` with your name).
2. Socket Mode: toggle it OFF then back ON to trigger the token-generation
   dialog → add the `connections:write` scope → **Generate** → copy the
   `xapp-...` App-Level Token.
3. **Install to Workspace** and copy the Bot User OAuth Token (`xoxb-...`).
4. Enter both tokens in the app's Configure form (Settings above), or run
   `personalclaw setup` and paste them when prompted.

The first person to DM the bot is auto-claimed as the owner. Use
`/personalclaw @user` to allowlist more users and `/personalclaw #channel` to
track a channel.

## License

MIT — see `LICENSE`.
