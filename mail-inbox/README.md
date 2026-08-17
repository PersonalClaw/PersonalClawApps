# Mail Inbox

An **inbox source** that polls an IMAP mailbox and surfaces mail as PersonalClaw inbox
items. Compose it with your ordinary Gmail/Outlook filters to turn any email-emitting
service (alerts, receipts, calendar invites, form submissions) into something the agent
can see — with **zero per-vendor integration**.

It also **replies** over SMTP — but only as a **draft** until you say otherwise. Sending
mail is irreversible and leaves your machine, so it is off by default. See
[Replies](#replies-draft-by-default).

## What it does

- **Polls IMAP** (`IMAP4_SSL` by default) for messages newer than a persisted UID
  cursor, so a restart never reprocesses or skips mail. A duplicate `Message-ID` is
  dropped as a second belt.
- **Fail-closed sender allowlist.** Only senders matching your allow-glob patterns are
  ever surfaced. An **empty allowlist surfaces nothing at all** — never "everything".
  This is the security posture, not a bug: an unknown sender can never trigger anything.
  The posture is logged at startup so a deliberately-empty inbox is diagnosable, and
  every rejection records a `mail_sender_rejected` security event.
- **Extracts readable text.** Prefers `text/plain`; sanitizes HTML-only mail to visible
  text (dropping `<script>`/`<style>`); and pulls text from PDF/DOCX/PPTX attachments
  through the platform's own document readers.
- **Prompt-bound addresses.** A purpose-specific receiving address can carry a stored
  prompt ("build my itinerary and add calendar entries"). Mail to it becomes that prompt
  followed by the mail wrapped in an `<untrusted_content source="mail:<address>">` fence,
  so the instruction is yours and the mail stays data. Each bound address has its own
  sender allowlist that **narrows** the app-wide one.
- **Replies, drafted by default.** A reply is composed as a properly threaded message
  (`In-Reply-To` + `References`) and written to disk as a real `.eml`. Nothing is sent
  until you turn sending on — see [Replies](#replies-draft-by-default).
- **Credentials never touch app config.** The IMAP password lives only in the shared
  credential store under the app's own key (`MAIL_INBOX_PASSWORD`), and the SMTP password
  under its own separate key (`MAIL_INBOX_SMTP_PASSWORD`). Host, folder, and
  the allowlist are non-secret settings persisted in the app's own store.

## Setup

Run `personalclaw setup` after installing; the app's setup step prompts for:

- **IMAP host / port / username** — e.g. `imap.gmail.com` / `993` / your full address.
- **Password** — use an **app-specific password** (e.g. a Gmail App Password), never
  your account password. Stored in the credential store.
- **Allowed senders** — comma-separated globs, e.g.
  `alerts@*.example.com, calendar-notification@google.com`.

- **SMTP for replies** (optional) — host / port / TLS mode / username, plus an SMTP
  password stored under its own credential key. Configuring it does **not** start sending;
  see below.

`personalclaw doctor` reports the connection, whether the password is set, the allowlist
posture (including a warning when it is empty and therefore surfacing nothing), and the
reply posture — `DRAFT only` with the exact reason, or a **warning** when replies are
going out live.

### Gmail example

1. Enable IMAP in Gmail settings and create an **App Password**.
2. Set host `imap.gmail.com`, port `993`, username your address, password the app
   password.
3. Add the senders you trust to the allowlist. Mail from anyone else is dropped
   (and audited) before it ever becomes an inbox item.

## Prompt-bound addresses

A **bound address** is a purpose-specific receiving address carrying a stored prompt.
Manage the table from **Apps → Mail Inbox → Configure** (the settings page the platform
generates from this app's manifest schema); the *Prompt-Bound Addresses* field takes one
JSON object per address:

```json
[
  {
    "name": "Business Travel",
    "address": "you+travel@gmail.com",
    "default_prompt": "Build my itinerary from this booking, add calendar entries, and buffer 90 minutes for travel to the airport.",
    "enabled": true,
    "allow_senders": ["*@booking.example.com", "noreply@airline.example"]
  }
]
```

- `address` — matched **exactly** (case-insensitively) against the mail's `Delivered-To`,
  `X-Original-To`, `To` and `Cc` headers. So a Gmail `+suffix`, a catch-all domain, and a
  per-purpose mailbox all work; `nottravel@…` never binds to `travel@…`.
- `default_prompt` — **your** instruction. It is the only trusted text in the composed
  prompt. A row with no prompt (or `enabled: false`) does not bind at all: the mail is
  surfaced as ordinary mail rather than half-firing.
- `allow_senders` — a per-address allowlist that **narrows** the app-wide one. It is
  checked after it, so it can only ever remove senders. An **empty list fires nothing**,
  and an unlisted sender records a `mail_address_sender_rejected` security event and is
  dropped entirely (no inbox item, no event, nothing fired).

Mail that binds becomes:

```
<your default_prompt>

<untrusted_content source="mail:you+travel@gmail.com">
Subject: Your flight is confirmed
…the mail body and any attachment text…
</untrusted_content>
```

The subject is inside the fence too — it is as sender-controlled as the body. Mail that
tries to *close* the fence and append instructions has its markers escaped, so the
injected text stays inside the fence as data.

### Worked example: Gmail filter → bound address → agent run

1. **Gmail** → Settings → *Filters and Blocked Addresses* → *Create a new filter*:
   `from:(booking.example.com) has:the words "confirmed"` → **Forward it to**
   `you+travel@gmail.com` (or apply a label and let the `+suffix` ride on the original
   `To:`). Gmail delivers `+suffix` mail to your normal inbox, so no new mailbox is needed.
2. **This app** → add the bound row above; keep `allow_senders` tight (only the services
   whose mail should be able to start a run).
3. **Triggers page** → *New trigger* → kind **Data event** → pattern **InboxAddress** →
   address glob `you+travel@gmail.com` → action **invoke-agent** with the task template
   `$value`. `$value` is the composed prompt-plus-fenced-mail above, so the run executes
   your stored prompt grounded in the mail. (The platform fences an event payload only
   when it is not already fenced, so this app's `mail:<address>` attribution reaches the
   action unchanged rather than being re-wrapped.)
4. Send yourself a booking confirmation from an allowlisted sender. The agent runs the
   stored prompt against the fenced mail; a mail to the same address from any other
   sender does nothing at all.

Nothing in this table is a secret, so nothing here is masked: this app's credentials are
the IMAP and SMTP passwords, and both live in the credential store, never in app config.

## Replies (draft-by-default)

A sent email cannot be recalled, and it is visible to someone who is not you. So this app
**composes replies and does not send them**. That is the default, not a setup step you
forgot.

What happens on a reply:

1. The reply is **composed** — `To:` the original sender, `Subject: Re: …` (no stacked
   `Re: Re:`), `In-Reply-To:` the original `Message-ID`, and a `References:` chain that
   extends the original's, which is what makes a real mail client thread it under the
   original conversation. The `From:` is the address the mail arrived at, so a reply to a
   prompt-bound address keeps that address's identity instead of exposing the mailbox
   login behind it.
2. It is **written to disk** at `~/.personalclaw/apps/mail-inbox/data/drafts/*.eml` — a
   real RFC822 file you can open in any mail client, edit, and send yourself.
3. It is **sent** only if *every* one of these holds: `send_enabled` is on, the SMTP
   settings are complete, an SMTP password is in the credential store, the caller did not
   ask for a dry run, and the platform's `PERSONALCLAW_DISABLE_LIVE_WRITES` guard is not
   set. Otherwise it stays a draft and the reason is recorded — in the log, in the
   security event log, and in `personalclaw doctor`.

In every draft case **no SMTP connection is opened and no sender object is even
constructed**, so a dry run cannot put a byte on the wire.

To turn sending on: **Apps → Mail Inbox → Configure → Send Replies**. Do it deliberately.

Every outcome is audited as a security event — `mail_reply_drafted`, `mail_reply_sent`,
`mail_reply_send_failed`, `mail_reply_refused` — recording the recipient and the
`In-Reply-To`, never the reply text and never a credential.

**Replies are fail-closed on the recipient.** `send_reply(channel_id, text, thread_ts)`
carries no address, so the app records how to answer each message while polling it. If a
reply names a channel or thread it has no record of, it is **refused** — nothing is
composed and nothing is sent. Answering the wrong person is worse than not answering.

TLS is verified, not attempted: in `starttls` mode a failed upgrade **aborts** the send
rather than continuing in the clear, so an app password is never put on a plaintext
socket. SMTP error text is scrubbed of the password before it reaches a log.

## Security

A mail-triggered turn is attacker-reachable by anyone who can get mail to an allowlisted
address, so mail bodies are treated as **untrusted data**: extracted text is carried raw
into the inbox and fenced at prompt-composition time — once, in one place
(`addresses.compose_prompt`), never in the MIME extractor, so text is never double-fenced.
The fail-closed allowlist bounds *who* can reach the agent (twice over for a bound
address); fencing bounds *what their content can do*. A compromised
allowlisted sender bypasses the allowlist by definition — the allowlist is not
sufficient on its own; fencing, budgets, and the storm cap are what bound the damage.

## Development

```
python -m pytest mail-inbox -q
```

Tests run against core installed from the repo with no live mail server: the IMAP client
and the SMTP sender are both injected as in-memory fakes, and the sender-trust /
security-log surfaces are the real core seams writing into an isolated tmp home. **No test
sends real mail** — the outbound tests assert on the captured `EmailMessage` and on the
`.eml` written to a tmp home, and `sender.sent == []` is how "nothing left the machine" is
proved.
