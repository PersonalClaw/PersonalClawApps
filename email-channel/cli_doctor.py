"""The email-channel app's `personalclaw doctor` probe (manifest `cli.doctor`).

Registered via ``app.json`` → ``cli.doctor: "cli_doctor:probe"``. The core doctor runner
(``personalclaw.app_cli.run_app_doctor_probes``) imports ``probe`` and calls it (bounded
by a timeout + exception guard), rendering the returned ``list[DoctorLine]`` as this
app's doctor section.

The probe here is the plan's ``probe = login+select``, and it runs it **live** for both
protocols: IMAP login plus a SELECT of the polled folder, and an SMTP login. That is
more than the Telegram/Discord doctors do (they only check that a token is present and
point at the Channels-page Test), and deliberately so — an email channel has *two*
independent connections, and the failures that matter here (a wrong folder name, an
SMTP port/security mismatch) are invisible to a credential-presence check. Both probes
are blocking socket calls; the doctor runner already bounds them with a timeout.
"""

from personalclaw.sdk.channel import AppConfig
from personalclaw.sdk.cli import DoctorLine

from email_runtime.imap_client import probe_login as imap_probe
from email_runtime.settings import CRED_IMAP_PASS, CRED_SMTP_PASS, EmailSettings
from email_runtime.smtp_client import probe_login as smtp_probe


def probe() -> list[DoctorLine]:
    settings = EmailSettings.load()
    if not settings.inbound_configured and not settings.outbound_configured:
        return [
            DoctorLine(
                "status", "info",
                "not configured — run 'personalclaw setup' to connect a mailbox",
            )
        ]

    creds = AppConfig.load().load_credentials()
    imap_pass = creds.get(CRED_IMAP_PASS, "")
    smtp_pass = creds.get(CRED_SMTP_PASS, "") or imap_pass

    lines: list[DoctorLine] = [
        DoctorLine("mailbox", "ok", settings.mailbox_address or "(unset)"),
    ]
    lines.extend(_imap_lines(settings, imap_pass))
    lines.extend(_smtp_lines(settings, smtp_pass))
    lines.append(
        DoctorLine(
            "trust", "info",
            "unknown senders are refused until paired — 'personalclaw pair email'",
        )
    )
    return lines


def _imap_lines(settings: EmailSettings, password: str) -> list[DoctorLine]:
    if not settings.inbound_configured:
        return [DoctorLine("imap", "warn", "not configured — inbound is offline")]
    where = f"{settings.imap_host}:{settings.imap_port} ({settings.imap_user})"
    if not password:
        return [
            DoctorLine("imap", "ok", where),
            DoctorLine(
                "imap password", "fail",
                f"missing from the credential store ({CRED_IMAP_PASS}) — run "
                "'personalclaw setup'",
            ),
        ]
    ok, detail = imap_probe(
        settings.imap_host, settings.imap_port, settings.imap_user, password,
        settings.folder, use_ssl=settings.imap_use_ssl,
    )
    return [
        DoctorLine("imap", "ok", where),
        DoctorLine("imap login+select", "ok" if ok else "fail", detail),
    ]


def _smtp_lines(settings: EmailSettings, password: str) -> list[DoctorLine]:
    if not settings.outbound_configured:
        return [DoctorLine("smtp", "warn", "not configured — the agent cannot reply")]
    where = f"{settings.smtp_host}:{settings.smtp_port} ({settings.smtp_security})"
    if not password:
        return [
            DoctorLine("smtp", "ok", where),
            DoctorLine(
                "smtp password", "fail",
                f"missing from the credential store ({CRED_SMTP_PASS} or {CRED_IMAP_PASS})",
            ),
        ]
    ok, detail = smtp_probe(
        settings.smtp_host, settings.smtp_port, settings.smtp_user, password,
        security=settings.smtp_security,
    )
    return [
        DoctorLine("smtp", "ok", where),
        DoctorLine("smtp login", "ok" if ok else "fail", detail),
    ]
