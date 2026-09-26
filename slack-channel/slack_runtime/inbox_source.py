"""Slack inbox source — the poll-based ``MessageSourceProvider`` half of this app.

The app already registers a ``channel``
provider (``transport.create_provider``) for the live Socket-Mode conversation.
This module adds the *second* provider: an ``inbox`` one, so Slack messages also
reach the generic Inbox through core's vendor-neutral message-source seam instead
of a Slack-shaped path in core. Core only ever sees ``MessageSourceProvider`` /
``IncomingMessage``, imported from ``personalclaw.sdk`` — the only import surface
an app is allowed to use.

It is a thin ADAPTER, not a second Slack client: every call delegates to the
existing ``slack_runtime.client.RealSlackClient`` through the ``SlackClientOps``
ABC, so a test can substitute the bundle's ``MockSlackClient`` unchanged.

Checkpointing: core hands us the per-channel cursor dict it persisted last time
and expects the updated one back. The cursor is a Slack message ``ts``, passed
straight to ``conversations.history``'s ``oldest``, which is EXCLUSIVE when
``inclusive`` is not set (see ``client.fetch_history``) — so a poll never
re-delivers what the previous poll already returned, and the ``ts == cursor``
equality skip the earlier draft needed is unnecessary. Advancing the cursor is
deliberately decoupled from FILTERING: a bot/own message still moves the cursor
(it was seen and judged), while a channel whose fetch RAISED keeps its old cursor
so the next poll retries the same window rather than silently skipping messages.

**A channel's first poll starts after its newest message.** A channel with no cursor yet
(newly watched, or the first poll after the app is enabled) records the ts of its newest
message and surfaces nothing: those messages were not sent to PersonalClaw, and every one
surfaced raises an inbox event, so the channel's backlog would fire the owner's inbox
automations at once. Mail Inbox and Email Channel start the same way.

**A poll that can read no watched channel RAISES**, with a sentence naming each channel and
Slack's reason, which core's inbox shows as this source's health; it used to log at debug
and return nothing, so a revoked token read like a quiet workspace. A channel that fails
while others read is logged at WARNING and read again at the next poll.
"""

from __future__ import annotations

import logging
import sys as _sys
from pathlib import Path as _Path
from typing import Any

# The app loader only keeps this app's dir on sys.path while it execs THIS module,
# but the sibling ``slack_runtime.*`` imports below must keep resolving for the life
# of the process (the inbox service polls long after boot). Pin it, exactly as
# transport.py does for the channel half.
_APP_DIR = str(_Path(__file__).resolve().parents[1])
if _APP_DIR not in _sys.path:
    _sys.path.insert(0, _APP_DIR)

from personalclaw.sdk.inbox import IncomingMessage, MessageSourceProvider

from slack_runtime.client import RealSlackClient, SlackClientOps
from slack_runtime.settings import LiveConfig, load_tokens

logger = logging.getLogger(__name__)

#: Slack returns newest-first; cap a single poll. (A channel's first poll surfaces nothing:
#: it only records where the channel is.)
_POLL_LIMIT = 50


class SlackUnreadable(Exception):
    """No watched channel could be read. Its message is the sentence the inbox shows."""


def _slack_reason(exc: BaseException) -> str:
    """Slack's own word for a failed call (``not_in_channel``, ``invalid_auth``), else the
    exception's text: ``SlackApiError`` spells its reply over two lines around a URL."""
    response = getattr(exc, "response", None)
    try:
        said = response.get("error") if response is not None else None
    except Exception:  # noqa: BLE001 - a response that cannot be read says nothing
        said = None
    return str(said or exc or type(exc).__name__)


class SlackInboxSource(MessageSourceProvider):
    """Expose Slack as a generic inbox message source."""

    #: Shown by /api/inbox/providers alongside ``source_name``.
    display_name = "Slack"

    def __init__(
        self, config: dict[str, Any] | None = None, client: SlackClientOps | None = None
    ) -> None:
        # The config, kept live exactly as SlackTransport keeps it, and the token resolved
        # through the same ``load_tokens``: the registry used to build this provider once, at
        # enable, with a Configure → Save re-cycling nothing, so a token read once in
        # ``__init__`` kept polling with the token it started with (none, on an install
        # configured after enable) until a restart. One resolution for both providers also
        # means they never disagree about which workspace this app is bound to.
        self._config = LiveConfig(config if config is not None else {})
        # ``client`` is the test seam (MockSlackClient); production passes none.
        self._injected = client
        #: ``(token, client)`` last built, so an unchanged token keeps its client.
        self._built: tuple[str, SlackClientOps] | None = None
        # Resolved lazily and cached: a display name per Slack user id. Bounded by
        # the number of distinct senders in watched channels.
        self._names: dict[str, str] = {}

    @property
    def _client(self) -> SlackClientOps:
        """The Slack client for the bot token configured now, rebuilt when that token changes."""
        if self._injected is not None:
            return self._injected
        token = load_tokens(self._config.current())[0]
        if self._built is None or self._built[0] != token:
            self._built = (token, RealSlackClient(token))
        return self._built[1]

    @property
    def source_name(self) -> str:
        return "slack"

    async def poll(
        self, watched_channels: list[str], checkpoints: dict[str, str], user_id: str
    ) -> tuple[list[IncomingMessage], dict[str, str]]:
        """Fetch messages newer than each channel's checkpoint.

        Returns ``(messages, updated_checkpoints)``. A channel with no checkpoint yet only
        records where it is (see the module docstring). A channel whose fetch fails keeps
        its old checkpoint, so the next poll retries the same window instead of skipping
        past unread messages; when every watched channel fails, the poll raises.
        """
        out: list[IncomingMessage] = []
        cursors = dict(checkpoints)
        failed: list[str] = []
        for channel_id in watched_channels:
            first = channel_id not in checkpoints
            since = checkpoints.get(channel_id, "0")
            try:
                limit = 1 if first else _POLL_LIMIT
                raw = await self._client.fetch_history(channel_id, since, limit)
            except Exception as exc:  # noqa: BLE001 - one channel's failure is reported, not fatal
                # Keep the old cursor: a transient API error must not look like
                # "nothing new" and consume the unread window.
                reason = _slack_reason(exc)
                failed.append(f"{channel_id} ({reason})")
                logger.warning(
                    "slack inbox: channel %s could not be read (%s); it is read again at the "
                    "next poll",
                    channel_id, reason,
                )
                continue
            if first:
                start = max((str(m.get("ts", "")) for m in raw), key=_ts_epoch, default="0")
                cursors[channel_id] = start if _ts_epoch(start) else "0"
                logger.info(
                    "slack inbox: first poll of %s — starting after %s, so the messages already "
                    "there are not surfaced",
                    channel_id, cursors[channel_id],
                )
                continue
            newest = since
            # Slack returns newest-first; reverse so the Inbox reads oldest-first.
            for msg in reversed(raw):
                ts = str(msg.get("ts", ""))
                if not ts:
                    continue
                # Cursor advances for every message SEEN, including ones filtered
                # below — they were judged, not missed.
                if _ts_newer(ts, newest):
                    newest = ts
                sender = str(msg.get("user", ""))
                # Skip our own messages and anything without a human author (bots,
                # joins, topic changes) — those are not inbox-worthy.
                if not sender or sender == user_id or msg.get("bot_id"):
                    continue
                out.append(
                    IncomingMessage(
                        id=ts,
                        channel_id=channel_id,
                        channel_name=channel_id,
                        thread_id=str(msg.get("thread_ts")) if msg.get("thread_ts") else None,
                        text=str(msg.get("text", "")),
                        sender_id=sender,
                        sender_name=await self.resolve_user_name(sender),
                        timestamp=_ts_epoch(ts),
                        is_dm=channel_id.startswith("D"),
                    )
                )
            cursors[channel_id] = newest
        if failed and len(failed) == len(watched_channels):
            raise SlackUnreadable(
                "none of the watched Slack channels could be read: " + ", ".join(failed)
            )
        return out, cursors

    async def send_reply(
        self, channel_id: str, text: str, thread_ts: str | None = None
    ) -> "bool | ReplyNotPosted":
        """``True`` once Slack took the reply; otherwise a falsy :class:`ReplyNotPosted`
        whose ``str()`` is Slack's reason, which core's inbox shows the owner who pressed
        Send (a plain ``False`` told them nothing)."""
        try:
            await self._client.post_message(channel_id, text, thread_ts)
            return True
        except Exception as exc:  # noqa: BLE001 - the reason is the owner's answer
            logger.warning("slack inbox: the reply to %s was not posted", channel_id, exc_info=True)
            return ReplyNotPosted(channel_id, _slack_reason(exc))

    async def add_reaction(self, channel_id: str, ts: str, emoji: str) -> bool:
        try:
            # The client returns None and swallows its own API errors (reactions are
            # best-effort by design), so a clean return is the only success signal
            # available here.
            await self._client.add_reaction(channel_id, ts, emoji)
            return True
        except Exception:
            logger.debug("inbox add_reaction failed for %s", channel_id, exc_info=True)
            return False

    async def get_channel_history(
        self, channel_id: str, oldest: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Raw history for digest/context use. Empty list on failure.

        Unlike ``poll``, an error here is not cursor-bearing — the caller wants
        best-effort context, so degrading to "no history" is correct.
        """
        try:
            return [dict(m) for m in await self._client.fetch_history(channel_id, oldest, limit)]
        except Exception:
            logger.debug("inbox history failed for %s", channel_id, exc_info=True)
            return []

    async def resolve_user_name(self, user_id: str) -> str:
        if user_id in self._names:
            return self._names[user_id]
        try:
            info = await self._client.get_user_info(user_id) or {}
            name = info.get("real_name") or info.get("name") or user_id
        except Exception:
            logger.debug("inbox resolve_user_name failed for %s", user_id, exc_info=True)
            name = user_id
        self._names[user_id] = name
        return name


class ReplyNotPosted:
    """A reply Slack did not take: falsy, and its ``str()`` says why (Email Channel's
    ``SendRefused`` convention, which core's inbox reads)."""

    def __init__(self, channel_id: str, reason: str) -> None:
        self.channel_id = channel_id
        self.reason = reason

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return f"Slack refused it in {self.channel_id} ({self.reason})."


def _ts_epoch(ts: str) -> float:
    """Slack ``ts`` ("1700000000.000100") as epoch seconds; 0.0 if unparseable."""
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _ts_newer(candidate: str, current: str) -> bool:
    """True if ``candidate`` is a later Slack ts than ``current``.

    Compared NUMERICALLY, not as strings: Slack ts strings are fixed-width in
    practice, but a lexicographic compare would order "9999999999.0" above
    "10000000000.0" the moment the epoch gains a digit. Unparseable values never
    win, so a malformed ts cannot poison the cursor.
    """
    try:
        return float(candidate) > float(current)
    except (TypeError, ValueError):
        return False


def create_provider(config: dict[str, Any] | None = None) -> SlackInboxSource:
    """Manifest factory — mirrors ``transport.create_provider``'s contract."""
    return SlackInboxSource(config)
