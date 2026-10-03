"""SlackDelivery — the app-side ChannelDelivery the gateway delivers through.

All Slack rendering (mrkdwn conversion, Block Kit ack buttons, message splitting,
timing footers, the interactive approval prompt + owner-response wait) lives HERE,
so core delivers with plain text + structured intent and never imports Slack code.
The Slack transport registers an instance onto the orchestrator at start_inbound.

Core masks every text it hands this handle, keys and exfiltration URLs included, before
any method here is called, so nothing here masks it again. The Slack handler's own
replies, which do not come through core, keep their own masking.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from personalclaw.sdk.channel import approval_brief_for

from slack_runtime.client import RealSlackClient
from slack_runtime.format import (
    SLACK_BLOCK_SECTION_LIMIT,
    build_cron_ack_block,
    escape_mrkdwn,
    model_blocks,
    split_message,
    to_slack_mrkdwn,
)

logger = logging.getLogger(__name__)

# deliver_cron_result puts parts[0] into a Block Kit `section.text`, so every part must
# respect the 3000-char section bound — not the 3900 plain-message bound. This was 3800,
# which let a cron result between 3001 and 3800 chars build a section Slack rejects
# (invalid_blocks); the overflow parts go out as plain messages, where 3000 is also fine.
_CRON_MSG_LIMIT = SLACK_BLOCK_SECTION_LIMIT


# How a progress line that did not end complete reads in Slack's task list, which knows only
# pending, in progress, complete and error: an error, with the ending in its title
# (``ChannelDelivery.append_stream_task``).
_TASK_ENDINGS = {
    "failed": "failed",
    "rejected": "rejected",
    "expired": "no answer in time, not run",
    "cancelled": "cancelled, not run",
}


class SlackDelivery:
    """Renders + delivers gateway results to Slack. Implements ChannelDelivery."""

    def __init__(self, client: RealSlackClient, owner: Callable[[], str]) -> None:
        self._client = client
        #: Who the owner is NOW, read each time it is needed. A fresh install claims its owner
        #: when the first person messages the bot; a value kept from the start left that owner
        #: unknown here until a restart, and every approval core asked skipped Slack.
        self._owner = owner

    # ── raw client passthrough (used by the approval flow + session routing) ──
    @property
    def client(self) -> RealSlackClient:
        return self._client

    async def open_dm(self, user_id: str, max_attempts: int = 3) -> str:
        """Resolve a DM channel, retrying transient Slack API errors."""
        from slack_sdk.errors import SlackApiError

        for attempt in range(1, max_attempts + 1):
            try:
                return await self._client.open_dm(user_id) or ""
            except (SlackApiError, ConnectionError, TimeoutError) as exc:
                retryable = (
                    not isinstance(exc, SlackApiError)
                    or exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if not retryable or attempt >= max_attempts:
                    raise
                logger.warning(
                    "open_dm attempt %d/%d failed, retrying in %ds", attempt, max_attempts, attempt,
                    exc_info=True,
                )
                await asyncio.sleep(attempt)
        return ""

    async def deliver_text(
        self, channel: str, text: str, thread_ts: str = "", *,
        unfurl_links: bool | None = None, unfurl_media: bool | None = None,
        reply_broadcast: bool | None = None,
    ) -> str:
        parts = split_message(to_slack_mrkdwn(text))
        last = ""
        for i, part in enumerate(parts):
            # Link/broadcast hints apply to the first message only; continuation
            # parts thread under it plainly.
            if i == 0:
                last = await self._client.post_message(
                    channel, part, thread_ts or None,
                    unfurl_links=unfurl_links, unfurl_media=unfurl_media,
                    reply_broadcast=reply_broadcast,
                ) or last
            else:
                last = await self._client.post_message(channel, part, thread_ts or None) or last
        return last

    async def deliver_rich(
        self, channel: str, payload: Any, fallback_text: str, *,
        thread_ts: str = "", unfurl_links: bool = True, unfurl_media: bool = True,
        reply_broadcast: bool = False,
    ) -> str:
        # payload is Slack Block Kit, masked by core like every text it hands a channel. A model
        # wrote it, so its text is shown as written and its mentions notify no one.
        return await self._client.post_blocks(
            channel, model_blocks(payload), to_slack_mrkdwn(fallback_text),
            thread_ts=thread_ts or None,
            unfurl_links=unfurl_links, unfurl_media=unfurl_media,
            reply_broadcast=reply_broadcast,
        ) or ""

    async def deliver_cron_result(
        self, channel: str, job_name: str, job_id: str, text: str, thread_ts: str = ""
    ) -> str:
        post_text = f"⏰ *Cron: {escape_mrkdwn(job_name)}*\n\n{to_slack_mrkdwn(text)}"
        parts = split_message(post_text, limit=_CRON_MSG_LIMIT)
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": parts[0]}},
        ] + build_cron_ack_block(job_id)
        parent_ts = await self._client.post_blocks(channel, blocks, parts[0], thread_ts or None)
        thread_root = thread_ts or parent_ts
        for part in parts[1:]:
            await self._client.post_message(channel, part, thread_root)
        return parent_ts or ""

    async def deliver_notification(
        self, channel: str, title: str, text: str, thread_ts: str = ""
    ) -> str:
        post = f"💓 *{escape_mrkdwn(title)}*\n\n{to_slack_mrkdwn(text)}"
        return await self._client.post_message(channel, post, thread_ts or None) or ""

    async def deliver_chat_mirror(
        self, channel: str, text: str, thread_ts: str = ""
    ) -> None:
        from slack_runtime.format import build_options_blocks
        from personalclaw.sdk.channel import extract_options

        body, options = extract_options(text)
        for part in split_message(to_slack_mrkdwn(body)):
            await self._client.post_message(channel, part, thread_ts or None)
        if options:
            await self._client.post_blocks(
                channel, build_options_blocks(options), "Options", thread_ts or None
            )

    async def deliver_subagent_reply(
        self, channel: str, text: str, thread_ts: str = "", elapsed_secs: float = 0.0
    ) -> None:
        for part in split_message(to_slack_mrkdwn(text)):
            await self._client.post_message(channel, part, thread_ts or None)
        try:
            from slack_runtime.handler import build_timing_footer

            footer_blocks, footer_text = build_timing_footer(elapsed_secs, self._client)
            await self._client.post_blocks(channel, footer_blocks, footer_text, thread_ts or None)
        except Exception:
            logger.debug("Failed to post subagent timing footer", exc_info=True)

    # ── Identity resolution ──
    async def resolve_user_name(self, user_id: str) -> str:
        try:
            info = await self._client.get_user_info(user_id) or {}
            return info.get("real_name") or info.get("name") or user_id
        except Exception:
            logger.debug("resolve_user_name failed for %s", user_id, exc_info=True)
            return user_id

    async def resolve_user_profile(self, user_id: str) -> dict:
        try:
            return await self._client.get_user_profile(user_id) or {}
        except Exception:
            logger.debug("resolve_user_profile failed for %s", user_id, exc_info=True)
            return {}

    async def channel_info(self, channel_id: str) -> dict:
        try:
            resp = await self._client._web.conversations_info(channel=channel_id)
            ch = resp.get("channel", {}) if isinstance(resp, dict) else {}
            return {"name": ch.get("name", ""), "is_im": bool(ch.get("is_im"))}
        except Exception:
            logger.debug("channel_info failed for %s", channel_id, exc_info=True)
            return {}

    def list_reply_channels(self) -> list[dict]:
        """Channels the bot can reply in — DM + tracked + active per-channel configs,
        from the app's OWN SlackSettings (core holds no Slack config)."""
        from slack_runtime.settings import get_settings

        s = get_settings()
        channels: list[dict] = [{"id": "dm", "name": "Direct Message"}]
        seen: set[str] = set()
        for tc in s.tracking_channels:
            cid = tc.get("channel_id", "")
            if cid and cid not in seen:
                channels.append({"id": cid, "name": tc.get("name", cid)})
                seen.add(cid)
        for cid, cc in s.channels.items():
            if cid not in seen and cc.activation in ("always", "mention", "observe"):
                channels.append({"id": cid, "name": cid})
                seen.add(cid)
        return channels

    def is_tracked_channel(self, channel_id: str) -> bool:
        from slack_runtime.settings import get_settings

        return channel_id in {
            c.get("channel_id") for c in get_settings().tracking_channels if c.get("channel_id")
        }

    def build_thread_link(self, channel: str, ts: str) -> str:
        """Slack deep link to a message (jump-to-source for notifications).

        The slack.com URL format is a Slack vendor concern, so it lives here —
        core asks the ChannelDelivery seam for the link and stays provider-blind.
        """
        if not channel:
            return ""
        if ts:
            return f"https://slack.com/app_redirect?channel={channel}&message_ts={ts}"
        return f"https://slack.com/app_redirect?channel={channel}"

    # ── Attachment + streaming primitives ──
    async def upload_attachment(
        self, channel: str, file_path: str, *, filename: str = "", thread_ts: str = "",
        title: str = "", initial_comment: str = "",
    ) -> str:
        # RealSlackClient.upload_file(channel, thread_ts, file, filename, title) → None.
        await self._client.upload_file(channel, thread_ts, file_path, filename or "", title or filename or "")
        return ""

    async def start_stream(self, channel: str, thread_ts: str = "", initial_text: str = "") -> str:
        return await self._client.start_stream(channel, thread_ts, initial_text=initial_text) or ""

    async def append_stream_task(
        self, channel: str, stream_ts: str, task_id: str, title: str, status: str,
    ) -> None:
        """Show a call's progress in the stream's task list, and how it ended.

        A call that ran and succeeded is complete. Any other ending is an error in Slack's list,
        whose red mark cannot say which, so the title says it: a call you rejected must not read
        as done. A status this app does not know is shown by its name, and not as done."""
        if status in ("in_progress", "complete"):
            await self._client.append_task(channel, stream_ts, task_id, title, status)
            return
        ending = _TASK_ENDINGS.get(status)
        await self._client.append_task(
            channel,
            stream_ts,
            task_id,
            f"{title} ({ending or status})",
            "error" if ending else "pending",
        )

    async def stop_stream(self, channel: str, stream_ts: str) -> None:
        await self._client.stop_stream(channel, stream_ts)

    async def request_approval(
        self, event: Any, *, source: str, parent_session_key: str = "",
        sessions: Any = None, on_prompted: Any = None,
    ) -> bool | None:
        """Post the Slack approval prompt, its buttons the answers the brief offers, and wait for
        the approval to end.

        Returns whether it ended approved, or None when Slack can't prompt (caller falls
        back to the dashboard), or there is no tool to show. A brief with no answers this prompt
        can offer still prompts, with no buttons and a line saying to answer it in PersonalClaw
        (``handler._NO_ANSWERS``), and it ends as any prompt does. A press resolves the pending
        record with the pressed answer's key. ``on_prompted(pending)`` lets the caller race a
        dashboard prompt against the Slack one: core resolves ``pending.future`` with how the
        approval ended wherever it ended, so the wait keeps no timer of its own. Once it ends, the
        prompt says how and loses its buttons (:func:`~slack_runtime.handler.close_prompt`), and
        so it does when this wait is cancelled."""
        import re

        from slack_runtime.handler import (
            _close_prompt_later,
            _log_no_answers,
            _offered,
            _pending_approvals,
            _PendingApproval,
            _post_approval,
            close_prompt,
        )

        owner = self._owner()
        if not owner:
            return None
        brief = approval_brief_for(event)
        if brief is None:
            return None
        answers = _offered(brief)
        if not answers:
            # Returning None here left the owner a bare link with no reason given, and the log
            # said nothing: the prompt says why it has no buttons, and where to answer instead.
            _log_no_answers()
        request_id = str(event.request_id)
        thread_ts: str | None = None
        channel: str | None = None
        if parent_session_key and sessions:
            channel = sessions.get_channel(parent_session_key)
            thread_ts = sessions.get_thread(parent_session_key)
            if not thread_ts and channel and re.fullmatch(r"\d+\.\d+", parent_session_key):
                thread_ts = parent_session_key
        if not channel:
            channel = await self._client.open_dm(owner)
            thread_ts = None
        if not channel:
            return None

        # What will run, as the dashboard's card shows it, over as many messages as it takes;
        # the buttons are on the last one, whose ts this is, and they are the answers core's
        # brief offers for this call (`_approval_messages`).
        approval_ts = await _post_approval(self._client, channel, thread_ts, event, source=source)

        pending = _PendingApproval(
            provider=None, request_id=request_id, session_key=parent_session_key,  # type: ignore[arg-type]
            answers=answers,
        )
        key = f"{channel}:{approval_ts}"
        _pending_approvals[key] = pending
        if on_prompted:
            on_prompted(pending)

        try:
            outcome = await pending.future
        except asyncio.CancelledError:
            _close_prompt_later(
                self._client, channel, approval_ts, event, outcome="cancelled", source=source
            )
            raise
        finally:
            _pending_approvals.pop(key, None)

        pressed = pending.answer(outcome)
        ending = pressed["ends"] if pressed else outcome
        await close_prompt(
            self._client, channel, approval_ts, event, outcome=ending,
            kept=(pressed or {}).get("promise", ""), source=source,
        )
        return ending == "approved"
