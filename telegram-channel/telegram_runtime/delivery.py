"""TelegramDelivery — the app-side ChannelDelivery the gateway delivers through.

All Telegram rendering (MarkdownV2 conversion, message splitting, throttled
edit-streaming, the inline-keyboard approval prompt + owner-response wait) lives
HERE, so core delivers with plain text + structured intent and never imports
Telegram code. The transport registers an instance onto the gateway + dashboard at
``start_inbound``.

Streaming is edit-based: Telegram has no chunk-append API, so a "stream" is one
message repeatedly edited via ``editMessageText``. Telegram rate-limits edits
hard, so :class:`TelegramDelivery` throttles to at most one edit per
:data:`_EDIT_MIN_INTERVAL` seconds and always flushes the exact final text on
``stop_stream`` — the contract the fake-API tests pin.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from personalclaw.sdk.channel import (
    is_tracked_channel,
    redact_credentials,
    redact_exfiltration_urls,
)

from telegram_runtime.api import TelegramAPI
from telegram_runtime.format import split_message, to_markdown_v2

logger = logging.getLogger(__name__)

# Minimum wall-clock seconds between two edits of the same streamed message. The
# plan sets the floor at 1.1s; Telegram tolerates roughly one edit/second.
_EDIT_MIN_INTERVAL = 1.1
# Approval prompts wait this long for the owner's button press before defaulting
# to rejected (mirrors Slack's 2h ceiling).
_APPROVAL_TIMEOUT = 7200
# callback_data is capped at 64 bytes by Telegram; our tokens are short ids.
_APPROVE = "approve"
_DENY = "deny"


class _StreamState:
    """Bookkeeping for one edit-streamed message."""

    __slots__ = ("chat_id", "message_id", "last_edit", "last_text", "pending_text")

    def __init__(self, chat_id: str, message_id: int) -> None:
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_edit = 0.0
        self.last_text = ""
        self.pending_text = ""


class _PendingApproval:
    __slots__ = ("future", "chat_id", "message_id", "request_id")

    def __init__(self, request_id: str, chat_id: str, message_id: int) -> None:
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.chat_id = chat_id
        self.message_id = message_id
        self.request_id = request_id


class TelegramDelivery:
    """Renders + delivers gateway results to Telegram. Implements ChannelDelivery."""

    def __init__(self, api: TelegramAPI, owner_id: str) -> None:
        self._api = api
        self._owner_id = owner_id
        self._streams: dict[str, _StreamState] = {}
        # keyed by "chat_id:message_id" of the prompt message the buttons live on.
        self._pending: dict[str, _PendingApproval] = {}
        # monotonic clock is injectable so the throttle test needn't sleep.
        self._now = _monotonic

    # ── DM resolution ──
    async def open_dm(self, user_id: str) -> str:
        # A Telegram user's DM chat_id equals their user id; there is no open step.
        return str(user_id)

    # ── text / rich ──
    async def deliver_text(
        self, channel: str, text: str, thread_ts: str = "", *,
        unfurl_links: bool | None = None, unfurl_media: bool | None = None,
        reply_broadcast: bool | None = None,
    ) -> str:
        body_plain, _ = redact_exfiltration_urls(text)
        body_plain, _ = redact_credentials(body_plain)
        body = to_markdown_v2(body_plain)
        last = ""
        for part in split_message(body):
            msg = await self._api.send_message(
                channel, part, parse_mode="MarkdownV2",
                disable_web_page_preview=(unfurl_links is False) or None,
            )
            last = str(msg.get("message_id", "")) or last
        return last

    async def deliver_rich(
        self, channel: str, payload: Any, fallback_text: str, *,
        thread_ts: str = "", unfurl_links: bool = True, unfurl_media: bool = True,
        reply_broadcast: bool = False,
    ) -> str:
        # Telegram has no Block-Kit analogue; render the plain-text fallback. When a
        # caller hands a reply_markup dict through, pass it as an inline keyboard.
        markup = payload if isinstance(payload, dict) and "inline_keyboard" in payload else None
        msg = await self._api.send_message(
            channel, to_markdown_v2(fallback_text), parse_mode="MarkdownV2", reply_markup=markup,
        )
        return str(msg.get("message_id", ""))

    async def deliver_cron_result(
        self, channel: str, job_name: str, job_id: str, text: str, thread_ts: str = ""
    ) -> str:
        redacted, _ = redact_exfiltration_urls(text)
        redacted, _ = redact_credentials(redacted)
        header = to_markdown_v2(f"⏰ Cron: {job_name}\n\n")
        first = True
        last = ""
        for part in split_message(to_markdown_v2(redacted)):
            body = (header + part) if first else part
            first = False
            msg = await self._api.send_message(channel, body, parse_mode="MarkdownV2")
            last = str(msg.get("message_id", "")) or last
        return last

    async def deliver_notification(
        self, channel: str, title: str, text: str, thread_ts: str = ""
    ) -> str:
        body = to_markdown_v2(f"💓 {title}\n\n{text}")
        msg = await self._api.send_message(channel, body, parse_mode="MarkdownV2")
        return str(msg.get("message_id", ""))

    async def deliver_chat_mirror(self, channel: str, text: str, thread_ts: str = "") -> None:
        from personalclaw.sdk.channel import extract_options

        body, _ = redact_exfiltration_urls(text)
        body, _ = redact_credentials(body)
        body, options = extract_options(body)
        for part in split_message(to_markdown_v2(body)):
            await self._api.send_message(channel, part, parse_mode="MarkdownV2")
        if options:
            markup = {
                "inline_keyboard": [[{"text": o[:64], "callback_data": f"opt:{i}"}] for i, o in enumerate(options)]
            }
            await self._api.send_message(
                channel, to_markdown_v2("Options:"), parse_mode="MarkdownV2", reply_markup=markup,
            )

    async def deliver_subagent_reply(
        self, channel: str, text: str, thread_ts: str = "", elapsed_secs: float = 0.0
    ) -> None:
        body, _ = redact_exfiltration_urls(text)
        body, _ = redact_credentials(body)
        for part in split_message(to_markdown_v2(body)):
            await self._api.send_message(channel, part, parse_mode="MarkdownV2")
        if elapsed_secs:
            footer = to_markdown_v2(f"_took {elapsed_secs:.1f}s_")
            await self._api.send_message(channel, footer, parse_mode="MarkdownV2")

    # ── identity resolution (Telegram gives no cheap lookup; be honest) ──
    async def resolve_user_name(self, user_id: str) -> str:
        return str(user_id)

    async def resolve_user_profile(self, user_id: str) -> dict:
        return {"id": str(user_id)}

    async def channel_info(self, channel_id: str) -> dict:
        return {"name": str(channel_id), "is_im": False}

    def list_reply_channels(self) -> list[dict]:
        """The channels this delivery can post into for the dashboard picker.

        The tracked-group allowlist lives in the core trust seam (CE-1 owns it; this
        app keeps none of its own), and the SDK exposes only a membership check
        (:func:`is_tracked_channel`) — no enumeration — so the picker offers the DM
        entry, and a group reply targets a specific tracked chat id core already
        holds. Deliberately minimal, per the ChannelDelivery contract ("may be empty")."""
        return [{"id": "dm", "name": "Direct Message"}]

    def is_tracked_channel(self, channel_id: str) -> bool:
        return is_tracked_channel("telegram", channel_id)

    def build_thread_link(self, channel: str, ts: str) -> str:
        """Deep link to a message. Telegram links only resolve for public @username
        chats (``https://t.me/<name>/<id>``); a numeric private chat id has no public
        URL, so return "" honestly rather than a link that 404s."""
        if not channel:
            return ""
        if channel.startswith("@"):
            base = f"https://t.me/{channel[1:]}"
            return f"{base}/{ts}" if ts else base
        return ""

    # ── attachments ──
    async def upload_attachment(
        self, channel: str, file_path: str, *, filename: str = "", thread_ts: str = "",
        title: str = "", initial_comment: str = "",
    ) -> str:
        caption = initial_comment or title or None
        lower = file_path.lower()
        if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            msg = await self._api.send_photo(channel, file_path, caption=caption)
        else:
            msg = await self._api.send_document(channel, file_path, caption=caption)
        return str(msg.get("message_id", ""))

    # ── edit-based streaming ──
    async def start_stream(self, channel: str, thread_ts: str = "", initial_text: str = "") -> str:
        text = to_markdown_v2(initial_text or "…")
        msg = await self._api.send_message(channel, text, parse_mode="MarkdownV2")
        mid = int(msg.get("message_id", 0) or 0)
        if not mid:
            return ""
        key = f"{channel}:{mid}"
        st = _StreamState(channel, mid)
        st.last_edit = self._now()
        st.last_text = initial_text or "…"
        self._streams[key] = st
        return str(mid)

    async def append_stream_task(
        self, channel: str, stream_ts: str, task_id: str, title: str, status: str,
    ) -> None:
        """Append a progress line to the streamed message, throttled.

        Telegram has no task-animation primitive, so a task update is folded into
        the streamed text as a status line and edited in — at most one edit per
        :data:`_EDIT_MIN_INTERVAL`. The final flush happens in :meth:`stop_stream`,
        so a throttled-away update is never lost."""
        key = f"{channel}:{stream_ts}"
        st = self._streams.get(key)
        if st is None:
            return
        icon = "✅" if status in ("complete", "completed", "done") else "⏳"
        st.pending_text = f"{st.last_text}\n{icon} {title}".strip()
        await self._maybe_edit(st, force=False)

    async def stop_stream(self, channel: str, stream_ts: str) -> None:
        key = f"{channel}:{stream_ts}"
        st = self._streams.pop(key, None)
        if st is None:
            return
        # Always flush the exact final text, throttle be damned.
        await self._maybe_edit(st, force=True)

    async def _maybe_edit(self, st: _StreamState, *, force: bool) -> None:
        text = st.pending_text or st.last_text
        if text == st.last_text and not force:
            return
        now = self._now()
        if not force and (now - st.last_edit) < _EDIT_MIN_INTERVAL:
            return  # throttled — the pending text rides until the next edit/flush
        try:
            await self._api.edit_message_text(
                st.chat_id, st.message_id, to_markdown_v2(text), parse_mode="MarkdownV2",
            )
            st.last_edit = now
            st.last_text = text
            st.pending_text = ""
        except Exception:
            logger.debug("telegram: stream edit failed", exc_info=True)

    # ── approval via inline keyboard ──
    async def request_approval(
        self, event: Any, *, source: str, parent_session_key: str = "",
        sessions: Any = None, on_prompted: Any = None,
    ) -> bool | None:
        """Post an Approve/Deny inline keyboard and wait for the owner's press.

        Returns approved/rejected, or None when we can't prompt (no owner/chat) so
        the gateway falls back to the dashboard. ``on_prompted(pending)`` lets core
        race a dashboard prompt against this one — a dashboard click resolves the
        same future."""
        # Resolve the chat to prompt in: the session's linked chat if there is one,
        # else the owner's DM (their user id == their DM chat id).
        chat_id = ""
        if parent_session_key and sessions is not None:
            try:
                chat_id = sessions.get_channel(parent_session_key) or ""
            except Exception:
                chat_id = ""
        if not chat_id:
            chat_id = self._owner_id
        if not chat_id:
            return None

        request_id = str(getattr(event, "request_id", ""))
        title, _ = redact_exfiltration_urls(str(getattr(event, "title", "")))
        title, _ = redact_credentials(title)
        markup = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"{_APPROVE}:{request_id}"},
                {"text": "🚫 Deny", "callback_data": f"{_DENY}:{request_id}"},
            ]]
        }
        prompt = to_markdown_v2(f"🔐 [{source}] Approve: {title}?")
        msg = await self._api.send_message(chat_id, prompt, parse_mode="MarkdownV2", reply_markup=markup)
        mid = int(msg.get("message_id", 0) or 0)
        key = f"{chat_id}:{mid}"
        pending = _PendingApproval(request_id, chat_id, mid)
        self._pending[key] = pending
        # Index by request_id too so resolve_callback can find it from callback_data.
        self._pending[f"req:{request_id}"] = pending
        if on_prompted:
            try:
                on_prompted(pending)
            except Exception:
                logger.debug("telegram: on_prompted hook failed", exc_info=True)

        try:
            outcome = await asyncio.wait_for(pending.future, timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            outcome = "rejected"
        finally:
            self._pending.pop(key, None)
            self._pending.pop(f"req:{request_id}", None)

        status = "✅ Approved" if outcome == "approved" else "🚫 Rejected"
        try:
            await self._api.edit_message_text(
                chat_id, mid, to_markdown_v2(f"🔐 {title} — {status}"), parse_mode="MarkdownV2",
            )
        except Exception:
            logger.debug("telegram: approval finalize edit failed", exc_info=True)
        return outcome == "approved"

    async def resolve_callback(self, cq: dict[str, Any]) -> None:
        """Resolve a pending approval from a ``callback_query`` (button press)."""
        data = cq.get("data", "") or ""
        cq_id = cq.get("id", "")
        action, _, request_id = data.partition(":")
        if action in (_APPROVE, _DENY) and request_id:
            pending = self._pending.get(f"req:{request_id}")
            if pending is not None and not pending.future.done():
                pending.future.set_result("approved" if action == _APPROVE else "rejected")
        # Acknowledge so Telegram stops the button's spinner.
        if cq_id:
            try:
                await self._api.answer_callback_query(cq_id, text="Recorded")
            except Exception:
                logger.debug("telegram: answerCallbackQuery failed", exc_info=True)


def _monotonic() -> float:
    import time

    return time.monotonic()
