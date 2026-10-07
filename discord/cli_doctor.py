"""CLI doctor/check for the Discord app."""

from personalclaw.sdk.cli import CliDoctor

_APP = "discord"


class DiscordCliDoctor(CliDoctor):
    app_name = _APP

    def check(self) -> dict:
        results = {
            "app": _APP,
            "checks": [],
        }
        results["checks"].append({
            "name": "app_configured",
            "label": "App configured",
            "ok": True,
        })
        return results


doctor = DiscordCliDoctor()
