"""The telegram-channel app's `personalclaw doctor` probe (manifest `cli.doctor`).

Registered via ``app.json`` → ``cli.doctor: "cli_doctor:probe"``. The core doctor
runner (``personalclaw.app_cli.run_app_doctor_probes``) imports ``probe`` and calls
it (bounded by a timeout + exception guard), rendering the returned
``list[DoctorLine]`` as this app's doctor section. Checks token presence + Telegram's own owner id,
with a hint to the Channels-page Test action for live ``getMe`` validation (which the
app owns, not core's doctor). The token is resolved by
:func:`telegram_runtime.settings.load_bot_token`, the order the channel itself uses.
"""

from personalclaw.sdk.channel import AppConfig, owner_id_credential, owner_id_for
from personalclaw.sdk.cli import DoctorLine

from telegram_runtime.settings import PROVIDER, load_bot_token


def probe() -> list[DoctorLine]:
    creds = AppConfig.load().load_credentials()
    if not load_bot_token(None, creds):
        return [
            DoctorLine(
                "status", "info",
                "not configured (dashboard-only mode) — run 'personalclaw setup' to add a bot token",
            )
        ]
    lines = [DoctorLine("token", "ok", "configured")]
    owner = owner_id_for(PROVIDER)
    if owner:
        lines.append(DoctorLine("owner", "ok", owner))
    else:
        lines.append(DoctorLine("owner", "warn", f"{owner_id_credential(PROVIDER)} not set"))
    lines.append(
        DoctorLine("bot", "info", "use the Channels page → Telegram → Test to verify the token (getMe)")
    )
    return lines
