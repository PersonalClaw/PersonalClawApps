"""Discord app settings resolved via the SDK account/capability API."""

_APP = "discord"
PROVIDER = "discord"

ACCOUNT_SETTINGS = [
    {
        "id": "bot_token",
        "type": "sensitive",
        "label": "Bot Token",
        "description": "Discord bot token for API access.",
    },
    {
        "id": "application_id",
        "type": "string",
        "label": "Application ID",
        "description": "Discord application ID.",
    },
]

CAPABILITY_SETTINGS = {
    "channel": [
        {
            "id": "dm_activation",
            "type": "boolean",
            "label": "DM Activation",
            "description": "Require explicit user consent before the bot responds to DMs.",
        }
    ],
}


def get_bot_token(account_settings: dict) -> str | None:
    """Return the bot token from account-level settings."""
    return account_settings.get("bot_token")


def get_application_id(account_settings: dict) -> str | None:
    """Return the application ID from account-level settings."""
    return account_settings.get("application_id")


def get_dm_activation(capability_settings: dict) -> bool:
    """Return whether DM activation is enabled for the channel capability."""
    return capability_settings.get("dm_activation", True)


def resolve_settings(account_api, capability_api):
    """Resolve all settings through the SDK account and capability APIs."""
    account_data = account_api.get_account_settings(_APP)
    capability_data = capability_api.get_capability_settings(_APP, "channel")
    dm_activation = capability_data.get("dm_activation", True)
    return {
        "bot_token": account_data.get("bot_token"),
        "application_id": account_data.get("application_id"),
        "dm_activation": dm_activation,
    }
