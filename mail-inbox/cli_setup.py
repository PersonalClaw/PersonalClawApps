"""The mail-inbox app's `personalclaw setup` step (manifest `cli.setup`).

Registered via ``app.json`` → ``cli.setup: "cli_setup:run"``. The core setup runner
imports ``run`` and calls it with a :class:`personalclaw.sdk.cli.SetupContext`. This
writes ONLY app-owned homes:

- the IMAP **password** → the shared credential store under this app's own key
  ``MAIL_INBOX_PASSWORD`` (``ctx.save_credential``) — the ONLY place a secret ever lives;
- the non-secret mailbox config + the sender allowlist → this app's ``ProviderSettings``
  (``ctx.settings.update``).

Core config.json holds no mail config. Declining the token step leaves the source
disabled (no password ⇒ the provider never polls).
"""

from personalclaw.sdk.cli import SetupContext

from mail_inbox_runtime.settings import CRED_MAIL_PASSWORD

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
