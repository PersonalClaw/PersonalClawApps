"""The telegram-channel app's `personalclaw doctor` probe (manifest `cli.doctor`).

Registered via ``app.json`` → ``cli.doctor: "cli_doctor:probe"``. The core doctor
runner (``personalclaw.app_cli.run_app_doctor_probes``) imports ``probe`` and calls
it (bounded by a timeout + exception guard), rendering the returned
``list[DoctorLine]`` as this app's doctor section. Checks token presence in the
generic credential store + owner id, with a hint to the Channels-page Test action
for live ``getMe`` validation (which the app owns, not core's doctor).
"""

from personalclaw.sdk.channel import CRED_OWNER_ID, AppConfig
from personalclaw.sdk.cli import DoctorLine

from telegram_runtime.settings import CRED_TELEGRAM_BOT_TOKEN


def probe() -> list[DoctorLine]:
    creds = AppConfig.load().load_credentials()
    if not creds.get(CRED_TELEGRAM_BOT_TOKEN):
        return [
            DoctorLine(
                "status", "info",
                "not configured (dashboard-only mode) — run 'personalclaw setup' to add a bot token",
            )
        ]
    lines = [DoctorLine("token", "ok", "configured")]
    owner = creds.get(CRED_OWNER_ID)
    if owner:
        lines.append(DoctorLine("owner", "ok", owner))
    else:
        lines.append(DoctorLine("owner", "warn", "PERSONALCLAW_OWNER_ID not set"))
    lines.append(
        DoctorLine("bot", "info", "use the Channels page → Telegram → Test to verify the token (getMe)")
    )
    return lines
