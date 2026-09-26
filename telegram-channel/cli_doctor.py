"""The telegram-channel app's `personalclaw doctor` probe (manifest `cli.doctor`).

Registered via ``app.json`` → ``cli.doctor: "cli_doctor:probe"``. The core doctor
runner (``personalclaw.app_cli.run_app_doctor_probes``) imports ``probe`` and calls
it (bounded by a timeout + exception guard), rendering the returned
``list[DoctorLine]`` as this app's doctor section. Checks token presence + owner id,
with a hint to the Channels-page Test action for live ``getMe`` validation (which the
app owns, not core's doctor). The token is resolved by
:func:`telegram_runtime.settings.load_bot_token`, the order the channel itself uses.
"""

import sys
from pathlib import Path

from personalclaw.sdk.channel import CRED_OWNER_ID, AppConfig
from personalclaw.sdk.cli import DoctorLine

# `personalclaw doctor` loads this file by path, and core's loader does not put the app's
# directory on sys.path the way the gateway's provider loader does, so an import of this app's
# own package failed there and the probe read "unavailable". Hold it on the path while the
# import runs, as the provider loader does.
_APP_DIR = str(Path(__file__).resolve().parent)
sys.path.insert(0, _APP_DIR)
try:
    from telegram_runtime.settings import load_bot_token
finally:
    sys.path.remove(_APP_DIR)


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
    owner = creds.get(CRED_OWNER_ID)
    if owner:
        lines.append(DoctorLine("owner", "ok", owner))
    else:
        lines.append(DoctorLine("owner", "warn", "PERSONALCLAW_OWNER_ID not set"))
    lines.append(
        DoctorLine("bot", "info", "use the Channels page → Telegram → Test to verify the token (getMe)")
    )
    return lines
