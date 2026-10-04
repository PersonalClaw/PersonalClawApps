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
    A turn's progress message keeps only its task lines when the turn ends, each
    task's line updated in place with how its call ended (✅ done, ❌ failed, 🚫 rejected,
    ⌛ or ⏹️ not run), and is removed when it only ever said "Thinking…": the reply is a
    message of its own. An approval prompt says what will run, as
    PersonalClaw's own approval card does: the tool, its arguments in a code block, why the
    agent is calling it, what the call can touch with its risk and, for a command that reaches a
    host off your allowed hosts, a line saying so (from core's brief, already masked), as there is
    for a call someone else asked for, naming who. One too long for one message is split like
    a reply, its buttons on the last part.
    Its buttons are the answers PersonalClaw's approval card offers for that call, each on a
    row of its own: **Allow once** and **Deny**, and **Allow for this chat** on a prompt in the
    chat that is asking (never for a call that may destroy something, for a command that
    reaches a host off your allowed hosts, which is asked about every time, or for a call
    someone else asked for, which none of your grants answers), which the prompt
    explains first, in the card's words. That one trusts the chat in PersonalClaw, as the card's
    *This chat* does: its header shows it and you turn it off there.
    The answers come from PersonalClaw, so the app declares the core feature that hands them
    over (`requiresCoreFeatures`: `approval-answers`), and a PersonalClaw without it will not
    install or update the app. A PersonalClaw from before that check sends no answers: the
    prompt then has no buttons and says so, telling you to answer it in PersonalClaw and to
    update PersonalClaw, and the gateway log says it once.
    When the approval ends, however it ends (an answer here, an answer in PersonalClaw,
    nobody answering within PersonalClaw's approval wait, or the work that asked for it
    stopping first), the buttons come off and how it ended is added under the last part, or
    takes its place when the two would not fit one message, so the chat keeps what happened.
    A press after that is told how it ended, and one on a prompt from before a restart is told
    the approval is no longer waiting. The wait is PersonalClaw's (*Settings → Agent defaults →
    Approval wait*, up to a week); the prompt keeps no clock of its own.
    Only the owner's press answers an approval: in a tracked group every member sees
    the buttons, and anyone else's press is refused and logged. And only a press on the
    prompt's own buttons does: a button naming the approval on any other message answers
    nothing, and is logged.
  - `format.py` — the MarkdownV2 renderer (the classic Telegram footgun, contained), and
    the splitter that cuts a long reply into messages BEFORE rendering them, so every
    part is MarkdownV2 Telegram accepts and a code block stays code on both sides of a
    cut. A reply is read as CommonMark reads it: code spans and blocks arrive exactly as
    written, wherever they sit, and an underscore inside a word (`list_allowed_directories`)
    is a character, not italics. A progress line's tool title is shown as written. A link is
    a link only when it points at a web or mail address: Telegram reads a link to
    `tg://user?id=…` as a mention that notifies that person, so one is shown as written. A part
    Telegram still refuses goes out as plain text rather than not at all. A rich message the
    agent sends goes out as its text, and the buttons on a message are only ever this app's own:
    Telegram hands this app a press by the callback data its writer chose, and this app answers
    its own buttons (an approval's) by theirs, so a keyboard the agent wrote is never sent.
  - `settings.py` — the app's own DM-activation config + credential key.
- `cli_setup.py` / `cli_doctor.py` — the app's `personalclaw setup` / `doctor` hooks.
- `test_provider.py` + `tests/` — the app's own tests.

It imports core **only** via the PersonalClaw **SDK** (never core internals), so
core can evolve without breaking it:

- `personalclaw.sdk.channel` — transport ABC, `ChannelMessage`, the sender-trust
  seam (`guard_inbound`), `run_chat`, `ProviderSettings`, `atomic_write`.

Core masks every text it hands the delivery handle (keys and exfiltration URLs), so the app
masks nothing itself.
- `personalclaw.sdk.cli` — `SetupContext` / `DoctorLine`.

Who may talk (allowlist, pairing) and which groups are tracked are owned by the
**core sender-trust seam** (`channel_trust`, provider `"telegram"`) — this app keeps
no allowlist of its own. The bot token is a secret: setup and the Configure form save it
as the app's `bot_token` setting, which keeps the value in the credential store under a
key this app owns (the settings file holds only a reference), and uninstalling the app
removes it. With the setting empty, a `TELEGRAM_BOT_TOKEN` in the credential store or
the environment is used instead.

## Automations from inbound traffic

This bundle also registers a **`trigger_source`** provider
(`telegram_runtime.trigger_source:create_provider`), so a message that arrives here can fire a
`kind: event` automation. Event names are namespaced by core from the app name:

- `app:telegram-channel:direct_message` — a message in a private chat.
- `app:telegram-channel:group_message` — a message in a tracked group or supergroup.

Author a trigger with pattern `AppEvent` and an `event_glob` matching one of those (or
`app:telegram-channel:*` for any of them). The message body arrives as the payload,
**fenced at origin** by core; `meta` carries identifiers only (`channel_id`, `sender`,
`chat_type`, `is_dm`).

Three things this deliberately does *not* do:

- **It observes nothing your trust gate refused.** The publish happens after core's guarded
  door returns `allowed`. A denied sender gets no session and arms no automation.
- **The event name never comes from the message.** It is chosen in code from the frozen
  list above by a structural fact, so a sender cannot pick which of your automations runs.
- **Prose never lands in `meta`.** `meta` is matched, not narrated, and core does not fence
  it — so a sender's chosen display name is not there.

## Results from your schedules

A schedule can send its results here too. In the schedule's Advanced → Notify channel, pick
Telegram, then **You, in a direct message** or **A chat or channel** with its id: a number like
`4242`, `-1001234567890` for a group, or a public channel's `@username`. Your DMs need Telegram
to know who you are, its owner. Telegram checks the id when you save and says what's wrong if
it can't send there.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Telegram Channel** — the install runs through the security scanner and lifecycle
exactly like any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

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
6. Pair yourself as the owner, if setup did not: in the same Configure form, **Pair as owner**
   shows an 8-digit code. Send it to your bot in a direct message within ten minutes, and the
   bot answers that you are its owner. From then on, what PersonalClaw sends you on Telegram
   (results, scheduled messages, approval prompts) goes to that chat, with no restart.

The owner id is stored as Telegram's own owner, `PERSONALCLAW_OWNER_ID_TELEGRAM`, so setting up
another channel leaves it alone. An install set up by an earlier release kept the owner under the
shared `PERSONALCLAW_OWNER_ID`; the first time Telegram starts, it copies that owner to its own key.

Once configured, the transport long-polls `getUpdates`. Trust is enforced by the
core seam: an unknown DM sender gets a canned pairing-needed reply (run
`personalclaw pair telegram` for a code); a tracked group's non-owner content is
fenced before it enters a session.

The Telegram row on Settings → Providers reads the long-poll, not just the token. If Telegram
rejects the token (401, for example after `/revoke` in @BotFather) the receiver stops and the
row says so; save the new token in Configure to start it again. A long-poll that fails and is
retried (another poller holding the token, Telegram unreachable) reads as not receiving, with
Telegram's answer, until a poll gets through.

## Network

Reaches `api.telegram.org` only.

## License

MIT — see `LICENSE`.
