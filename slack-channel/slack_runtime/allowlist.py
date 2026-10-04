"""Slack user allowlist and tracking-channel management.

Handles two owner-approval workflows:

1. **User allowlist** — when a user joins a tracked channel
   (``member_joined_channel``) or is nominated via ``/personalclaw @user``,
   the owner gets a DM with Allow / Deny buttons.
2. **Tracking channel** — ``/personalclaw #channel`` sends an Add / Ignore
   prompt to the owner.  Approved channels are persisted to the app's own
   config store (SlackSettings).

Both flows share the same config persistence helpers so changes survive
gateway restarts.
"""

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from personalclaw.sdk.channel import AppConfig
from personalclaw.sdk.channel import (
    dashboard_origin,
    devspaces_proxy_url,
    is_local_bind,
    parse_dashboard_url,
    resolve_bind_host,
    resolve_dashboard_host,
)
from personalclaw.sdk.channel import forget_owner, owner_id_for, owner_sign_in_token, paired_owner
from personalclaw.sdk.channel import sel
from slack_runtime.handler import get_owner_id, is_owner, is_tracked_channel

if TYPE_CHECKING:
    from slack_runtime.client import SlackClientOps
    from slack_runtime.settings import SlackSettings

logger = logging.getLogger(__name__)

#: This channel's provider key: core keeps its owner under it (``owner_id_for``).
_PROVIDER = "slack"

# Block Kit action IDs shared with the interaction router
ACTION_ALLOWLIST_APPROVE = "allowlist_approve"
ACTION_ALLOWLIST_DENY = "allowlist_deny"
ACTION_TRACK_APPROVE = "track_channel_approve"
ACTION_TRACK_DENY = "track_channel_deny"


# ---------------------------------------------------------------------------
# Owner prompts — builds the Allow/Deny DMs
# ---------------------------------------------------------------------------


async def _send_prompt(
    slack: "SlackClientOps",
    owner_id: str,
    text: str,
    approve_label: str,
    deny_label: str,
    approve_action: str,
    deny_action: str,
    value: str,
    fallback: str,
) -> None:
    """Build a two-button Slack prompt and DM it to the owner."""
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": approve_label},
                    "style": "primary",
                    "action_id": approve_action,
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": deny_label},
                    "style": "danger",
                    "action_id": deny_action,
                    "value": value,
                },
            ],
        },
    ]
    try:
        dm = await slack.open_dm(owner_id)
        await slack.post_blocks(dm, blocks, fallback)
    except Exception:
        logger.exception("Failed to send prompt: %s", fallback)


async def prompt_track_channel(
    slack: "SlackClientOps",
    owner_id: str,
    channel_id: str,
    channel_name: str = "",
) -> None:
    """Send a Track / Ignore prompt to the owner for *channel_id*.

    When the channel is already tracked the prompt offers to keep or
    remove it instead of add/ignore.
    """
    if not channel_id:
        return

    already = is_tracked_channel(channel_id)
    logger.info(
        "track channel prompt: channel=%s (%s) already=%s",
        channel_id,
        channel_name,
        already,
    )

    if already:
        text = f"📡 <#{channel_id}> is currently tracked.\nKeep tracking or remove?"
        approve_label = "✅ Keep"
        deny_label = "🚫 Remove"
    else:
        text = f"📡 Track <#{channel_id}> for new member allowlist prompts?"
        approve_label = "✅ Track"
        deny_label = "🚫 Ignore"

    await _send_prompt(
        slack, owner_id, text, approve_label, deny_label,
        ACTION_TRACK_APPROVE, ACTION_TRACK_DENY,
        f"{channel_id}:{channel_name}", "Track channel — prompt",
    )


# ---------------------------------------------------------------------------
# Dashboard presigned link — always sent via DM, never in a channel
# ---------------------------------------------------------------------------


async def send_dashboard_link(
    slack: "SlackClientOps",
    user_id: str,
    ttl: int = 3600,
) -> str:
    """Generate a presigned dashboard URL and DM it to *user_id*, who must be the owner.

    Returns the generated URL (for logging), or an empty string when the DM failed.
    The link is always sent as a DM to prevent token leakage in channels.

    **Only the owner is sent one.** The link signs in as the owner, whatever id it names, and it
    was minted for anyone who asked: an allowed user's ``!dashboard`` got a link to the owner's
    whole dashboard. Core's ``owner_sign_in_token`` mints it for this channel's owner alone
    (an Enterprise Grid ``W…`` id is the same person as its ``U…`` form, as ``is_owner`` reads
    it) and refuses anyone else with ``ValueError`` carrying ``NOT_THE_OWNER_SENTENCE``, which
    propagates with nothing minted or sent: the caller shows it.

    The link can be opened until the sooner of the gateway's link window
    (``LINK_WINDOW_SECS``, 24 hours) and *ttl*, and the sign-in it starts ends *ttl* seconds
    after the link is made. The DM says both (``blocks.link_lifetime_text``).

    A *ttl* longer than the gateway lets a sign-in last is refused the same way, never
    shortened here: the ``ValueError``'s message is the sentence to show the person who asked.
    (A core from before that limit shortened the lifetime itself, without a word.)
    """
    from personalclaw.sdk.channel import LINK_WINDOW_SECS
    from slack_runtime.blocks import link_lifetime_text

    cfg = AppConfig.load()
    configured_host, port = parse_dashboard_url(cfg.dashboard.url)
    local_only = is_local_bind(resolve_bind_host())
    host = resolve_dashboard_host(local_only, configured_host)

    try:
        who = get_owner_id() if is_owner(user_id) else user_id
        token = owner_sign_in_token(_PROVIDER, who, ttl)
    except ValueError as exc:
        try:
            sel().log_api_access(
                caller=user_id,
                operation="slack.dashboard_token",
                outcome="denied",
                resources=f"ttl={ttl}",
                error=str(exc),
            )
        except Exception:
            logger.debug("could not audit a refused dashboard link", exc_info=True)
        raise
    origin = dashboard_origin(cfg.dashboard.url)
    url = f"{origin}/?token={token}" if origin else f"http://{host}:{port}/?token={token}"

    # Dev proxy: also provide proxy URL
    proxy_line = ""
    proxy = devspaces_proxy_url(port)
    if proxy:
        proxy_line = f"\n🔗 <{proxy}/?token={token}|Open via DevSpaces Proxy>"

    try:
        dm = await slack.open_dm(user_id)
        await slack.post_message(
            dm,
            f"🔗 <{url}|Open Dashboard>{proxy_line}\n"
            f"{link_lifetime_text(ttl, LINK_WINDOW_SECS)}",
        )
        sel().log_api_access(
            caller=user_id,
            operation="slack.dashboard_token",
            outcome="ok",
            resources=f"ttl={ttl}",
        )
    except Exception:
        try:
            sel().log_api_access(
                caller=user_id,
                operation="slack.dashboard_token",
                outcome="error",
                resources=f"ttl={ttl}",
            )
        except Exception:
            pass
        logger.exception("Failed to DM dashboard link to %s", user_id)
        return ""

    return url


# ---------------------------------------------------------------------------
# Config persistence — the app's OWN store (SlackSettings home), not core config.
# ---------------------------------------------------------------------------


def persist_allowed_user(user_id: str, name: str = "", *, remove: bool = False) -> None:
    """Add or remove *user_id* in the app store's ``allowed_users``."""
    from slack_runtime.settings import persist_list_entry, reload_settings

    persist_list_entry("allowed_users", "slack_id", user_id, remove=remove, name=name)
    reload_settings()
    # EA-7 write-through: the guarded inbound door consults core's channel_trust
    # store, so the owner's Allow/Deny ruling must land there too, not only in
    # SlackSettings — otherwise the two stores drift and the door rules on stale data.
    try:
        from personalclaw.sdk.channel import apply_trust_action

        apply_trust_action("deny" if remove else "allow", "slack", user_id, name)
    except Exception:
        logger.warning("channel_trust write-through failed for user %s", user_id, exc_info=True)


def persist_tracking_channel(channel_id: str, name: str = "", *, remove: bool = False) -> None:
    """Add or remove *channel_id* in the app store's ``tracking_channels``."""
    from slack_runtime.settings import persist_list_entry, reload_settings

    persist_list_entry("tracking_channels", "channel_id", channel_id, remove=remove, name=name)
    reload_settings()
    # EA-7 write-through — same reason as persist_allowed_user above.
    try:
        from personalclaw.sdk.channel import track, untrack

        if remove:
            untrack("slack", channel_id)
        else:
            track("slack", channel_id, name)
    except Exception:
        logger.warning(
            "channel_trust write-through failed for channel %s", channel_id, exc_info=True
        )


def member_name(
    user_id: str, settings: "SlackSettings", known: Mapping[str, str] | None = None
) -> str:
    """What to call a Slack member on the Sender trust page: the name the owner gave them in
    Allowed Users, else the display name this app already knows (*known*, member ID → name), else
    ``""``, and the page shows the ID."""
    for user in settings.allowed_users:
        if user.get("slack_id") == user_id and user.get("name"):
            return str(user["name"])
    shown = (known or {}).get(user_id, "")
    return shown if shown and shown != user_id else ""


def sync_channel_trust(settings: "SlackSettings") -> None:
    """Mirror the app store's tracked channels into core's channel_trust store (EA-7).

    The guarded inbound door consults core's per-provider trust store, not this
    app's SlackSettings, and the Sender trust page lists it. Each tracked channel
    is tracked, under the name the owner gave it. The owner is not written here:
    core's owner pairing trusted her when it named her. Run on every start: core
    writes and audits only what changed, so a name given since the last start
    reaches the page.
    """
    from personalclaw.sdk.channel import track

    try:
        for channel in settings.tracking_channels:
            cid = str(channel.get("channel_id", "") or "")
            if cid:
                track("slack", cid, str(channel.get("name", "") or ""))
    except Exception:
        logger.warning("channel_trust mirror failed", exc_info=True)


def forget_unpaired_owner() -> None:
    """Have core forget an owner it holds for Slack that core's owner pairing did not name.

    Slack keeps no owner but the one its pairing named. An earlier release made the first person
    to message the bot its owner, and stored them where setup stored a member id typed at its
    prompt and where a container can set one in the environment; nothing tells those apart, and
    none was confirmed from the account. So when the channel starts, any owner core holds for
    Slack other than the paired one is forgotten (``forget_owner``): no longer read, no longer
    trusted, and the shared key every channel wrote before each had its own no longer answers
    for Slack. The owner pairs once more from the Configure page. Fails loudly: a forget that
    cannot be written raises, and the channel does not start on an owner nobody paired. Logs
    that it forgot one, never the id.
    """
    owner = owner_id_for(_PROVIDER)
    if not owner or owner == paired_owner(_PROVIDER):
        return
    if forget_owner(_PROVIDER, owner):
        from slack_runtime.settings import PAIR_AS_OWNER

        logger.warning(
            "Slack forgot the owner it held, which was never paired here. Pair one in the "
            "dashboard: %s, then send the bot the code in a direct message.",
            PAIR_AS_OWNER,
        )
