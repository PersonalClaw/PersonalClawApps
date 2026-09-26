"""The telegram-channel app's `personalclaw setup` step (manifest `cli.setup`).

Registered via ``app.json`` → ``cli.setup: "cli_setup:run"``. The core setup
runner (``personalclaw.app_cli.run_app_setup_steps``) imports ``run`` and calls it
with a :class:`personalclaw.sdk.cli.SetupContext` after the core steps. This is the
Telegram-specific setup. The bot token and the DM-activation posture go to this app's
``ProviderSettings``, whose save keeps the token in the credential store under a key
this app owns (so uninstalling the app removes it; the plain ``TELEGRAM_BOT_TOKEN`` name
it used to be saved under outlived the app). The owner's Telegram user id goes to the
credential store under Telegram's OWN owner key, ``owner_id_credential("telegram")``
(``PERSONALCLAW_OWNER_ID_TELEGRAM``), which core reads to reach the owner on Telegram. Every
channel used to write the one shared ``PERSONALCLAW_OWNER_ID``, so setting up a second channel
replaced this one's owner with an id from another platform.
Core config.json holds no Telegram config; who may talk is owned by the core trust seam.
"""

from personalclaw.sdk.channel import owner_id_credential, owner_id_for
from personalclaw.sdk.cli import SetupContext

from telegram_runtime.settings import (
    ACTIVATION_ALWAYS,
    CRED_TELEGRAM_BOT_TOKEN,
    PROVIDER,
    _VALID_ACTIVATIONS,
    load_bot_token,
)

_APP = "telegram-channel"


def _mask(val: str) -> str:
    return val[:8] + "…" if len(val) > 12 else val


def run(ctx: SetupContext) -> None:
    """Prompt for the BotFather token and DM activation mode (→ this app's
    ProviderSettings) and the owner's user id (→ Telegram's own owner key in the credential
    store). Empty input keeps the current value; declining skips the whole step (the channel
    stays disabled)."""
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

    # The token the channel runs on now (this app's store, then the plain-named key an earlier
    # release's setup wrote). Enter keeps it.
    legacy = {CRED_TELEGRAM_BOT_TOKEN: ctx.get_credential(CRED_TELEGRAM_BOT_TOKEN)}
    cur_token = load_bot_token(ctx.settings.load(_APP), legacy)
    # Telegram's own owner, or the shared one an earlier release wrote (core falls back to
    # it). Enter keeps it, which stores it under Telegram's own key from here on.
    cur_owner = owner_id_for(PROVIDER)
    hint_token = f" [{_mask(cur_token)}]" if cur_token else ""
    hint_owner = f" [{cur_owner}]" if cur_owner else ""

    token = ctx.input(f"  Bot Token (123456:ABC-...){hint_token}: ").strip() or cur_token
    owner_id = ctx.input(f"  Your Telegram user ID{hint_owner}: ").strip() or cur_owner

    if not token:
        ctx.print("  ⚠️  No token — the Telegram channel will be disabled.\n")
        return

    ctx.settings.update(_APP, {"bot_token": token})
    if owner_id:
        ctx.save_credential(owner_id_credential(PROVIDER), owner_id)
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
