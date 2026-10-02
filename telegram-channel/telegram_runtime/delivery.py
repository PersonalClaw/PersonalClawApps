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
``stop_stream`` — the contract the fake-API tests pin. The stream's placeholder
("Thinking…") is gone when it stops: the message is left holding the task lines
alone, or deleted when there were none, because the reply is a message of its own
and a placeholder left behind reads as a turn that never finished. An edit Telegram
refuses as "message is not modified" is the text already being there, not a failure.

Every text that can outgrow one message goes out through :func:`send_parts`, which
splits it into parts Telegram accepts and never lets one go missing quietly. An
approval prompt does too, its buttons on the last part. The prompt shows what will run,
as the dashboard's approval card does (:func:`_approval_text`): the tool, its arguments, the
purpose and what the call can touch, from core's brief. Its buttons are the answers the brief
offers, the card's own (:func:`_approval_markup`): Allow once and Deny, and Allow for this chat
where core offers it, which the prompt explains before it is pressed.

Core masks every text it hands this handle, keys and exfiltration URLs included, before
any method here is called, so nothing here masks it again.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from typing import Any, Callable

from personalclaw.sdk.channel import approval_brief_for, is_tracked_channel, sel

from telegram_runtime.api import TelegramAPI, TelegramAPIError
from telegram_runtime.format import (
    TELEGRAM_MAX_TEXT,
    escape_markdown_v2,
    render_parts,
    to_markdown_v2,
    utf16_len,
)

logger = logging.getLogger(__name__)

#: Telegram's code for a request it will not take as sent: for a MarkdownV2 text, the
#: formatting it cannot parse.
_BAD_REQUEST = 400

#: Three or more backticks: a run that would end a code block.
_FENCE_RUN = re.compile(r"`{3,}")


async def send_parts(
    api: TelegramAPI,
    chat_id: int | str,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    disable_web_page_preview: bool | None = None,
    reply_to_message_id: int | None = None,
) -> str:
    """Send *text* as as many messages as it takes. Returns the last message id ("" for none).

    Each part (:func:`~telegram_runtime.format.render_parts`) goes out as MarkdownV2. One the
    Bot API refuses as a bad request is sent again as its plain source text, so the reader
    loses that part's formatting rather than the part. Any other refusal, or a plain re-send
    that is refused too, is logged naming the part and raised: a reply is never cut short
    without a trace. A keyboard rides the last part, where the reader ends up; a reply-to
    anchors the first."""
    parts = render_parts(text)
    last = ""
    for index, part in enumerate(parts, 1):
        options: dict[str, Any] = {
            "reply_markup": reply_markup if index == len(parts) else None,
            "disable_web_page_preview": disable_web_page_preview,
            "reply_to_message_id": reply_to_message_id if index == 1 else None,
        }
        try:
            msg = await api.send_message(
                chat_id, part.markdown_v2, parse_mode="MarkdownV2", **options
            )
        except TelegramAPIError as exc:
            if exc.error_code != _BAD_REQUEST:
                logger.warning(
                    "telegram: part %d of %d to %s was not delivered: %s",
                    index, len(parts), chat_id, exc.description,
                )
                raise
            logger.warning(
                "telegram: part %d of %d to %s was refused as MarkdownV2 (%s); sending it as "
                "plain text", index, len(parts), chat_id, exc.description,
            )
            try:
                msg = await api.send_message(chat_id, part.plain, **options)
            except Exception as plain_exc:
                logger.warning(
                    "telegram: part %d of %d to %s was not delivered, as plain text either: %s",
                    index, len(parts), chat_id, plain_exc,
                )
                raise
        except Exception as exc:
            logger.warning(
                "telegram: part %d of %d to %s was not delivered: %s",
                index, len(parts), chat_id, exc,
            )
            raise
        last = str(msg.get("message_id", "")) or last
    return last

# Minimum wall-clock seconds between two edits of the same streamed message. The
# plan sets the floor at 1.1s; Telegram tolerates roughly one edit/second.
_EDIT_MIN_INTERVAL = 1.1
# How many ended approvals are remembered, so a press on one is answered with how it ended.
_ENDED_KEPT = 256

# The line a prompt shows once its approval has ended, for each way core or a press ends one
# (``ChannelDelivery.request_approval``). Expired and cancelled are not a Deny: nobody decided.
_OUTCOME_LINES = {
    "approved": "✅ Approved",
    "rejected": "🚫 Rejected",
    "expired": "⌛ Nobody answered in time, so it did not run",
    "cancelled": "⏹️ Cancelled: the work that asked for it stopped first, so it did not run",
}

# What a press on a prompt whose approval has ended is told, for each ending.
_LATE_ANSWERS = {
    "approved": "Already approved. This press changes nothing.",
    "rejected": "Already rejected. This press changes nothing.",
    "expired": "Nobody answered in time, so it did not run. This press changes nothing.",
    "cancelled": (
        "Cancelled: the work that asked for it stopped first, so it did not run. "
        "This press changes nothing."
    ),
}
# ...and when this process never saw it end (the prompt is older than the gateway's last start).
_NO_LONGER_WAITING = "This approval is no longer waiting. This press changes nothing."
# A press naming no answer the prompt offers.
_NOT_OFFERED = "That is not an answer this approval offers. This press changes nothing."

# A progress line for each status core gives a call (``ChannelDelivery.append_stream_task``): its
# mark, and the words for an ending other than done. A call that did not run never reads done.
_TASK_LINES = {
    "in_progress": ("⏳", ""),
    "complete": ("✅", ""),
    "failed": ("❌", "failed"),
    "rejected": ("🚫", "rejected"),
    "expired": ("⌛", "no answer in time, not run"),
    "cancelled": ("⏹️", "cancelled, not run"),
}


def _task_line(title: str, status: str) -> str:
    """One call's progress line: its mark, its title, and how it ended when not done. A status
    this app does not know is shown by its name, and not as done."""
    mark, words = _TASK_LINES.get(status, ("•", status))
    line = f"{mark} {title}".strip()
    return f"{line} ({words})" if words else line
# An answer's callback_data: this prefix, the answer's place among those the prompt offers, then
# the request id ("a1:<id>"). Telegram caps callback_data at 64 bytes, so an answer is named by
# its place rather than its words; the prompt's pending record says which answer that is.
_ANSWER = "a"
# Each answer's mark: how it ends the approval.
_ANSWER_MARKS = {"approved": "✅", "rejected": "🚫"}


class _StreamState:
    """Bookkeeping for one edit-streamed message: its placeholder, and one line per task.

    A task's line is replaced in place as its status changes, so a finished task does not
    leave its "in progress" line behind. The text is plain: a title is a tool's name or the
    purpose given for the call, and an underscore, star or backtick in it is part of it."""

    __slots__ = ("chat_id", "message_id", "last_edit", "last_text", "head", "tasks")

    def __init__(self, chat_id: str, message_id: int, head: str) -> None:
        self.chat_id = chat_id
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
            if not lines or utf16_len(escape_markdown_v2(text)) <= TELEGRAM_MAX_TEXT:
                return text
            lines, dropped = lines[1:], True


def _not_modified(exc: TelegramAPIError) -> bool:
    """Telegram's answer to an edit that changes nothing: the text is already there."""
    return exc.error_code == _BAD_REQUEST and "message is not modified" in exc.description


class _PendingApproval:
    __slots__ = ("future", "chat_id", "message_id", "request_id", "answers")

    def __init__(
        self, request_id: str, chat_id: str, message_id: int, answers: list[dict[str, str]]
    ) -> None:
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.chat_id = chat_id
        self.message_id = message_id
        self.request_id = request_id
        #: The answers the prompt offers, in its buttons' order (core's brief).
        self.answers = answers

    def answer(self, key: str) -> dict[str, str] | None:
        """The offered answer whose key *key* is, or None."""
        return next((a for a in self.answers if a.get("key") == key), None)


class TelegramDelivery:
    """Renders + delivers gateway results to Telegram. Implements ChannelDelivery."""

    def __init__(self, api: TelegramAPI, owner: Callable[[], str]) -> None:
        self._api = api
        #: Who the owner is NOW, read each time it is needed. The owner can be paired from the
        #: channel's Configure page while this receiver runs; a value kept from the start sent the
        #: approval prompt to nobody until the next restart.
        self._owner = owner
        self._streams: dict[str, _StreamState] = {}
        # keyed by "chat_id:message_id" of the prompt message the buttons live on.
        self._pending: dict[str, _PendingApproval] = {}
        # How each ended approval ended, by its request id, for a press that comes after.
        self._ended: OrderedDict[str, str] = OrderedDict()
        # Background closes of prompts whose wait was cancelled, kept until they are sent.
        self._closing: set[asyncio.Task[None]] = set()
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
        return await send_parts(
            self._api, channel, text, disable_web_page_preview=(unfurl_links is False) or None,
        )

    async def deliver_rich(
        self, channel: str, payload: Any, fallback_text: str, *,
        thread_ts: str = "", unfurl_links: bool = True, unfurl_media: bool = True,
        reply_broadcast: bool = False,
    ) -> str:
        # Telegram has no Block-Kit analogue; render the plain-text fallback. When a
        # caller hands a reply_markup dict through, pass it as an inline keyboard.
        markup = payload if isinstance(payload, dict) and "inline_keyboard" in payload else None
        return await send_parts(self._api, channel, fallback_text, reply_markup=markup)

    async def deliver_cron_result(
        self, channel: str, job_name: str, job_id: str, text: str, thread_ts: str = ""
    ) -> str:
        return await send_parts(self._api, channel, f"⏰ Cron: {job_name}\n\n{text}")

    async def deliver_notification(
        self, channel: str, title: str, text: str, thread_ts: str = ""
    ) -> str:
        return await send_parts(self._api, channel, f"💓 {title}\n\n{text}")

    async def deliver_chat_mirror(self, channel: str, text: str, thread_ts: str = "") -> None:
        from personalclaw.sdk.channel import extract_options

        body, options = extract_options(text)
        await send_parts(self._api, channel, body)
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
        await send_parts(self._api, channel, text)
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
        head = initial_text or "…"
        msg = await self._api.send_message(
            channel, escape_markdown_v2(head), parse_mode="MarkdownV2"
        )
        mid = int(msg.get("message_id", 0) or 0)
        if not mid:
            return ""
        key = f"{channel}:{mid}"
        st = _StreamState(channel, mid, head)
        st.last_edit = self._now()
        self._streams[key] = st
        return str(mid)

    async def append_stream_task(
        self, channel: str, stream_ts: str, task_id: str, title: str, status: str,
    ) -> None:
        """Show a task's progress line in the streamed message, throttled.

        Telegram has no task-animation primitive, so a task update is folded into
        the streamed text as a status line, the task's own (a finished task's line
        replaces its "in progress" one), and edited in — at most one edit per
        :data:`_EDIT_MIN_INTERVAL`. The final flush happens in :meth:`stop_stream`,
        so a throttled-away update is never lost."""
        key = f"{channel}:{stream_ts}"
        st = self._streams.get(key)
        if st is None:
            return
        st.tasks[task_id] = _task_line(title, status)
        await self._maybe_edit(st, force=False)

    async def stop_stream(self, channel: str, stream_ts: str) -> None:
        """Leave the streamed message holding the task lines, or remove it when it only ever
        held its placeholder: the reply is a message of its own, and a "Thinking…" left above
        it reads as a turn that never finished."""
        key = f"{channel}:{stream_ts}"
        st = self._streams.pop(key, None)
        if st is None:
            return
        final = st.text(final=True)
        if not final:
            try:
                await self._api.delete_message(st.chat_id, st.message_id)
            except Exception:
                logger.warning(
                    "telegram: the stream placeholder %s in %s could not be removed",
                    st.message_id, st.chat_id, exc_info=True,
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
            await self._api.edit_message_text(
                st.chat_id, st.message_id, escape_markdown_v2(text), parse_mode="MarkdownV2",
            )
        except TelegramAPIError as exc:
            if not _not_modified(exc):
                logger.warning("telegram: stream edit failed: %s", exc.description)
                return
        except Exception:
            logger.warning("telegram: stream edit failed", exc_info=True)
            return
        st.last_edit = now
        st.last_text = text

    # ── approval via inline keyboard ──
    async def request_approval(
        self, event: Any, *, source: str, parent_session_key: str = "",
        sessions: Any = None, on_prompted: Any = None,
    ) -> bool | None:
        """Post an inline keyboard of the answers the brief offers and wait for the approval to
        end.

        Returns whether it ended approved, or None when we can't prompt (no owner/chat, or a
        brief that offers nothing) so the gateway falls back to the dashboard. A press resolves
        the pending record with the pressed answer's key. ``on_prompted(pending)`` lets core
        race a dashboard prompt against this one: core resolves the same future with how
        the approval ended wherever it ended, so the wait keeps no timer of its own. Once
        it ends, the prompt says how and loses its buttons (:meth:`_close`), and so it does
        when this wait is cancelled."""
        # Resolve the chat to prompt in: the session's linked chat if there is one,
        # else the owner's DM (their user id == their DM chat id).
        chat_id = ""
        if parent_session_key and sessions is not None:
            try:
                chat_id = sessions.get_channel(parent_session_key) or ""
            except Exception:
                chat_id = ""
        if not chat_id:
            chat_id = self._owner()
        if not chat_id:
            return None

        brief = approval_brief_for(event) or {}
        answers = list(brief.get("answers") or [])
        if not answers:
            return None
        request_id = str(getattr(event, "request_id", ""))
        # What will run, as the dashboard's card shows it. Split like a reply, the buttons on
        # the last part: one message of it all was refused as too long, and the owner was
        # never asked.
        prompt = _approval_text(brief, source)
        parts = render_parts(prompt)
        mid = int(
            await send_parts(
                self._api, chat_id, prompt, reply_markup=_approval_markup(answers, request_id)
            )
            or 0
        )
        key = f"{chat_id}:{mid}"
        pending = _PendingApproval(request_id, chat_id, mid, answers)
        self._pending[key] = pending
        # Index by request_id too so resolve_callback can find it from callback_data.
        self._pending[f"req:{request_id}"] = pending
        if on_prompted:
            try:
                on_prompted(pending)
            except Exception:
                logger.debug("telegram: on_prompted hook failed", exc_info=True)

        try:
            outcome = await pending.future
        except asyncio.CancelledError:
            self._close_later(chat_id, mid, parts, request_id, "cancelled")
            raise
        finally:
            self._pending.pop(key, None)
            self._pending.pop(f"req:{request_id}", None)

        pressed = pending.answer(outcome)
        await self._close(chat_id, mid, parts, request_id, outcome, pressed)
        return (pressed["ends"] if pressed else outcome) == "approved"

    async def _close(
        self,
        chat_id: str,
        mid: int,
        parts: list,
        request_id: str,
        outcome: str,
        pressed: dict[str, str] | None = None,
    ) -> None:
        """Show how the approval ended under what the prompt asked, and take its buttons off (an
        edit that sends no keyboard removes it). *pressed* is the answer the owner pressed here,
        whose ending it is and whose promise the prompt then says it kept. A press after this is
        answered with how it ended."""
        ending = pressed["ends"] if pressed else outcome
        self._ended.pop(request_id, None)
        self._ended[request_id] = ending
        while len(self._ended) > _ENDED_KEPT:
            self._ended.popitem(last=False)
        line = _OUTCOME_LINES.get(ending) or f"Ended: {ending}"
        if pressed and pressed.get("promise"):
            line = f"{line}. {pressed['promise']}"
        try:
            await self._api.edit_message_text(
                chat_id, mid, to_markdown_v2(_answered(parts, line)), parse_mode="MarkdownV2",
            )
        except Exception:
            logger.warning("telegram: approval finalize edit failed", exc_info=True)

    def _close_later(
        self, chat_id: str, mid: int, parts: list, request_id: str, outcome: str
    ) -> None:
        """:meth:`_close` for a wait that is being cancelled, on its own so the cancellation is
        not held up by a call to Telegram."""
        try:
            task = asyncio.get_running_loop().create_task(
                self._close(chat_id, mid, parts, request_id, outcome)
            )
        except RuntimeError:
            return
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def resolve_callback(self, cq: dict[str, Any]) -> None:
        """Resolve a pending approval from a ``callback_query`` (button press).

        Only the owner's press answers it. A prompt for a chat linked to a tracked group is posted
        in that group, where every member sees the buttons, and a member must not approve what
        the owner's agent runs. Anyone else's press is refused and logged."""
        data = cq.get("data", "") or ""
        cq_id = cq.get("id", "")
        action, _, request_id = data.partition(":")
        answer = "Recorded"
        place = action[len(_ANSWER):]
        if action.startswith(_ANSWER) and place.isdigit() and request_id:
            pending = self._pending.get(f"req:{request_id}")
            if pending is None or pending.future.done():
                # A press after the approval ended: told how it ended, not "Recorded".
                ended = self._ended.get(request_id, "")
                answer = _LATE_ANSWERS.get(ended) or _NO_LONGER_WAITING
            elif int(place) >= len(pending.answers):
                answer = _NOT_OFFERED
            else:
                presser = str((cq.get("from") or {}).get("id", "") or "")
                owner = str(self._owner() or "")
                if owner and presser == owner:
                    pending.future.set_result(pending.answers[int(place)]["key"])
                else:
                    answer = "Only the owner can answer this."
                    logger.warning("telegram: refused an approval press from %s, not the owner", presser)
                    sel().log_api_access(
                        caller=f"telegram:{presser or 'unknown'}",
                        operation="telegram.approval_press",
                        outcome="denied",
                        source="telegram",
                        resources=request_id,
                        error="not the owner",
                    )
        # Acknowledge so Telegram stops the button's spinner.
        if cq_id:
            try:
                await self._api.answer_callback_query(cq_id, text=answer)
            except Exception:
                logger.debug("telegram: answerCallbackQuery failed", exc_info=True)


def _approval_text(brief: dict, source: str) -> str:
    """The approval prompt, from core's brief (``approval_brief_for``): the tool, its arguments
    in a code block, the purpose the runner gave, the summary line (what the call can touch, and
    its risk) and, for a command that reaches a host off the owner's allowed hosts, the line
    saying so. That is what the dashboard's approval card shows; the prompt used to show the
    tool's name alone, so a command was approved unseen. Then what each standing answer does, in
    the card's words, so it is read before it is pressed. Every string is already masked."""
    tool = str(brief.get("tool") or "") or "a tool"
    lines = [f"🔐 [{source}] Approve `{tool}`?"]
    arguments = str(brief.get("input") or "")
    if arguments:
        lines += ["```", _unfenced(arguments), "```"]
    lines += [str(brief[k]) for k in ("purpose", "summary", "reach") if brief.get(k)]
    lines += [
        f"{a['label']}: {a['promise']}" for a in brief.get("answers") or [] if a.get("promise")
    ]
    return "\n".join(lines)


def _approval_markup(answers: list[dict[str, str]], request_id: str) -> dict[str, Any]:
    """The prompt's inline keyboard: one button per answer the brief offers, in its order, each
    on a row of its own so a phone shows its words whole. A button names its answer by its place
    (:data:`_ANSWER`), which fits Telegram's 64-byte cap whatever the request id is."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{_ANSWER_MARKS.get(a.get('ends', ''), '')} {a['label']}".strip(),
                    "callback_data": f"{_ANSWER}{place}:{request_id}",
                }
            ]
            for place, a in enumerate(answers)
        ]
    }


def _unfenced(text: str) -> str:
    """*text* with every run of three or more backticks broken by zero-width spaces, so what is
    shown cannot close the code block it is shown in."""
    return _FENCE_RUN.sub(lambda m: "\u200b".join(m.group(0)), text)


def _answered(parts: list, status: str) -> str:
    """What the prompt's last message says once it is answered: its own text with the outcome
    under it, so the chat keeps what was approved; or the outcome alone when that would not fit
    one message (the parts above keep the rest)."""
    text = f"{parts[-1].plain}\n{status}" if parts else status
    return text if len(render_parts(text)) == 1 else status


def _monotonic() -> float:
    import time

    return time.monotonic()
