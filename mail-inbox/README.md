# Mail Inbox

An **inbox source** that polls an IMAP mailbox and surfaces mail as PersonalClaw inbox
items. Compose it with your ordinary Gmail/Outlook filters to turn any email-emitting
service (alerts, receipts, calendar invites, form submissions) into something the agent
can see — with **zero per-vendor integration**.

This bundle is the **inbound** half (IMAP, checkpointing, allowlist, MIME extraction).
Sending replies over SMTP and prompt-bound receiving addresses ship separately.

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
- **Credentials never touch app config.** The IMAP password lives only in the shared
  credential store under the app's own key (`MAIL_INBOX_PASSWORD`). Host, folder, and
  the allowlist are non-secret settings persisted in the app's own store.

## Setup

Run `personalclaw setup` after installing; the app's setup step prompts for:

- **IMAP host / port / username** — e.g. `imap.gmail.com` / `993` / your full address.
- **Password** — use an **app-specific password** (e.g. a Gmail App Password), never
  your account password. Stored in the credential store.
- **Allowed senders** — comma-separated globs, e.g.
  `alerts@*.example.com, calendar-notification@google.com`.

`personalclaw doctor` reports the connection, whether the password is set, and the
allowlist posture (including a warning when it is empty and therefore surfacing nothing).

### Gmail example

1. Enable IMAP in Gmail settings and create an **App Password**.
2. Set host `imap.gmail.com`, port `993`, username your address, password the app
   password.
3. Add the senders you trust to the allowlist. Mail from anyone else is dropped
   (and audited) before it ever becomes an inbox item.

## Security

A mail-triggered turn is attacker-reachable by anyone who can get mail to an allowlisted
address, so mail bodies are treated as **untrusted data**: extracted text is carried raw
into the inbox and fenced downstream at prompt time. The fail-closed allowlist bounds
*who* can reach the agent; fencing bounds *what their content can do*. A compromised
allowlisted sender bypasses the allowlist by definition — the allowlist is not
sufficient on its own; fencing, budgets, and the storm cap are what bound the damage.

## Development

```
python -m pytest mail-inbox -q
```

Tests run against core installed from the repo with no live mail server: the IMAP client
is injected as an in-memory fake, and the sender-trust / security-log surfaces are the
real core seams writing into an isolated tmp home.
