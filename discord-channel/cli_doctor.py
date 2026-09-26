"""The discord-channel app's `personalclaw doctor` probe (manifest `cli.doctor`).

Registered via ``app.json`` → ``cli.doctor: "cli_doctor:probe"``. The core doctor
runner (``personalclaw.app_cli.run_app_doctor_probes``) imports ``probe`` and calls
it (bounded by a timeout + exception guard), rendering the returned
``list[DoctorLine]`` as this app's doctor section. Checks the token (resolved by
:func:`discord_runtime.settings.load_bot_token`, the order the channel itself uses),
the application id in this app's own store, and the owner id, with a pointer to the
Channels-page Test action for the live ``GET /gateway/bot`` hello probe (which the app
owns, not core's doctor — the same division as Telegram's).
"""

import sys
from pathlib import Path

from personalclaw.sdk.channel import CRED_OWNER_ID, AppConfig, ProviderSettings
from personalclaw.sdk.cli import DoctorLine

# `personalclaw doctor` loads this file by path, and core's loader does not put the app's
# directory on sys.path the way the gateway's provider loader does, so an import of this app's
# own package failed there and the probe read "unavailable". Hold it on the path while the
# import runs, as the provider loader does.
_APP_DIR = str(Path(__file__).resolve().parent)
sys.path.insert(0, _APP_DIR)
try:
    from discord_runtime.settings import load_bot_token
finally:
    sys.path.remove(_APP_DIR)

_APP = "discord-channel"


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

    app_id = ProviderSettings.load(_APP).get("application_id") or ""
    if app_id:
        lines.append(DoctorLine("application id", "ok", str(app_id)))
    else:
        # Not fatal: the runtime never needs it (interactions carry their own token).
        # It only costs the setup step's invite URL, so warn rather than fail.
        lines.append(
            DoctorLine("application id", "warn", "not set — no bot invite URL can be printed")
        )

    owner = creds.get(CRED_OWNER_ID)
    if owner:
        lines.append(DoctorLine("owner", "ok", owner))
    else:
        lines.append(DoctorLine("owner", "warn", "PERSONALCLAW_OWNER_ID not set"))

    lines.append(
        DoctorLine(
            "gateway", "info",
            "use the Channels page → Discord → Test for the live gateway hello probe",
        )
    )
    lines.append(
        DoctorLine(
            "intent", "info",
            "the MESSAGE CONTENT privileged intent must be enabled in the Developer "
            "Portal or inbound messages arrive with empty content",
        )
    )
    return lines
