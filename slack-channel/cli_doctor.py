"""The slack-channel app's `personalclaw doctor` probe (manifest `cli.doctor`).

Registered via ``app.json`` → ``cli.doctor: "cli_doctor:probe"``. The core doctor
runner (``personalclaw.app_cli.run_app_doctor_probes``) imports ``probe`` and calls
it (bounded by a timeout + exception guard), rendering the returned
``list[DoctorLine]`` as this app's doctor section. Reproduces the presence check
core's doctor used to hardcode: token presence + owner id,
with a hint to the Channels-page Test action for live workspace validation (which
the app owns, not core's doctor).

Token presence is read through :func:`slack_runtime.settings.load_tokens`, the same
resolution the channel runs on. Setup and the Configure form write the tokens to this
app's store, so a check of the shared credential store alone would report a working
channel as "not configured". The owner is Slack's own (``owner_id_for("slack")``), the one the
channel checks, and is the owner only when core's owner pairing named it (``paired_owner``): the
channel forgets any other when it starts, and the line says so, and how to pair.
"""

from personalclaw.sdk.channel import AppConfig, owner_id_credential, owner_id_for, paired_owner
from personalclaw.sdk.cli import DoctorLine

from slack_runtime.settings import PAIR_AS_OWNER, load_tokens


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
    owner = owner_id_for("slack")
    if owner and owner == paired_owner("slack"):
        lines.append(DoctorLine("owner", "ok", owner))
    elif owner:
        lines.append(
            DoctorLine(
                "owner", "warn",
                f"{owner} was never paired here, so Slack forgets it when it starts — pair one in "
                f"the dashboard ({PAIR_AS_OWNER})",
            )
        )
    else:
        lines.append(
            DoctorLine(
                "owner", "warn",
                f"{owner_id_credential('slack')} not set — pair one in the dashboard ({PAIR_AS_OWNER})",
            )
        )
    lines.append(
        DoctorLine("workspace", "info", "use the Channels page → Slack → Test to verify the token")
    )
    return lines
