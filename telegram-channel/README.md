# Telegram Channel

Telegram bot integration over the raw Bot API. Pair a chat, converse in DMs, track
groups, and receive results with inline approvals.

**Telegram Channel** is a **channel-transport provider** — it implements the
`personalclaw.sdk.channel` `ChannelTransportProvider` contract and shows up under
the messaging channels alongside the dashboard and Slack.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It
ships as a self-contained directory:

- `app.json` — the manifest (provider type `channel`; `implementation` points at
  `telegram_runtime.transport:create_provider`).
- `telegram_runtime/` — the implementation:
  - `api.py` — a thin `httpx`-backed Bot API client (no vendor SDK), with the one
    piece of real logic Telegram forces on every caller: `429 retry_after` backoff.
    A `TelegramAPI` ABC lets tests swap a fake in.
  - `transport.py` — the `getUpdates` long-poll inbound loop + outbound `send`.
  - `delivery.py` — the `ChannelDelivery` the gateway delivers results through
    (MarkdownV2 rendering, throttled edit-streaming, inline-keyboard approvals).
  - `format.py` — the MarkdownV2 escaper (the classic Telegram footgun, contained).
  - `settings.py` — the app's own DM-activation config + credential key.
- `cli_setup.py` / `cli_doctor.py` — the app's `personalclaw setup` / `doctor` hooks.
- `test_provider.py` + `tests/` — the app's own tests.

It imports core **only** via the PersonalClaw **SDK** (never core internals), so
core can evolve without breaking it:

- `personalclaw.sdk.channel` — transport ABC, `ChannelMessage`, the sender-trust
  seam (`guard_inbound`), redaction, `_run_chat`, `ProviderSettings`, `atomic_write`.
- `personalclaw.sdk.cli` — `SetupContext` / `DoctorLine`.

Who may talk (allowlist, pairing) and which groups are tracked are owned by the
**core sender-trust seam** (`channel_trust`, provider `"telegram"`) — this app keeps
no allowlist of its own. The bot token is a secret in the shared credential store
under this app's own `TELEGRAM_BOT_TOKEN` key.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Telegram Channel** — the install runs through the security scanner and lifecycle
exactly like any other app. (Or `POST /api/apps {"source": ".../apps/telegram-channel"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `bot_token` | Bot Token | Telegram Bot API token from @BotFather (`123456:ABC-...`). Stored as a secret. |
| `dm_activation` | DM Activation | `always` (answer every paired DM), `mention` (only when @-mentioned), or `off`. |

## The live-writes kill switch

Sending a Telegram message is a live, outward, un-sendable write, so this transport
honors the platform's process-wide `PERSONALCLAW_DISABLE_LIVE_WRITES` switch — the same
one core applies to non-GET egress and local-model deletion.

With the switch set, `send()` transmits nothing and returns a **typed refusal**
(`SendRefused`) instead. It is falsy, so every existing "did it send?" caller keeps
reading "not delivered" unchanged, but a caller that cares can tell a suppressed write
from a failed one with `isinstance(result, SendRefused)` — the two demand opposite
responses, and a bare `False` would conflate them.

Parsing follows the platform's fail-safe rule exactly: an **absent** variable allows
writes (the switch is opt-in), an explicit `0`/`false`/`no`/`off` turns the guard off,
and **any other present value — including a typo — turns it on**.

## Telegram bot setup

1. Open a chat with [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Pick a display name and a username ending in `bot`.
3. Copy the HTTP API token (`123456:ABC-...`).
4. Optionally send `/setprivacy` → **Disable** to let the bot read group messages.
5. Enter the token in the app's Configure form (Settings above), or run
   `personalclaw setup` and paste it when prompted (along with your Telegram user
   id, used as the owner DM target for approvals).

Once configured, the transport long-polls `getUpdates`. Trust is enforced by the
core seam: an unknown DM sender gets a canned pairing-needed reply (run
`personalclaw pair telegram` for a code); a tracked group's non-owner content is
fenced before it enters a session.

## License

MIT — see `LICENSE`.
