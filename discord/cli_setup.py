"""CLI setup for the Discord app."""

from personalclaw.sdk.cli import CliSetup

_APP = "discord"
_LABEL = "Discord"


class DiscordCliSetup(CliSetup):
    app_name = _APP
    label = _LABEL

    def configure(self) -> dict:
        return {
            "app": _APP,
            "label": _LABEL,
            "account_settings": [
                {"id": "bot_token", "label": "Bot Token", "sensitive": True},
                {"id": "application_id", "label": "Application ID"},
            ],
            "capabilities": [
                {
                    "id": "channel",
                    "label": "Chat with your agent in Discord",
                    "starts_on": True,
                    "settings": [
                        {"id": "dm_activation", "label": "DM Activation", "type": "boolean"},
                    ],
                },
                {
                    "id": "triggers",
                    "label": "Run automations on Discord messages",
                    "starts_on": False,
                },
            ],
        }


setup = DiscordCliSetup()
