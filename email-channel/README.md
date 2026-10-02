# Email Channel

Two-way email channel over stdlib IMAP/SMTP. Mail the bound mailbox and the agent
replies in the same thread; cron results, notifications and approvals arrive there too.

**Email Channel** is a **channel-transport provider** — it implements the
`personalclaw.sdk.channel` `ChannelTransportProvider` + `ChannelDelivery` contracts and
shows up under the messaging channels alongside the dashboard, Slack, Telegram and
Discord.

## Not the same as Mail Inbox

The sibling **`mail-inbox`** app is an *inbox source*: it surfaces mail as read-only
inbox items behind its own sender allowlist. This app is a *channel*: you converse with
the agent by email, and it replies into the thread. Two different seams, two different
trust owners:

| | `mail-inbox` | `email-channel` (this app) |
|---|---|---|
| Seam | `MessageSourceProvider` (inbox) | `ChannelTransportProvider` + `ChannelDelivery` |
| Direction | inbound only | two-way |
| Who may talk | app-local `allow_senders` globs | the **core trust seam** (`channel_trust`, provider `email`) |
| Result | an inbox item | a conversational turn, answered in-thread |

Install both if you want mail to *trigger* things (mail-inbox) **and** to *talk* to your
agent (this app). They poll independently and hold separate cursors.

## What this is

A standalone PersonalClaw app bundle. It ships as a self-contained directory:

- `app.json` — the manifest (provider type `channel`; `implementation` points at
  `email_runtime.transport:create_provider`).
- `email_runtime/` — the implementation:
  - `imap_client.py` — the blocking IMAP mechanics behind a narrow protocol: UID-only
    commands, read-only `SELECT`, `BODY.PEEK[]`, `UIDVALIDITY`, the folder's newest UID,
    and the `_MAXLINE` ceiling raised at import.
  - `smtp_client.py` — the blocking SMTP mechanics: STARTTLS or SSL, **never plaintext** (a
    server that cannot upgrade, or a failed upgrade, aborts before the login and the mail).
  - `tls.py` — the one TLS context every IMAP and SMTP connection uses, and the sentence
    a connection that fails reports.
  - `mime.py` — inbound parse (RFC-2047 header decoding, `text/plain` preference, HTML
    stripped to text, quoted-history trimming, `parseaddr`-only sender addresses, and the
    RFC 3834 check for mail a program sent) and outbound build (`Message-ID` /
    `In-Reply-To` / `References`).
  - `transport.py` — the IMAP poll loop, the self-message filter, trust-seam
    integration, code-in-reply pairing, and session routing.
  - `delivery.py` — the `ChannelDelivery` the gateway delivers results through, plus the
    persisted thread state that keeps replies in one conversation.
  - `settings.py` — the app's own non-secret config + its credential keys.
- `cli_setup.py` / `cli_doctor.py` — the app's `personalclaw setup` / `doctor` hooks.
- `test_provider.py` + `tests/` — the app's own tests (no network, no sleeps, no writes
  outside a tmp home).

It imports core **only** via the PersonalClaw **SDK** (never core internals), so core can
evolve without breaking it:

- `personalclaw.sdk.channel` — the transport ABC, `ChannelMessage`, the sender-trust seam
  (`guard_inbound`, `redeem_pairing_code`, `is_tracked_channel`), `run_chat`,
  `ProviderSettings`, `AppConfig`, `atomic_write`.
- `personalclaw.sdk.util` — `app_data_dir` (the UID cursor, thread state and the approvals it
  mailed).
- `personalclaw.sdk.cli` — `SetupContext` / `DoctorLine`.

Core masks every text it hands the delivery handle (keys and exfiltration URLs), the words of a
subject included, so the app masks nothing itself.

**No vendor SDK and no new dependencies:** `imaplib`, `smtplib` and `email` are stdlib.
The manifest declares no `pythonDependencies`.

## Automations from inbound traffic

This bundle also registers a **`trigger_source`** provider
(`email_runtime.trigger_source:create_provider`), so a message that arrives here can fire a
`kind: event` automation. Event names are namespaced by core from the app name:

- `app:email-channel:mail_received` — mail arrived from an allowed correspondent.
  One event, not two: every mail is a direct message (there is no room concept), so
  there is no second structural fact to name a second event with.

Author a trigger with pattern `AppEvent` and an `event_glob` matching one of those (or
`app:email-channel:*` for any of them). The quote-stripped new prose of the mail arrives as
the payload, **fenced at origin** by core, followed by an `Attached:` list naming each file
the mail came with (name, type and size); `meta` carries identifiers only
(`channel_id`, `sender`, `is_dm`).

Three things this deliberately does *not* do:

- **It observes nothing your trust gate refused.** The publish happens after core's guarded
  door returns `allowed` — and a `From` address is trivially forged, which is exactly why
  that decision is core's. A denied sender gets no session and arms no automation.
- **The event name never comes from the message.** It is chosen in code from the frozen
  list above by a structural fact, so a sender cannot pick which of your automations runs.
- **Prose never lands in `meta`.** `meta` is matched, not narrated, and core does not fence
  it — so neither the `From` display name nor the **`Subject`** is there. That does mean a
  subject-matching trigger is not expressible today; the limit is recorded rather than
  traded for the fence.

## Install

From the App Store, add the apps directory as a **local source**, then install **Email
Channel** — the install runs through the security scanner and lifecycle exactly like any
other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Mailbox setup

1. **Dedicate a mailbox.** Use a fresh address, not your personal inbox: every message
   from a paired sender becomes a conversational turn.
2. **Create an app password** — never your account password:
   - **Gmail** — Google Account → Security → 2-Step Verification → App passwords → Mail.
   - **Fastmail** — Settings → Privacy & Security → App Passwords → New, scoped to
     *Mail (IMAP/SMTP)*.
   - **iCloud** — appleid.apple.com → Sign-In and Security → App-Specific Passwords.
3. Run `personalclaw setup` and pick your provider (hosts and ports are prefilled), or
   fill the Configure form and add the passwords with `personalclaw setup`.
4. Run `personalclaw doctor` — it performs the live **login + SELECT** probe on IMAP and
   a login probe on SMTP, so a wrong folder or SMTP port fails loudly rather than
   silently.

## Settings

| Key | Label | Notes |
|---|---|---|
| `imap_host` / `imap_port` / `imap_user` / `imap_use_ssl` | IMAP | Inbound. 993 + SSL by default. With IMAP SSL off (usually 143), the connection is upgraded with STARTTLS before the login. |
| `imap_password` | IMAP App Password | Write-only. An app password, never your account password. |
| `folder` | Folder | Polled **read-only** — your mail is never marked read. |
| `smtp_host` / `smtp_port` / `smtp_user` / `smtp_security` | SMTP | Outbound. 587 + STARTTLS by default, or `ssl` (usually 465). There is no mode without TLS: `plain` is gone, and a setting that still names it reads as `starttls`. |
| `smtp_password` | SMTP App Password | Write-only. Blank reuses the IMAP one (one app password usually covers both). |
| `tls_ca_file` | CA Certificate File | Optional. The PEM certificate of the authority your mail server's certificate comes from, when it is not a public one (a company relay, a home server). Trusted in addition to this machine's authorities. |
| `address` | Mailbox Address | Sends as, receives at, and anchors the self-message filter. Defaults to the IMAP login. |
| `poll_secs` | Poll Interval | 60s default, clamped to 10–3600. |
| `dm_activation` | Inbound Activation | `always`, or `off` to keep outbound delivery only. |

The two passwords never sit in the settings file: each is kept in the credential store
under a key this app owns, the file holds only a reference, and uninstalling the app
removes them. Passwords an earlier release's setup saved under `EMAIL_IMAP_PASS` /
`EMAIL_SMTP_PASS` are still used while the settings are empty.

## Connecting

- **The server is verified before it gets the password.** IMAP, implicit-TLS SMTP and
  STARTTLS all check the server's certificate and host name against this machine's
  certificate authorities, plus the one **CA Certificate File** names. Nothing turns the
  check off: a server nothing vouches for is refused, and the channel's status says
  which server, why, and that the password was not sent. With **IMAP SSL** off, the IMAP
  connection is upgraded with STARTTLS before the login, and a server that does not offer
  STARTTLS is refused the same way: the password is never sent in the clear. SMTP is the
  same: `starttls` upgrades before the login and the mail, and a relay that does not offer
  STARTTLS is refused with *the SMTP server … doesn't offer STARTTLS, so nothing was sent to
  it*.
- **A first connection starts after the newest message.** The mail already in the folder
  when you set the channel up is never read as the channel's; mail that arrives after it is.
  The same happens when the server renumbers the folder (`UIDVALIDITY` changes).
- **Mail a program sent is never answered** (RFC 3834): `Auto-Submitted` other than `no`,
  an empty `Return-Path`, `Precedence: bulk` / `list` / `junk`, mailing-list headers, and
  no-reply or daemon senders. Such mail gets no reply, no turn, no notification and no Inbox
  row. An allowed correspondent's automated mail still reaches your automations.
- **The status is what the connections last did.** It reads ready while the receiver runs,
  its last poll read the folder and the last send went out. Otherwise it names what failed
  (a refused login, an untrusted certificate, an unreachable server), and Test agrees
  with it. A poll that fails waits longer before each retry, up to 15 minutes.

## The live-writes kill switch

Handing a message to an SMTP relay is the least reversible write this app makes —
once the server accepts it there is no recall, no edit and no delete — so this
transport honors the platform's process-wide `PERSONALCLAW_DISABLE_LIVE_WRITES`
switch, the same one core applies to non-GET egress and local-model deletion.
(This is the platform guard, checked after this app's own SMTP configuration gate: an
unconfigured mailbox reports a plain `False` because it could not have written anything,
and only a transport that WOULD have transmitted reports a refusal.)

With the switch set, `send()` transmits nothing and returns a **typed refusal**
(`SendRefused`) instead. It is falsy, so every existing "did it send?" caller keeps
reading "not delivered" unchanged, but a caller that cares can tell a suppressed write
from a failed one with `isinstance(result, SendRefused)` — the two demand opposite
responses, and a bare `False` would conflate them.

Parsing follows the platform's fail-safe rule exactly: an **absent** variable allows
writes (the switch is opt-in), an explicit `0`/`false`/`no`/`off` turns the guard off,
and **any other present value — including a typo — turns it on**.

## Trust and pairing

Who may talk is owned by the **core sender-trust seam** (`channel_trust`, provider
`email`) — this app keeps no allowlist of its own.

**A stranger is sent nothing.** This mailbox is yours, and what the app sends goes out from
your address, so it never answers someone PersonalClaw does not know: a reply would tell them
the address is read, and it would be mail you never agreed to send. Their mail waits in
PersonalClaw's **Inbox** as someone new, and you get one notification, at most once a day per
address, however often it writes. From the Inbox you choose:

- **Reply**: what you write goes to them, in their thread, when you press Send.
- **Pair**: they can talk to your agent from their next mail on (this one is not handed to it).
- **Ignore**: the row is dismissed, and they hear nothing.

A thread you mute in the Inbox holds nothing more, and nothing in it is announced: you are told
only of mail that is in your Inbox.

Or pair them with a code, while Settings → Sender trust → Email lets a code in (**A code lets
them in**, the default; under **Only you let them in** a code lets nobody in):

1. Press **Pair someone** on Settings → Sender trust → Email, or run `personalclaw pair email`,
   for an 8-digit code (TTL 10 min, single use), and give it to them yourself (a reply from the
   Inbox works).
2. They **reply with the code anywhere in the body** — quoting and signatures are fine. They
   are told they are paired: the one mail this app sends on its own to someone it did not
   know.
3. From then on they converse; each thread gets its own session.

**The owner** is the address approvals and anything else for you are sent to. Pair it from
Configure → **Pair as owner**: mail the code the page shows to the mailbox from that address,
anywhere in the message. It then counts as a paired sender too. Five wrong codes cancel it, and
the mailbox's own address can never be the owner (its mail is dropped unread).

Trust is keyed on the address parsed out of `From`, never on the display name. A message
whose display name reads `allowed@example.com` but whose actual address is
`evil@attacker.test` is denied.

## Attachments

A mail's attachments reach PersonalClaw as the files they are, never as body text. From
someone PersonalClaw does not know yet, the Inbox row that holds the mail lists each file by
name, type and size, with a download. From an allowed correspondent, the files ride with the
mail's turn as that turn's attached files, the way a file you attach in the chat does. A mail
that is only its attachments is still a turn: it names what came.

## Capabilities

| Capability | Value | Why |
|---|---|---|
| `inbound` | ✅ | the IMAP poll loop |
| `threads` | ✅ | `Message-ID` / `In-Reply-To` / `References` chains |
| `attachments` | ✅ | `upload_attachment` adds a MIME part |
| `rich_text` | ✅ | `deliver_rich` sends an HTML alternative |
| `reactions` | ❌ | email has no reaction concept |
| `typing_indicator` | ❌ | nothing to show between messages |
| `edits` | ❌ | **this is how `streaming=false` is declared** — see below |
| `speaks_as_owner` | ✅ | a mail goes out from your own mailbox, so a stranger is sent nothing and their mail waits in your Inbox |
| `groups` | ❌ | a mail to the mailbox is one person writing to you, so Sender trust shows no group rule |

**Streaming is deliberately absent** (the plan's C3 table marks the streaming trio
MUST-NOT for email: a "live-updating message" would mean one mail per token).
`ChannelCapabilities` has no `streaming` field, and in every other channel a stream *is* a
repeatedly-edited message — so `edits=False` carries that meaning, and `start_stream()`
returns `""` with no-op append/stop. Core's mirror path already treats `""` as "this
channel cannot stream". Both halves are asserted together in
`tests/test_transport.py::TestCapabilities`.

Approvals arrive as a **reply token**. The prompt mail names where the call came from
(*from loop “Fix the README”*) and says what will run, as PersonalClaw's
own approval card does: the tool, its arguments, why the agent is calling it, what the call can
touch with its risk and, for a command that reaches a host off your allowed hosts, a line saying
so (masked by PersonalClaw). It lists the answers PersonalClaw's approval card offers for that
call, each with the word to reply with: `APPROVE <token>` (Allow once) and
`DENY <token>`, and `TRUST <token>` (Allow for this chat, explained in the card's words) on a mail
in the chat that is asking, never for a call that may destroy something or for a command that
reaches a host off your allowed hosts, which is asked about every time. That one trusts the chat
in PersonalClaw, as the card's *This chat* does: its header shows it and you turn it off there.
**Only the owner is asked, and only the owner answers.** The owner is the
address paired from Configure → **Pair as owner** (above), which core keeps as this channel's
owner id. The prompt goes to that address alone, and only a reply from it can resolve one. It
cannot be the mailbox's own address.

A chat in a thread with the owner is asked in that thread. A chat with anyone else, a paired
correspondent included, is asked in a new mail to the owner. A correspondent's reply carrying a
token decides nothing and is logged to the security event log. With no owner address, no
approval is asked by mail, and it waits in PersonalClaw instead.

Both a word the mail listed and the token must be present. A body naming more than one answer
gives the narrowest: an explicit `DENY` wins, and `APPROVE` wins over `TRUST`. A chat that started in this mailbox is asked here, whatever the notification rules say.
Where the rest are asked is up to *Settings → Notifications*: *Send approvals to*, and the
Approval needed row's Channel DM target.

The mail says how long PersonalClaw waits for the answer (*Settings → Agent defaults → Approval
wait*, up to a week). If nobody answers by then, the call does not run, and that is not a Deny.
This app keeps no clock of its own: the approval ends when PersonalClaw ends it, however it
ends (a reply here, an answer in PersonalClaw, the wait running out, or the work that asked for
it stopping first). A reply after that decides nothing and does not reach the agent as a
message: it is answered in its thread with how the approval ended. That holds after a restart
too: the app keeps each approval it mailed, and how it ended, in its own data from the moment the
mail goes out (the newest 256). A reply to one a stopped gateway never saw end is told it is no
longer waiting.

Notifications can reach the mailbox too. A notification rule with the **Channel DM** target
sends its note to the owner on the first connected channel that reaches them, in name order,
so here when Email is that channel (`deliver_text`); a schedule's or heartbeat's result for
the owner is sent the same way, through `deliver_notification`.

## Deferred, on purpose

- **IMAP IDLE.** `imaplib` has no IDLE support, so it would mean hand-rolling the command
  plus its 29-minute re-issue cycle and dead-connection detection. The plan calls IDLE
  "optional later"; the 60s poll cadence is configurable in the meantime.
- **OAuth2 / XOAUTH2** (DISCOVERY). Every provider documented above issues per-application
  passwords precisely for clients like this, and they need no token refresh, no client
  registration, and no browser round-trip in a headless gateway. OAuth2 would add a
  per-provider registration story and a refresh-token lifecycle before it improved
  anything; when a provider we care about drops app passwords, it becomes worth building.

## License

MIT — see `LICENSE`.
