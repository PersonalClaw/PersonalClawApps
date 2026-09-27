"""DiscordDelivery — the app-side ChannelDelivery the gateway delivers through.

All Discord rendering (message splitting, throttled edit-streaming, the
button-component approval prompt + owner-response wait, reactions, the typing
indicator) lives HERE, so core delivers with plain text + structured intent and
never imports Discord code. The transport registers an instance onto the gateway +
dashboard at ``start_inbound``.

Two Discord-specific shapes drive this module:

* **Streaming is edit-based.** Discord has no chunk-append API, so a "stream" is
  one message repeatedly PATCHed. Message edits share a per-channel rate bucket
  with sends, so :class:`DiscordDelivery` throttles to at most one edit per
  :data:`_EDIT_MIN_INTERVAL` seconds and always flushes the exact final text on
  ``stop_stream`` — the contract the fake-API tests pin. The stream's placeholder
  ("Thinking…") is gone when it stops: the message keeps the task lines alone, or is
  deleted when there were none, because the reply is a message of its own and a
  placeholder left above it reads as a turn that never finished.
* **Approvals are message COMPONENTS.** An action row of two buttons; the press
  arrives back as an ``INTERACTION_CREATE`` (not a message), which MUST be answered
  within three seconds or Discord shows the user "This interaction failed". When
  the decision resolves, the prompt is edited to show the outcome AND its
  ``components`` are cleared — a still-clickable approval button on a
  hours-old decided request is a real footgun, not a cosmetic one. A prompt too
  long for one message is split like a reply, the buttons on its last part.

Discord renders standard markdown, so unlike Telegram's MarkdownV2 there is no
escaping layer: the model's markdown goes out as-is. Length is the only rendering
constraint, hence :func:`split_message` — which keeps a code block whole in every
message it spans, because Discord renders each message's markdown on its own.

Core masks every text it hands this handle, keys and exfiltration URLs included, before
any method here is called, so nothing here masks it again.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

from personalclaw.sdk.channel import is_tracked_channel, sel

from discord_runtime.api import (
    BUTTON_STYLE_DANGER,
    BUTTON_STYLE_SUCCESS,
    COMPONENT_ACTION_ROW,
    COMPONENT_BUTTON,
    DISCORD_MAX_TEXT,
    DiscordAPI,
)

logger = logging.getLogger(__name__)

# Minimum wall-clock seconds between two edits of the same streamed message.
# Discord's per-channel message bucket is roughly 5 requests / 5 seconds, and edits
# spend from the same budget as the sends around them, so 1.1s leaves headroom.
_EDIT_MIN_INTERVAL = 1.1
# Approval prompts wait this long for the owner's button press before defaulting to
# rejected (mirrors Slack + Telegram's 2h ceiling).
_APPROVAL_TIMEOUT = 7200
# custom_id prefixes. Discord caps custom_id at 100 chars; a request id is short.
_APPROVE = "approve"
_DENY = "deny"
# The component interaction type on INTERACTION_CREATE (3 = MESSAGE_COMPONENT).
# Slash commands (2) and modals (5) arrive on the same event and are NOT ours.
INTERACTION_TYPE_COMPONENT = 3


#: A line that opens or closes a fenced code block (CommonMark allows three spaces of indent).
_FENCE_LINE_RE = re.compile(r"^ {0,3}```")
#: The longest info string (a code block's language) carried onto a block reopened in the next
#: message. Anything longer, or with a space or backtick in it, is not a language tag.
_MAX_FENCE_INFO = 32
_CLOSE_FENCE = "```"


def split_message(text: str, limit: int = DISCORD_MAX_TEXT) -> list[str]:
    """Split *text* into messages of at most *limit* characters, at line breaks where it can.

    Discord rejects a message body over 2000 chars with ``50035 Invalid Form Body``, so a long
    reply goes out as several messages, and Discord renders each one's markdown on its own. A
    code block cut in two is therefore closed at the end of one message and opened again, with
    its language, at the start of the next: cut anywhere, the first message kept its fence open
    and the second showed the rest of the code as markdown. A line longer than a whole message is
    cut at its last space that fits, or where it has to be in code (whose spaces are content) and
    in a run with no space."""
    if len(text) <= limit:
        return [text] if text else []
    lines = text.split("\n")
    parts: list[str] = []
    i = 0
    #: The opening line of the code block line ``i`` is in; "" outside one.
    fence = ""
    while i < len(lines):
        if not fence:
            # Outside code, the break between two messages already separates them: a
            # message does not start on blank lines.
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i == len(lines):
                break
        head = [_reopening(fence)] if fence else []
        body: list[str] = []
        state = fence
        while i < len(lines):
            after = _fence_after(state, lines[i])
            if len(_part(head + body + [lines[i]], after)) > limit:
                break
            body.append(lines[i])
            state = after
            i += 1
        if i < len(lines) and len(body) > 1 and _opens(body[-1], state):
            # A message does not end on the line that opens a code block: the block would
            # arrive empty, and its code as the next message's.
            body.pop()
            i -= 1
            state = ""
        if i < len(lines) and (not body or _opens(body[-1], state)):
            # The next line does not fit even at the start of a message: its longest piece
            # that does ends this one, and the rest of it starts the next.
            before = len("\n".join(head + body + [""]))
            room = max(1, limit - before - (len(_CLOSE_FENCE) + 1 if state else 0))
            piece, lines[i] = _cut(lines[i], room, in_code=bool(state))
            body.append(piece)
        if not state:
            while body and not body[-1].strip():
                body.pop()
        parts.append(_part(head + body, state))
        fence = state
    return parts


def _part(lines: list[str], fence: str) -> str:
    """*lines* as one message, its code block closed when the message ends inside one."""
    text = "\n".join(lines)
    return f"{text}\n{_CLOSE_FENCE}" if fence else text


def _fence_after(fence: str, line: str) -> str:
    """The code block the text is in after *line*: a fence line opens one, or closes it."""
    if not _FENCE_LINE_RE.match(line):
        return fence
    return "" if fence else line.strip()


def _opens(line: str, fence: str) -> bool:
    """Whether *line* opened the block *fence* (the state after it) is in."""
    return bool(fence) and bool(_FENCE_LINE_RE.match(line))


def _reopening(fence: str) -> str:
    """The line that reopens the code block *fence* opened, in the next message."""
    info = fence[3:].strip()
    if info and len(info) <= _MAX_FENCE_INFO and " " not in info and "`" not in info:
        return f"{_CLOSE_FENCE}{info}"
    return _CLOSE_FENCE


def _cut(line: str, room: int, *, in_code: bool) -> tuple[str, str]:
    """``(piece, rest)``: the first *room* characters of *line*, ended at the last space in
    them outside code (the cut drops that space, as a line break is dropped at a cut)."""
    prefix = line[:room]
    space = prefix.rfind(" ")
    if not in_code and space > 0 and prefix[:space].strip():
        return prefix[:space], line[space + 1:]
    return prefix, line[room:]


class _StreamState:
    """Bookkeeping for one edit-streamed message: its placeholder, and one line per task.

    A task's line is replaced in place as its status changes, so a finished task does not
    leave its "in progress" line behind."""

    __slots__ = ("channel_id", "message_id", "last_edit", "last_text", "head", "tasks")

    def __init__(self, channel_id: str, message_id: str, head: str) -> None:
        self.channel_id = channel_id
        self.message_id = message_id
        self.last_edit = 0.0
        self.last_text = head
        self.head = head
        self.tasks: dict[str, str] = {}

    def text(self, *, final: bool = False) -> str:
        """What the message shows: the placeholder over the task lines while it runs, the task
        lines alone once it stops ("" when there were none). The oldest lines give way when
        they would not fit one message."""
        lines = list(self.tasks.values())
        dropped = False
        while True:
            body = (["…"] if dropped else []) + lines
            text = "\n".join(body if final else [self.head, *body]).strip()
            if not lines or len(text) <= DISCORD_MAX_TEXT:
                return text[:DISCORD_MAX_TEXT]
            lines, dropped = lines[1:], True


class _PendingApproval:
    __slots__ = ("future", "channel_id", "message_id", "request_id")

    def __init__(self, request_id: str, channel_id: str, message_id: str) -> None:
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.channel_id = channel_id
        self.message_id = message_id
        self.request_id = request_id


class DiscordDelivery:
    """Renders + delivers gateway results to Discord. Implements ChannelDelivery."""

    def __init__(self, api: DiscordAPI, owner: Callable[[], str]) -> None:
        self._api = api
        #: Who the owner is NOW, read each time it is needed. The owner can be paired from the
        #: channel's Configure page while this receiver runs; a value kept from the start sent the
        #: approval prompt to nobody until the next restart.
        self._owner = owner
        self._streams: dict[str, _StreamState] = {}
        # keyed by "req:<request_id>" (from the button custom_id) and by
        # "<channel>:<message>" (the prompt the buttons live on).
        self._pending: dict[str, _PendingApproval] = {}
        # user id → opened DM channel id. create_dm is idempotent server-side but
        # costs a request on a bucket shared with sends, so cache the resolution.
        self._dm_channels: dict[str, str] = {}
        # channel id → guild id, populated from inbound events (a DM carries no
        # guild_id, so a channel absent from this map links as @me — the correct DM
        # form). Per-instance, never a class attribute: two transports would
        # otherwise share one map.
        self._channel_guilds: dict[str, str] = {}
        # monotonic clock is injectable so the throttle test needn't sleep.
        self._now = _monotonic

    # ── DM resolution ──
    async def open_dm(self, user_id: str) -> str:
        """Resolve the DM channel id for a user id.

        Unlike Telegram (where the user id IS the chat id), Discord DMs have their
        own channel id that must be opened via ``POST /users/@me/channels``. Returns
        "" on failure so a caller degrades instead of posting to a user id that
        Discord would 404."""
        if not user_id:
            return ""
        cached = self._dm_channels.get(str(user_id))
        if cached:
            return cached
        try:
            channel = await self._api.create_dm(str(user_id))
        except Exception:
            logger.warning("discord: open_dm failed for %s", user_id, exc_info=True)
            return ""
        cid = str(channel.get("id", ""))
        if cid:
            self._dm_channels[str(user_id)] = cid
        return cid

    # ── text / rich ──
    async def deliver_text(
        self, channel: str, text: str, thread_ts: str = "", *,
        unfurl_links: bool | None = None, unfurl_media: bool | None = None,
        reply_broadcast: bool | None = None,
    ) -> str:
        last = ""
        for part in split_message(text):
            msg = await self._api.create_message(channel, part)
            last = str(msg.get("id", "")) or last
        return last

    async def deliver_rich(
        self, channel: str, payload: Any, fallback_text: str, *,
        thread_ts: str = "", unfurl_links: bool = True, unfurl_media: bool = True,
        reply_broadcast: bool = False,
    ) -> str:
        """Deliver a rich payload. Discord's analogue of Block Kit is ``components``.

        A caller that hands through a Discord-shaped ``{"components": [...]}`` gets
        them attached; anything else falls back to the plain text, per the contract."""
        components = None
        if isinstance(payload, dict) and isinstance(payload.get("components"), list):
            components = payload["components"]
        msg = await self._api.create_message(
            channel, fallback_text[:DISCORD_MAX_TEXT], components=components
        )
        return str(msg.get("id", ""))

    async def deliver_cron_result(
        self, channel: str, job_name: str, job_id: str, text: str, thread_ts: str = ""
    ) -> str:
        header = f"**Cron: {job_name}**\n\n"
        parts = split_message(text, DISCORD_MAX_TEXT - len(header))
        last = ""
        for i, part in enumerate(parts or [""]):
            msg = await self._api.create_message(channel, (header + part) if i == 0 else part)
            last = str(msg.get("id", "")) or last
        return last

    async def deliver_notification(
        self, channel: str, title: str, text: str, thread_ts: str = ""
    ) -> str:
        last = ""
        for part in split_message(f"**{title}**\n\n{text}"):
            msg = await self._api.create_message(channel, part)
            last = str(msg.get("id", "")) or last
        return last

    async def deliver_chat_mirror(self, channel: str, text: str, thread_ts: str = "") -> None:
        """Mirror a dashboard reply, rendering a trailing ``[OPTIONS: …]`` as buttons."""
        from personalclaw.sdk.channel import extract_options

        body, options = extract_options(text)
        for part in split_message(body):
            await self._api.create_message(channel, part)
        if options:
            # Discord allows at most 5 buttons per action row; chunk accordingly.
            rows = [
                {
                    "type": COMPONENT_ACTION_ROW,
                    "components": [
                        {
                            "type": COMPONENT_BUTTON,
                            "style": BUTTON_STYLE_SUCCESS,
                            "label": opt[:80],
                            "custom_id": f"opt:{idx}",
                        }
                        for idx, opt in chunk
                    ],
                }
                for chunk in _chunk(list(enumerate(options)), 5)
            ]
            await self._api.create_message(channel, "Options:", components=rows)

    async def deliver_subagent_reply(
        self, channel: str, text: str, thread_ts: str = "", elapsed_secs: float = 0.0
    ) -> None:
        for part in split_message(text):
            await self._api.create_message(channel, part)
        if elapsed_secs:
            await self._api.create_message(channel, f"_took {elapsed_secs:.1f}s_")

    # ── identity resolution ──
    async def resolve_user_name(self, user_id: str) -> str:
        """Display name for a user id via ``GET /users/{id}``.

        Prefers ``global_name`` (Discord's display name) over the handle, and falls
        back to the id so a lookup failure degrades to something printable."""
        try:
            user = await self._api.get_user(str(user_id))
        except Exception:
            return str(user_id)
        return str(user.get("global_name") or user.get("username") or user_id)

    async def resolve_user_profile(self, user_id: str) -> dict:
        try:
            return await self._api.get_user(str(user_id)) or {"id": str(user_id)}
        except Exception:
            return {"id": str(user_id)}

    async def channel_info(self, channel_id: str) -> dict:
        """Channel metadata. Discord channel ``type`` 1 is a 1:1 DM."""
        try:
            channel = await self._api.get_channel(str(channel_id))
        except Exception:
            return {"name": str(channel_id), "is_im": False}
        return {
            "name": str(channel.get("name") or channel_id),
            "is_im": int(channel.get("type", 0) or 0) == 1,
            "guild_id": str(channel.get("guild_id", "") or ""),
        }

    def list_reply_channels(self) -> list[dict]:
        """The channels this delivery can post into for the dashboard picker.

        The tracked-channel allowlist lives in the core trust seam (CE-1 owns it),
        and the SDK exposes only a membership check (:func:`is_tracked_channel`) —
        no enumeration — so the picker offers the DM entry, and a guild reply targets
        a specific tracked channel id core already holds. Deliberately minimal, per
        the ChannelDelivery contract ("may be empty")."""
        return [{"id": "dm", "name": "Direct Message"}]

    def is_tracked_channel(self, channel_id: str) -> bool:
        return is_tracked_channel("discord", channel_id)

    def build_thread_link(self, channel: str, ts: str) -> str:
        """Deep link to a message: ``/channels/<guild|@me>/<channel>/<message>``.

        Discord DOES have a stable link format (unlike Telegram's private chats), so
        this returns a real URL. The guild id is not on this call's signature, so it
        is read from the guild the channel was last seen in (:meth:`note_channel_guild`,
        fed by the transport's inbound path); a DM has no guild and uses the literal
        ``@me``. Returns "" when there is no channel to link to — honest over a URL
        that 404s."""
        if not channel:
            return ""
        guild = self._channel_guilds.get(str(channel), "") or "@me"
        base = f"https://discord.com/channels/{guild}/{channel}"
        return f"{base}/{ts}" if ts else base

    def note_channel_guild(self, channel_id: str, guild_id: str) -> None:
        """Remember which guild a channel belongs to (for :meth:`build_thread_link`)."""
        if channel_id and guild_id:
            self._channel_guilds[str(channel_id)] = str(guild_id)

    # ── attachments ──
    async def upload_attachment(
        self, channel: str, file_path: str, *, filename: str = "", thread_ts: str = "",
        title: str = "", initial_comment: str = "",
    ) -> str:
        """Upload a file. Discord renders images inline from the attachment itself,
        so there is no photo-vs-document split to make (unlike Telegram)."""
        caption = initial_comment or title or ""
        msg = await self._api.upload_file(
            channel, file_path, filename=filename, content=caption[:DISCORD_MAX_TEXT]
        )
        return str(msg.get("id", ""))

    # ── reactions + typing (implemented, therefore declared True) ──
    async def add_reaction(self, channel: str, message_id: str, emoji: str) -> bool:
        """React to a message as the bot. Returns whether it landed."""
        try:
            await self._api.add_reaction(channel, message_id, emoji)
            return True
        except Exception:
            logger.debug("discord: add_reaction failed", exc_info=True)
            return False

    async def show_typing(self, channel: str) -> bool:
        """Show the typing indicator (~10s, or until the next message)."""
        try:
            await self._api.trigger_typing(channel)
            return True
        except Exception:
            logger.debug("discord: trigger_typing failed", exc_info=True)
            return False

    # ── edit-based streaming ──
    async def start_stream(self, channel: str, thread_ts: str = "", initial_text: str = "") -> str:
        head = initial_text or "…"
        msg = await self._api.create_message(channel, head)
        mid = str(msg.get("id", ""))
        if not mid:
            return ""
        st = _StreamState(channel, mid, head)
        st.last_edit = self._now()
        self._streams[f"{channel}:{mid}"] = st
        return mid

    async def append_stream_task(
        self, channel: str, stream_ts: str, task_id: str, title: str, status: str,
    ) -> None:
        """Show a task's progress line in the streamed message, throttled.

        Discord has no task-animation primitive, so a task update is folded into the
        streamed text as a status line, the task's own (a finished task's line replaces
        its "in progress" one), and edited in — at most one edit per
        :data:`_EDIT_MIN_INTERVAL`. The final flush happens in :meth:`stop_stream`,
        so a throttled-away update is never lost."""
        st = self._streams.get(f"{channel}:{stream_ts}")
        if st is None:
            return
        mark = "✅" if status in ("complete", "completed", "done") else "⏳"
        st.tasks[task_id] = f"{mark} {title}".strip()
        await self._maybe_edit(st, force=False)

    async def stop_stream(self, channel: str, stream_ts: str) -> None:
        """Leave the streamed message holding the task lines, or remove it when it only ever
        held its placeholder: the reply is a message of its own, and a "Thinking…" left above
        it reads as a turn that never finished."""
        st = self._streams.pop(f"{channel}:{stream_ts}", None)
        if st is None:
            return
        final = st.text(final=True)
        if not final:
            try:
                await self._api.delete_message(st.channel_id, st.message_id)
            except Exception:
                logger.warning(
                    "discord: the stream placeholder %s in %s could not be removed",
                    st.message_id, st.channel_id, exc_info=True,
                )
            return
        # Always flush the exact final text, throttle be damned.
        await self._edit(st, final, self._now())

    async def _maybe_edit(self, st: _StreamState, *, force: bool) -> None:
        """Show the running stream's text now, unless an edit landed inside the throttle
        window (``force`` ignores it)."""
        text = st.text()
        if text == st.last_text:
            return
        now = self._now()
        if not force and (now - st.last_edit) < _EDIT_MIN_INTERVAL:
            return  # throttled — the text rides until the next edit/flush
        await self._edit(st, text, now)

    async def _edit(self, st: _StreamState, text: str, now: float) -> None:
        try:
            await self._api.edit_message(st.channel_id, st.message_id, text)
        except Exception:
            logger.warning("discord: stream edit failed", exc_info=True)
            return
        st.last_edit = now
        st.last_text = text

    # ── approval via message components ──
    async def request_approval(
        self, event: Any, *, source: str, parent_session_key: str = "",
        sessions: Any = None, on_prompted: Any = None,
    ) -> bool | None:
        """Post an Approve/Deny button row and wait for the owner's press.

        Returns approved/rejected, or None when we can't prompt (no owner/channel) so
        the gateway falls back to the dashboard. ``on_prompted(pending)`` lets core
        race a dashboard prompt against this one — a dashboard click resolves the
        same future."""
        channel_id = ""
        if parent_session_key and sessions is not None:
            try:
                channel_id = sessions.get_channel(parent_session_key) or ""
            except Exception:
                channel_id = ""
        if not channel_id:
            # No linked channel: prompt the owner's DM, which must be OPENED first —
            # a Discord user id is not a postable channel id.
            owner = self._owner()
            channel_id = await self.open_dm(owner) if owner else ""
        if not channel_id:
            return None

        request_id = str(getattr(event, "request_id", ""))
        title = str(getattr(event, "title", ""))
        # Split like a reply, the buttons on the last part: the prompt was cut at 2,000
        # characters, so the owner approved a command whose end they never saw.
        parts = split_message(f"🔐 [{source}] Approve: {title}?")
        msg: dict[str, Any] = {}
        for index, part in enumerate(parts, 1):
            last = index == len(parts)
            msg = await self._api.create_message(
                channel_id, part, components=_approval_components(request_id) if last else None
            )
        message_id = str(msg.get("id", ""))
        pending = _PendingApproval(request_id, channel_id, message_id)
        self._pending[f"{channel_id}:{message_id}"] = pending
        # Index by request_id too so resolve_interaction can find it from custom_id.
        self._pending[f"req:{request_id}"] = pending
        if on_prompted:
            try:
                on_prompted(pending)
            except Exception:
                logger.debug("discord: on_prompted hook failed", exc_info=True)

        try:
            outcome = await asyncio.wait_for(pending.future, timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            outcome = "rejected"
        finally:
            self._pending.pop(f"{channel_id}:{message_id}", None)
            self._pending.pop(f"req:{request_id}", None)

        approved = outcome == "approved"
        status = "✅ Approved" if approved else "🚫 Rejected"
        try:
            # components=[] strips the buttons: a decided request must not leave a
            # clickable Approve behind.
            await self._api.edit_message(
                channel_id, message_id, _answered(title, parts, status), components=[]
            )
        except Exception:
            logger.debug("discord: approval finalize edit failed", exc_info=True)
        return approved

    async def resolve_interaction(self, interaction: dict[str, Any]) -> None:
        """Resolve a pending approval from an ``INTERACTION_CREATE`` button press.

        Acknowledging is NOT optional and NOT conditional on the press being ours:
        Discord shows the pressing user "This interaction failed" if nothing answers
        within three seconds, so the ack happens even for an unknown/stale custom_id."""
        if int(interaction.get("type", 0) or 0) != INTERACTION_TYPE_COMPONENT:
            return
        custom_id = str((interaction.get("data") or {}).get("custom_id", ""))
        action, _, request_id = custom_id.partition(":")
        if action in (_APPROVE, _DENY) and request_id:
            pending = self._pending.get(f"req:{request_id}")
            if pending is not None and not pending.future.done():
                # Only the owner's press answers it. A prompt for a chat linked to a tracked
                # channel is posted there, where everyone in it sees the buttons, and a member
                # must not approve what the owner's agent runs. In a server the presser is
                # `member.user`, in a DM `user`.
                member_user = (interaction.get("member") or {}).get("user") or {}
                presser = str((member_user or interaction.get("user") or {}).get("id", "") or "")
                owner = str(self._owner() or "")
                if owner and presser == owner:
                    pending.future.set_result("approved" if action == _APPROVE else "rejected")
                else:
                    logger.warning("discord: refused an approval press from %s, not the owner", presser)
                    sel().log_api_access(
                        caller=f"discord:{presser or 'unknown'}",
                        operation="discord.approval_press",
                        outcome="denied",
                        source="discord",
                        resources=request_id,
                        error="not the owner",
                    )
        iid = str(interaction.get("id", ""))
        itoken = str(interaction.get("token", ""))
        if iid and itoken:
            try:
                await self._api.create_interaction_response(iid, itoken)
            except Exception:
                logger.debug("discord: interaction ack failed", exc_info=True)


def _approval_components(request_id: str) -> list[dict[str, Any]]:
    """One action row with the Approve (success) / Deny (danger) buttons.

    The request id rides in each ``custom_id`` — that is the only state Discord
    hands back on the press, so it is what re-finds the pending future."""
    return [
        {
            "type": COMPONENT_ACTION_ROW,
            "components": [
                {
                    "type": COMPONENT_BUTTON,
                    "style": BUTTON_STYLE_SUCCESS,
                    "label": "Approve",
                    "custom_id": f"{_APPROVE}:{request_id}",
                },
                {
                    "type": COMPONENT_BUTTON,
                    "style": BUTTON_STYLE_DANGER,
                    "label": "Deny",
                    "custom_id": f"{_DENY}:{request_id}",
                },
            ],
        }
    ]


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _monotonic() -> float:
    import time

    return time.monotonic()


def _answered(title: str, parts: list[str], status: str) -> str:
    """What the prompt's last message says once it is answered, in one message: the prompt with
    its outcome, or, for a prompt split over several, its last part with it, or the outcome
    alone when that would not fit (the parts above keep the rest)."""
    text = f"🔐 {title} — {status}" if len(parts) <= 1 else f"{parts[-1]} — {status}"
    return text if len(text) <= DISCORD_MAX_TEXT else f"🔐 {status}"
