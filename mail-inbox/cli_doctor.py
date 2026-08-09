"""The mail-inbox app's `personalclaw doctor` probe (manifest `cli.doctor`).

Registered via ``app.json`` → ``cli.doctor: "cli_doctor:probe"``. The core doctor
runner imports ``probe`` and calls it (bounded by a timeout + exception guard),
rendering the returned ``list[DoctorLine]`` as this app's doctor section. It reports
mailbox connection config, password presence (in the credential store), and — most
importantly — the fail-closed allowlist posture, so a deliberately-empty inbox is
diagnosable rather than mysterious.
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
    return lines
