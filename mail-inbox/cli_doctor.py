"""The mail-inbox app's `personalclaw doctor` probe (manifest `cli.doctor`).

Registered via ``app.json`` → ``cli.doctor: "cli_doctor:probe"``. The core doctor
runner imports ``probe`` and calls it (bounded by a timeout + exception guard),
rendering the returned ``list[DoctorLine]`` as this app's doctor section. It reports
mailbox connection config, password presence (in the credential store), and — most
importantly — the fail-closed allowlist posture, so a deliberately-empty inbox is
diagnosable rather than mysterious. The same applies per prompt-bound address: a row that
CANNOT fire (no stored prompt, or an empty per-address allowlist) is reported here, because
"configured and silent" is the one state a user cannot tell from a working one.
"""

from personalclaw.sdk.channel import AppConfig
from personalclaw.sdk.cli import DoctorLine

from mail_inbox_runtime.settings import CRED_MAIL_PASSWORD, MailInboxSettings


def probe() -> list[DoctorLine]:
    settings = MailInboxSettings.load()
    if not settings.configured:
        return [
            DoctorLine(
                "status", "info",
                "not configured — run 'personalclaw setup' to connect an IMAP mailbox",
            )
        ]

    lines = [
        DoctorLine("host", "ok", f"{settings.host}:{settings.port} ({settings.username})"),
        DoctorLine("folder", "ok", settings.folder),
    ]

    creds = AppConfig.load().load_credentials()
    if creds.get(CRED_MAIL_PASSWORD):
        lines.append(DoctorLine("password", "ok", "configured (credential store)"))
    else:
        lines.append(
            DoctorLine("password", "fail", "missing — set it via 'personalclaw setup'")
        )

    if settings.allow_senders:
        lines.append(
            DoctorLine(
                "allowlist", "ok",
                f"{len(settings.allow_senders)} pattern(s) — fail-closed, only these are surfaced",
            )
        )
    else:
        lines.append(
            DoctorLine(
                "allowlist", "warn",
                "EMPTY — fail-closed, surfacing ZERO messages. Add allow_senders to enable.",
            )
        )

    lines.extend(_bound_address_lines(settings))
    return lines


def _bound_address_lines(settings: MailInboxSettings) -> list[DoctorLine]:
    """The prompt-bound address posture: how many can fire, and which cannot (and why)."""
    rows = settings.bound_addresses
    if not rows:
        return [DoctorLine("bound addresses", "info", "none — mail is surfaced without a prompt")]
    firing = [r for r in rows if r.bound and r.allow_senders]
    lines = [
        DoctorLine(
            "bound addresses", "ok" if firing else "warn",
            f"{len(firing)} of {len(rows)} can fire a stored prompt",
        )
    ]
    for row in rows:
        if not row.enabled:
            lines.append(DoctorLine(f"  {row.label}", "info", "disabled"))
        elif not row.default_prompt:
            lines.append(DoctorLine(f"  {row.label}", "warn", "no default_prompt — never fires"))
        elif not row.allow_senders:
            lines.append(
                DoctorLine(
                    f"  {row.label}", "warn",
                    "no allow_senders — fail-closed, so this address fires nothing",
                )
            )
    return lines
