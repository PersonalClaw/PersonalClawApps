"""The telegram-channel app's `personalclaw setup` step (manifest `cli.setup`).

Registered via ``app.json`` → ``cli.setup: "cli_setup:run"``. The core setup
runner (``personalclaw.app_cli.run_app_setup_steps``) imports ``run`` and calls it
with a :class:`personalclaw.sdk.cli.SetupContext` after the core steps. This is the
Telegram-specific setup: it reads/writes ONLY app-owned homes — the generic
credential store (via ``ctx.save_credential``, this app's own ``TELEGRAM_BOT_TOKEN``
key) and this app's ``ProviderSettings`` (the DM-activation posture). Core
config.json holds no Telegram config; who may talk is owned by the core trust seam.
"""

from personalclaw.sdk.channel import CRED_OWNER_ID
from personalclaw.sdk.cli import SetupContext

from telegram_runtime.settings import (
    ACTIVATION_ALWAYS,
    CRED_TELEGRAM_BOT_TOKEN,
    _VALID_ACTIVATIONS,
)

_APP = "telegram-channel"


def _mask(val: str) -> str:
    return val[:8] + "…" if len(val) > 12 else val


def run(ctx: SetupContext) -> None:
    """Prompt for the BotFather token (→ credential store) and DM activation mode
    (→ this app's ProviderSettings). Empty input keeps the current value; declining
    skips the whole step (the channel stays disabled)."""
    _setup_token(ctx)
    _setup_activation(ctx)


def _setup_token(ctx: SetupContext) -> None:
    ctx.print("── Telegram Channel App Credentials ──\n")
    ctx.print(
        "  Create a bot with @BotFather on Telegram:\n"
        "    1. Open a chat with @BotFather and send /newbot\n"
        "    2. Pick a display name and a username ending in 'bot'\n"
        "    3. Copy the HTTP API token it gives you (123456:ABC-...)\n"
        "  Optionally send /setprivacy → Disable to let the bot read group messages.\n"
    )

    answer = ctx.input("  Configure the Telegram bot token? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        ctx.print("  ⏭  Skipped. The Telegram channel will be disabled.\n")
        return

    cur_token = ctx.get_credential(CRED_TELEGRAM_BOT_TOKEN)
    cur_owner = ctx.get_credential(CRED_OWNER_ID)
    hint_token = f" [{_mask(cur_token)}]" if cur_token else ""
    hint_owner = f" [{cur_owner}]" if cur_owner else ""

    token = ctx.input(f"  Bot Token (123456:ABC-...){hint_token}: ").strip() or cur_token
    owner_id = ctx.input(f"  Your Telegram user ID{hint_owner}: ").strip() or cur_owner

    if not token:
        ctx.print("  ⚠️  No token — the Telegram channel will be disabled.\n")
        return

    ctx.save_credential(CRED_TELEGRAM_BOT_TOKEN, token)
    if owner_id:
        ctx.save_credential(CRED_OWNER_ID, owner_id)
    ctx.print("  ✅ Credentials saved.\n")


def _setup_activation(ctx: SetupContext) -> None:
    current = ctx.settings.load(_APP).get("dm_activation") or ACTIVATION_ALWAYS

    ctx.print("── DM Activation ──\n")
    ctx.print(f"  How should the bot respond in DMs? Options: {', '.join(sorted(_VALID_ACTIVATIONS))}\n")
    raw = ctx.input(f"  DM activation [{current}]: ").strip().lower()
    if not raw:
        raw = current
    if raw not in _VALID_ACTIVATIONS:
        ctx.print(f"  ⚠️  Unknown mode — keeping '{current}'.")
        raw = current

    ctx.settings.update(_APP, {"dm_activation": raw})
    ctx.print(f"  ✅ DM activation: {raw}\n")
