"""The slack-channel app's `personalclaw setup` step (manifest `cli.setup`).

Registered via ``app.json`` → ``cli.setup: "cli_setup:run"``. The core setup
runner (``personalclaw.app_cli.run_app_setup_steps``) imports ``run`` and calls
it with a :class:`personalclaw.sdk.cli.SetupContext` after the core steps. This
is the slack-specific setup that used to live hardcoded in core's ``cli_setup.py``
(plan 32 PROVIDER-BOUNDARY-COMPLETION moved it here). Where each answer goes:

- the Bot and App tokens → this app's ``ProviderSettings`` (``bot_token`` / ``app_token``,
  the fields the Apps page's Configure form writes too). Saving them there keeps each value
  in the credential store under a key THIS APP owns and puts only a reference in the
  settings file, so uninstalling the app removes them. They used to go to the shared store
  under the plain names ``SLACK_BOT_TOKEN`` / ``SLACK_APP_TOKEN``, which no uninstall can
  attribute to an app, so both tokens outlived the app;
- the owner's Slack member id → the shared store under ``PERSONALCLAW_OWNER_ID``, which core's
  gateway reads by that name (an identity, not a secret, and not this app's alone);
- the slash-command name → this app's ``ProviderSettings``.

Core config.json holds no Slack config.
"""

from personalclaw.sdk.channel import (
    CRED_OWNER_ID,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
)
from personalclaw.sdk.cli import SetupContext

from slack_runtime.settings import load_tokens

_APP = "slack-channel"


def _mask(val: str) -> str:
    return val[:8] + "…" if len(val) > 12 else val


def run(ctx: SetupContext) -> None:
    """Prompt for the Slack tokens and the slash command name (→ this app's
    ProviderSettings) and the owner ID (→ the shared credential store). Empty input
    keeps the current value; declining skips the whole step (the channel stays
    disabled)."""
    _setup_tokens(ctx)
    _setup_slash_command(ctx)


def _setup_tokens(ctx: SetupContext) -> None:
    ctx.print("── Slack Channel App Credentials ──\n")
    ctx.print(
        "  Create a Slack app at https://api.slack.com/apps → 'From a manifest',\n"
        "  using this app's slack-manifest.yaml (replace {{USERNAME}}),\n"
        "  then paste its tokens below.\n"
    )

    answer = ctx.input("  Configure Slack tokens? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        ctx.print("  ⏭  Skipped. The Slack channel will be disabled.\n")
        return

    # The tokens the channel runs on now, by the runtime's own resolution: this app's store,
    # then the plain-named keys an earlier release's setup wrote. Enter keeps that value.
    legacy = {k: ctx.get_credential(k) for k in (CRED_SLACK_BOT_TOKEN, CRED_SLACK_APP_TOKEN)}
    cur_bot, cur_app = load_tokens(ctx.settings.load(_APP), legacy)
    cur_owner = ctx.get_credential(CRED_OWNER_ID)

    hint_app = f" [{_mask(cur_app)}]" if cur_app else ""
    hint_bot = f" [{_mask(cur_bot)}]" if cur_bot else ""
    hint_owner = f" [{cur_owner}]" if cur_owner else ""

    app_token = ctx.input(f"  App Token (xapp-...){hint_app}: ").strip() or cur_app
    bot_token = ctx.input(f"  Bot Token (xoxb-...){hint_bot}: ").strip() or cur_bot
    owner_id = ctx.input(f"  Your Slack Member ID{hint_owner}: ").strip() or cur_owner

    if not app_token or not bot_token:
        ctx.print("  ⚠️  Missing tokens — the Slack channel will be disabled.\n")
        return

    ctx.settings.update(_APP, {"app_token": app_token, "bot_token": bot_token})
    if owner_id:
        ctx.save_credential(CRED_OWNER_ID, owner_id)
    ctx.print("  ✅ Credentials saved.\n")


def _setup_slash_command(ctx: SetupContext) -> None:
    current = ctx.settings.load(_APP).get("command") or "personalclaw"

    ctx.print("── Slash Command ──\n")
    raw = ctx.input(f"  Slash command name [{current}]: ").strip()
    if raw:
        raw = raw.lstrip("/").strip()
    if not raw:
        raw = current
    if not all(c.isalnum() or c in "-_" for c in raw):
        ctx.print("  ⚠️  Command name should only contain letters, numbers, hyphens, or underscores.")
        raw = current
    if len(raw) > 32:
        ctx.print("  ⚠️  Command name too long (max 32 chars).")
        raw = current

    ctx.settings.update(_APP, {"command": raw})
    ctx.print(f"  ✅ Slash command: /{raw}\n")
