"""The mail-inbox app's `personalclaw setup` step (manifest `cli.setup`).

Registered via ``app.json`` → ``cli.setup: "cli_setup:run"``. The core setup runner
imports ``run`` and calls it with a :class:`personalclaw.sdk.cli.SetupContext`. This
writes ONLY app-owned homes:

- the IMAP **password** → the shared credential store under this app's own key
  ``MAIL_INBOX_PASSWORD`` (``ctx.save_credential``) — the ONLY place a secret ever lives;
- the SMTP **password** → the same store under ``MAIL_INBOX_SMTP_PASSWORD``;
- the non-secret mailbox/SMTP config + the sender allowlist → this app's
  ``ProviderSettings`` (``ctx.settings.update``).

Core config.json holds no mail config. Declining the token step leaves the source
disabled (no password ⇒ the provider never polls).

The outbound step never turns sending ON: it configures the transport and stores the
password, and ``send_enabled`` stays False (guardrail 4 — draft-by-default). Enabling a
send is a separate, deliberate act in the app's settings page, because a sent email cannot
be taken back.
"""

from personalclaw.sdk.cli import SetupContext

from mail_inbox_runtime.settings import CRED_MAIL_PASSWORD, CRED_SMTP_PASSWORD

_APP = "mail-inbox"


def run(ctx: SetupContext) -> None:
    """Prompt for the mailbox connection, password (→ credential store), and the
    fail-closed sender allowlist (→ ProviderSettings). Empty input keeps the current
    value; declining skips the whole step (the source stays disabled)."""
    ctx.print("── Mail Inbox App ──\n")
    ctx.print(
        "  Connect an IMAP mailbox as an inbox source. Use an app-specific password\n"
        "  (e.g. a Gmail App Password), never your account password.\n"
        "  Mail is surfaced ONLY from senders you allow — an empty allowlist surfaces\n"
        "  NOTHING (fail-closed).\n"
    )
    answer = ctx.input("  Configure the mail inbox now? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        ctx.print("  ⏭  Skipped. The mail inbox source will be disabled.\n")
        return

    _setup_connection(ctx)
    _setup_password(ctx)
    _setup_allowlist(ctx)
    _setup_outbound(ctx)


def _setup_connection(ctx: SetupContext) -> None:
    cur = ctx.settings.load(_APP)
    host = ctx.input(f"  IMAP host [{cur.get('host', '')}]: ").strip() or str(cur.get("host", ""))
    port_raw = ctx.input(f"  IMAP port [{cur.get('port', 993)}]: ").strip()
    username = (
        ctx.input(f"  Username [{cur.get('username', '')}]: ").strip()
        or str(cur.get("username", ""))
    )
    address = (
        ctx.input(f"  Receiving address [{cur.get('address', '') or username}]: ").strip()
        or str(cur.get("address", ""))
        or username
    )
    folder = (
        ctx.input(f"  Folder [{cur.get('folder', 'INBOX')}]: ").strip()
        or str(cur.get("folder", "INBOX"))
    )
    update: dict = {"host": host, "username": username, "address": address, "folder": folder}
    if port_raw:
        try:
            update["port"] = int(port_raw)
        except ValueError:
            ctx.print(f"  ⚠️  Invalid port — keeping {cur.get('port', 993)}.")
    ctx.settings.update(_APP, update)


def _setup_password(ctx: SetupContext) -> None:
    cur = ctx.get_credential(CRED_MAIL_PASSWORD)
    hint = " [set]" if cur else ""
    password = ctx.input(f"  IMAP password / app password{hint}: ").strip()
    if password:
        ctx.save_credential(CRED_MAIL_PASSWORD, password)
        ctx.print("  ✅ Password saved to the credential store.\n")
    elif not cur:
        ctx.print("  ⚠️  No password — the mail inbox will stay disabled until one is set.\n")


def _setup_allowlist(ctx: SetupContext) -> None:
    cur = ctx.settings.load(_APP).get("allow_senders", []) or []
    ctx.print("── Allowed Senders (fail-closed) ──\n")
    ctx.print(
        "  Comma-separated glob patterns of From addresses permitted to trigger\n"
        "  (e.g. alerts@*.example.com, boss@example.com). EMPTY = nothing is surfaced.\n"
    )
    raw = ctx.input(f"  Allowed senders [{', '.join(cur)}]: ").strip()
    if not raw:
        return
    patterns = [p.strip().lower() for p in raw.split(",") if p.strip()]
    ctx.settings.update(_APP, {"allow_senders": patterns})
    ctx.print(f"  ✅ Allowlist: {len(patterns)} pattern(s).\n")


def _setup_outbound(ctx: SetupContext) -> None:
    """Configure SMTP for replies. Never enables sending — see the module docstring."""
    cur = ctx.settings.load(_APP)
    ctx.print("── Replies (draft-by-default) ──\n")
    ctx.print(
        "  Replies are always composed as threaded drafts first. Configuring SMTP here does\n"
        "  NOT start sending mail — flip 'Send Replies' in the app's settings when you want\n"
        "  that. Skip this and replies still work; they just stay drafts.\n"
    )
    answer = ctx.input("  Configure SMTP for replies now? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        ctx.print("  ⏭  Skipped. Replies will be drafted only.\n")
        return

    host = (
        ctx.input(f"  SMTP host [{cur.get('smtp_host', '')}]: ").strip()
        or str(cur.get("smtp_host", ""))
    )
    port_raw = ctx.input(f"  SMTP port [{cur.get('smtp_port', 587)}]: ").strip()
    security = (
        ctx.input(f"  TLS mode (starttls/ssl/plain) [{cur.get('smtp_security', 'starttls')}]: ")
        .strip()
        .lower()
        or str(cur.get("smtp_security", "starttls"))
    )
    username = (
        ctx.input(f"  SMTP username [{cur.get('smtp_username', '') or 'same as IMAP'}]: ").strip()
        or str(cur.get("smtp_username", ""))
    )
    update: dict = {"smtp_host": host, "smtp_security": security, "smtp_username": username}
    if port_raw:
        try:
            update["smtp_port"] = int(port_raw)
        except ValueError:
            ctx.print(f"  ⚠️  Invalid port — keeping {cur.get('smtp_port', 587)}.")
    ctx.settings.update(_APP, update)
    _setup_smtp_password(ctx)
    ctx.print(
        "  ℹ️  Sending stays OFF (send_enabled=false). Replies are drafted under the app's\n"
        "     data dir (drafts/*.eml) until you enable sending in the app's settings.\n"
    )


def _setup_smtp_password(ctx: SetupContext) -> None:
    """Store the SMTP secret under its OWN key.

    An empty answer may COPY the IMAP password — most providers issue one app password per
    account, so this is the common case — but it is copied explicitly, on the user's say-so,
    rather than fallen back to silently at run time."""
    cur = ctx.get_credential(CRED_SMTP_PASSWORD)
    hint = " [set]" if cur else ""
    password = ctx.input(f"  SMTP password / app password{hint}: ").strip()
    if password:
        ctx.save_credential(CRED_SMTP_PASSWORD, password)
        ctx.print("  ✅ SMTP password saved to the credential store.\n")
        return
    if cur:
        return
    imap_password = ctx.get_credential(CRED_MAIL_PASSWORD)
    if imap_password:
        answer = ctx.input("  Reuse the IMAP password for SMTP? [y/N]: ").strip().lower()
        if answer in ("y", "yes"):
            ctx.save_credential(CRED_SMTP_PASSWORD, imap_password)
            ctx.print("  ✅ Copied the IMAP password to the SMTP credential key.\n")
            return
    ctx.print("  ⚠️  No SMTP password — replies will stay drafts (fail-closed).\n")
