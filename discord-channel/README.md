# Discord Channel

Discord bot integration over the raw Gateway WebSocket + REST API. Pair a DM,
converse in servers, and receive results with approval buttons.

**Discord Channel** is a **channel-transport provider** — it implements the
`personalclaw.sdk.channel` `ChannelTransportProvider` contract and shows up under
the messaging channels alongside the dashboard, Slack and Telegram.

> **Read this first: enable the MESSAGE CONTENT intent.** It is a *privileged*
> intent, off by default, and without it Discord delivers every message with an
> **empty `content`**. The bot connects, shows as online, receives events — and
> ignores everything you say. This is the single most common reason a Discord bot
> looks broken. Developer Portal → your application → **Bot** → **Privileged Gateway
> Intents** → enable **Message Content Intent**.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It
ships as a self-contained directory:

- `app.json` — the manifest (provider type `channel`; `implementation` points at
  `discord_runtime.transport:create_provider`).
- `discord_runtime/` — the implementation:
  - `gateway.py` — the Gateway WebSocket client over `websockets` (no vendor SDK):
    identify with the intents bitfield, heartbeat + ACK tracking, session resume,
    and dispatch of `MESSAGE_CREATE` / `INTERACTION_CREATE`. Contains the
    **zombie-connection** detection (a heartbeat unacked by the time the next is due
    means the gateway stopped processing us even though the socket looks open).
  - `api.py` — a thin `httpx`-backed REST client (no vendor SDK), with the one piece
    of real logic Discord forces on every caller: **per-bucket** rate limiting, and
    a global 429 tracked separately from a per-route one.
  - `transport.py` — the inbound event path (trust seam, self-message filter) +
    outbound `send`.
  - `delivery.py` — the `ChannelDelivery` the gateway delivers results through
    (message splitting, throttled edit-streaming, button approvals, reactions).
  - `settings.py` — the app's own DM-activation / application-id config + the
    credential key.
- `cli_setup.py` / `cli_doctor.py` — the app's `personalclaw setup` / `doctor` hooks.
- `test_provider.py` + `tests/` — the app's own tests.

It imports core **only** via the PersonalClaw **SDK** (never core internals), so
core can evolve without breaking it:

- `personalclaw.sdk.channel` — transport ABC, `ChannelMessage`, the sender-trust
  seam (`guard_inbound`), redaction, `_run_chat`, `ProviderSettings`.
- `personalclaw.sdk.cli` — `SetupContext` / `DoctorLine`.

Both wire protocols are implemented directly against libraries that are **already
core dependencies** (`httpx`, `websockets`), so this app declares no
`pythonDependencies` at all — there is no `discord.py` or any other vendor SDK
anywhere in the bundle, tests included.

Who may talk (allowlist, pairing) and which server channels are tracked are owned by
the **core sender-trust seam** (`channel_trust`, provider `"discord"`) — this app
keeps no allowlist of its own. The bot token is a secret in the shared credential
store under this app's own `DISCORD_BOT_TOKEN` key. The **application id** is *not* a
secret (Discord prints it publicly and it appears in every invite URL), so it lives
in the app's own settings where you can see and edit it.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Discord Channel** — the install runs through the security scanner and lifecycle
exactly like any other app. (Or `POST /api/apps {"source": ".../apps/discord-channel"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `bot_token` | Bot Token | Discord bot token from the Developer Portal. Sent as `Authorization: Bot <token>` — the literal `Bot ` prefix is mandatory. Stored as a secret. |
| `application_id` | Application ID | The application's public ID. Used to build the bot invite URL. Not a secret. |
| `dm_activation` | DM Activation | `always` (answer every paired DM), `mention` (only when @-mentioned), or `off`. A DM posture only — it never gags a tracked server channel. |

## Discord bot setup

1. Go to <https://discord.com/developers/applications> → **New Application**, name it.
2. **General Information** → copy the **Application ID**.
3. **Bot** → **Reset Token** → copy the token (shown once).
4. **Bot** → **Privileged Gateway Intents** → enable **MESSAGE CONTENT INTENT**.
   (See the warning at the top — skip this and the bot receives empty messages.)
5. Run `personalclaw setup` and paste the token, application id and your own Discord
   user id (enable Settings → Advanced → **Developer Mode**, then right-click your
   name → **Copy User ID**). The setup step then prints the **OAuth2 invite URL**
   with the permission bits already computed — open it and pick a server.
6. Track the channels you want the bot active in from the Channels page.

The invite requests exactly the permissions the code exercises: View Channels, Send
Messages, Send Messages in Threads, Add Reactions, Attach Files, Read Message
History. Nothing broader.

## How trust works

The transport enforces nothing itself — every inbound message goes through the core
seam, so the policy is identical across channels:

- **DMs pair.** An unknown DM sender gets a canned pairing-needed reply and is never
  routed; run `personalclaw pair discord` for a code. The owner also gets one
  actionable notification per unknown sender (deduped, restart-durable).
- **Server channels are tracked-only.** A message in an untracked channel is dropped
  silently — no owner spam.
- **Non-owner server content is fenced.** It enters the session wrapped so the model
  reads it as data, not instructions.
- **The bot ignores itself.** `MESSAGE_CREATE` fires for the bot's own sends, so
  self-authored (and other-bot) messages are dropped. Without that filter a bot
  answers its own replies forever.

## Tests

```
python -m pytest discord-channel -q
```

No network, no wall-clock sleeps, no vendor SDK: the REST client runs against an
`httpx.MockTransport`, the gateway against a scripted fake WebSocket, and the
heartbeat/throttle clocks are injected. Covers the gateway lifecycle
(identify/heartbeat/ack/resume/dispatch, zombie detection, invalid-session), the
per-bucket and global 429 paths, the approval button round-trip, and the trust
integration against the **real** core seam in an isolated home.

### Owner validation step (not automated)

Validating against a **real Discord application, bot and test server** is an
**owner real-world step** and is deliberately outside automated execution — it needs
a human to create an application in the Developer Portal, enable the privileged
intent, and invite the bot to a server. The automated suite above covers the
protocol; it cannot cover "Discord accepted this token". Run the Channels page →
Discord → **Test** action for the live gateway-hello probe once configured.

## License

MIT — see `LICENSE`.
