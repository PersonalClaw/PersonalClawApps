"""The slack-channel app's `personalclaw doctor` probe (manifest `cli.doctor`).

Registered via ``app.json`` → ``cli.doctor: "cli_doctor:probe"``. The core doctor
runner (``personalclaw.app_cli.run_app_doctor_probes``) imports ``probe`` and calls
it (bounded by a timeout + exception guard), rendering the returned
``list[DoctorLine]`` as this app's doctor section. Reproduces the presence check
core's doctor used to hardcode (plan 32 moved it here): token presence + owner id,
with a hint to the Channels-page Test action for live workspace validation (which
the app owns, not core's doctor).

Token presence is read through :func:`slack_runtime.settings.load_tokens`, the same
resolution the channel runs on. Setup and the Configure form write the tokens to this
app's store, so a check of the shared credential store alone would report a working
channel as "not configured".
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
    from slack_runtime.settings import load_tokens
finally:
    sys.path.remove(_APP_DIR)


def probe() -> list[DoctorLine]:
    creds = AppConfig.load().load_credentials()
    bot_token, app_token = load_tokens(None, creds)
    has_tokens = bool(bot_token and app_token)
    if not has_tokens:
        return [
            DoctorLine(
                "status", "info",
                "not configured (dashboard-only mode) — run 'personalclaw setup' to add tokens",
            )
        ]
    lines = [DoctorLine("tokens", "ok", "configured")]
    owner = creds.get(CRED_OWNER_ID)
    if owner:
        lines.append(DoctorLine("owner", "ok", owner))
    else:
        lines.append(DoctorLine("owner", "warn", "PERSONALCLAW_OWNER_ID not set"))
    lines.append(
        DoctorLine("workspace", "info", "use the Channels page → Slack → Test to verify the token")
    )
    return lines
