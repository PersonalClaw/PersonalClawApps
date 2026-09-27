"""TelegramDelivery — MarkdownV2 rendering, throttled edit-streaming, inline approvals.

The Bot API is a fake implementing the ``TelegramAPI`` ABC (records calls, hands
back incrementing message ids); the throttle clock is injected so the edit-rate
contract is pinned without sleeping."""

from __future__ import annotations

import asyncio

import pytest

from telegram_runtime.api import TelegramAPI
from telegram_runtime.delivery import _EDIT_MIN_INTERVAL, TelegramDelivery


class FakeAPI(TelegramAPI):
    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[dict] = []
        self.uploads: list[dict] = []
        self.deleted: list[dict] = []
        #: An edit Telegram refuses: a ``TelegramAPIError`` raised instead of editing.
        self.refuse_edit: Exception | None = None
        self._mid = 0

    def _next(self) -> int:
        self._mid += 1
        return self._mid

    async def get_me(self):
        return {"id": 1, "username": "bot"}

    async def get_updates(self, offset=0, timeout=50, allowed_updates=None):
        return []

    async def send_message(self, chat_id, text, *, parse_mode=None, reply_to_message_id=None,
                           reply_markup=None, disable_web_page_preview=None):
        mid = self._next()
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                          "reply_markup": reply_markup, "message_id": mid})
        return {"message_id": mid}

    async def edit_message_text(self, chat_id, message_id, text, *, parse_mode=None,
                                reply_markup=None, disable_web_page_preview=None):
        if self.refuse_edit is not None:
            raise self.refuse_edit
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {"message_id": message_id}

    async def delete_message(self, chat_id, message_id):
        self.deleted.append({"chat_id": chat_id, "message_id": message_id})
        return True

    async def send_document(self, chat_id, file_path, *, caption=None, reply_to_message_id=None):
        mid = self._next()
        self.uploads.append({"kind": "document", "chat_id": chat_id, "path": file_path, "caption": caption})
        return {"message_id": mid}

    async def send_photo(self, chat_id, file_path, *, caption=None, reply_to_message_id=None):
        mid = self._next()
        self.uploads.append({"kind": "photo", "chat_id": chat_id, "path": file_path, "caption": caption})
        return {"message_id": mid}

    async def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False):
        self.answers.append({"id": callback_query_id, "text": text})
        return True


def _delivery(owner="42"):
    return TelegramDelivery(FakeAPI(), lambda: owner)


class TestTextDelivery:
    @pytest.mark.asyncio
    async def test_deliver_text_renders_markdownv2(self):
        d = _delivery()
        await d.deliver_text("123", "Hello. **bold**")
        sent = d._api.sent[0]
        assert sent["parse_mode"] == "MarkdownV2"
        assert sent["text"] == r"Hello\. *bold*"

    @pytest.mark.asyncio
    async def test_deliver_text_splits_long_body(self):
        d = _delivery()
        await d.deliver_text("123", "x" * 5000)
        assert [len(m["text"]) for m in d._api.sent] == [4096, 904]
        assert all(m["parse_mode"] == "MarkdownV2" for m in d._api.sent)

    @pytest.mark.asyncio
    async def test_open_dm_returns_user_id(self):
        d = _delivery()
        assert await d.open_dm("777") == "777"

    @pytest.mark.asyncio
    async def test_deliver_chat_mirror_renders_options_keyboard(self):
        d = _delivery()
        await d.deliver_chat_mirror("123", "Pick one\n[OPTIONS: Yes | No]")
        # last send carries the inline keyboard for the options
        markup = d._api.sent[-1]["reply_markup"]
        assert markup and "inline_keyboard" in markup
        labels = [btn[0]["text"] for btn in markup["inline_keyboard"]]
        assert labels == ["Yes", "No"]


class TestBuildThreadLink:
    def test_public_username_chat_gets_link(self):
        d = _delivery()
        assert d.build_thread_link("@mychan", "55") == "https://t.me/mychan/55"

    def test_private_numeric_chat_has_no_public_link(self):
        d = _delivery()
        assert d.build_thread_link("-100123", "55") == ""

    def test_empty_channel(self):
        d = _delivery()
        assert d.build_thread_link("", "1") == ""


class TestListReplyChannels:
    def test_minimal_dm_only(self):
        d = _delivery()
        chans = d.list_reply_channels()
        assert chans == [{"id": "dm", "name": "Direct Message"}]

    def test_is_tracked_channel_delegates_to_core_seam(self):
        # No tracked channels in the isolated home → False, no crash.
        d = _delivery()
        assert d.is_tracked_channel("-100999") is False


class TestUploadAttachment:
    @pytest.mark.asyncio
    async def test_image_goes_as_photo(self, tmp_path):
        f = tmp_path / "pic.PNG"
        f.write_bytes(b"x")
        d = _delivery()
        await d.upload_attachment("123", str(f), initial_comment="look")
        assert d._api.uploads[0]["kind"] == "photo"
        assert d._api.uploads[0]["caption"] == "look"

    @pytest.mark.asyncio
    async def test_other_goes_as_document(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b")
        d = _delivery()
        await d.upload_attachment("123", str(f))
        assert d._api.uploads[0]["kind"] == "document"


class TestStreamThrottle:
    @pytest.mark.asyncio
    async def test_throttles_to_one_edit_per_interval_then_flushes_exact_final(self):
        d = _delivery()
        clock = {"t": 100.0}
        d._now = lambda: clock["t"]

        sts = await d.start_stream("123", initial_text="…")  # send #1
        assert d._api.sent[0]["message_id"] == int(sts)
        assert d._api.edits == []

        # First append well within the interval → throttled (no edit).
        clock["t"] = 100.5
        await d.append_stream_task("123", sts, "t1", "Step one", "in_progress")
        assert d._api.edits == []

        # Second append still inside the interval → still throttled.
        clock["t"] = 100.9
        await d.append_stream_task("123", sts, "t2", "Step two", "in_progress")
        assert d._api.edits == []

        # Past the interval → exactly one edit fires, carrying the latest pending text.
        clock["t"] = 100.0 + _EDIT_MIN_INTERVAL + 0.01
        await d.append_stream_task("123", sts, "t3", "Step three", "complete")
        assert len(d._api.edits) == 1
        assert "Step three" in d._api.edits[-1]["text"]

        # stop_stream force-flushes even inside the interval — the exact final text.
        clock["t"] += 0.001  # basically no time passed
        await d.append_stream_task("123", sts, "t4", "Final step", "complete")  # throttled away
        assert len(d._api.edits) == 1  # confirm it was throttled
        await d.stop_stream("123", sts)
        assert len(d._api.edits) == 2  # forced flush
        assert "Final step" in d._api.edits[-1]["text"]

    @pytest.mark.asyncio
    async def test_stop_unknown_stream_is_noop(self):
        d = _delivery()
        await d.stop_stream("123", "999")  # never started
        assert d._api.edits == []


class TestNoPlaceholderIsLeftBehind:
    """Ledger 281: a turn's stream opens with "Thinking…" and its reply is a message of its own.
    The final edit re-sent the same text, Telegram answered 400 "message is not modified", and
    the "Thinking…" stayed above the reply for good, reading as a turn that never finished."""

    @pytest.mark.asyncio
    async def test_a_stream_that_only_held_its_placeholder_is_removed(self):
        d = _delivery()
        sts = await d.start_stream("123", initial_text="Thinking…")
        await d.stop_stream("123", sts)
        assert d._api.deleted == [{"chat_id": "123", "message_id": int(sts)}]
        assert d._api.edits == [], "the placeholder was re-sent instead of removed"

    @pytest.mark.asyncio
    async def test_a_stream_with_tasks_keeps_only_their_lines(self):
        d = _delivery()
        clock = {"t": 0.0}
        d._now = lambda: clock["t"]
        sts = await d.start_stream("123", initial_text="Thinking…")
        clock["t"] = 5.0
        await d.append_stream_task("123", sts, "tool_1", "Read notes.md", "in_progress")
        assert d._api.edits[-1]["text"] == "Thinking…\n⏳ Read notes\\.md"
        await d.append_stream_task("123", sts, "tool_1", "Read notes.md", "complete")
        await d.stop_stream("123", sts)
        assert d._api.edits[-1]["text"] == "✅ Read notes\\.md", (
            "the placeholder, or the task's in-progress line, was left behind"
        )
        assert d._api.deleted == []

    @pytest.mark.asyncio
    async def test_not_modified_is_the_text_already_there(self, caplog):
        import logging

        from telegram_runtime.api import TelegramAPIError

        d = _delivery()
        sts = await d.start_stream("123", initial_text="Thinking…")
        await d.append_stream_task("123", sts, "t1", "Step", "complete")
        d._api.refuse_edit = TelegramAPIError(
            "Bad Request: message is not modified: specified new message content and reply "
            "markup are exactly the same as a current content", error_code=400,
            method="editMessageText",
        )
        with caplog.at_level(logging.WARNING, logger="telegram_runtime.delivery"):
            await d.stop_stream("123", sts)
        assert "stream edit failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_an_edit_that_really_failed_says_so(self, caplog):
        import logging

        from telegram_runtime.api import TelegramAPIError

        d = _delivery()
        sts = await d.start_stream("123", initial_text="Thinking…")
        await d.append_stream_task("123", sts, "t1", "Step", "complete")
        d._api.refuse_edit = TelegramAPIError(
            "Bad Request: message to edit not found", error_code=400, method="editMessageText"
        )
        with caplog.at_level(logging.WARNING, logger="telegram_runtime.delivery"):
            await d.stop_stream("123", sts)
        assert "stream edit failed: Bad Request: message to edit not found" in caplog.text

    @pytest.mark.asyncio
    async def test_a_long_task_list_still_fits_one_message(self):
        from telegram_runtime.format import TELEGRAM_MAX_TEXT, utf16_len

        d = _delivery()
        sts = await d.start_stream("123", initial_text="Thinking…")
        for i in range(120):
            await d.append_stream_task("123", sts, f"t{i}", f"Step number {i} " + "x" * 40, "complete")
        await d.stop_stream("123", sts)
        final = d._api.edits[-1]["text"]
        assert utf16_len(final) <= TELEGRAM_MAX_TEXT
        assert final.startswith("…") and "Step number 119" in final


class _Event:
    def __init__(self, request_id="req1", title="delete files", tool_input="", brief=None):
        self.request_id = request_id
        self.title = title
        self.tool_input = tool_input
        self.tool_purpose = ""
        self.tool_meta = {} if brief is None else {"approval_brief": brief}


#: What core stamps on an approval it asks a channel: the call, masked, as the dashboard's card
#: shows it (``personalclaw.sdk.channel.approval_brief_for``).
_BRIEF = {
    "tool": "execute_bash",
    "input": '{"command": "deploy --token [REDACTED: credential] --env staging"}',
    "purpose": "ship the staging build",
    "risk": "destructive",
    "summary": "Can: runs a command · Risk: Destructive",
}


class TestApproval:
    @pytest.mark.asyncio
    async def test_inline_keyboard_approval_resolves_pending(self):
        d = _delivery(owner="42")

        # Kick off the approval prompt; it awaits the owner's button press.
        task = asyncio.ensure_future(
            d.request_approval(_Event("reqX", "rm -rf"), source="tool")
        )
        await asyncio.sleep(0)  # let it post the prompt + register the pending

        # It prompted the owner's DM with an Approve/Deny keyboard.
        prompt = d._api.sent[-1]
        assert prompt["chat_id"] == "42"
        buttons = prompt["reply_markup"]["inline_keyboard"][0]
        cbs = {b["callback_data"] for b in buttons}
        assert cbs == {"approve:reqX", "deny:reqX"}

        # A button press (callback_query) resolves the same pending future.
        await d.resolve_callback({"id": "cbq1", "data": "approve:reqX", "from": {"id": 42}})
        approved = await asyncio.wait_for(task, timeout=1.0)
        assert approved is True
        # button spinner acknowledged
        assert d._api.answers[-1]["id"] == "cbq1"
        # the prompt was finalized with an edit
        assert any("Approved" in e["text"] for e in d._api.edits)

    @pytest.mark.asyncio
    async def test_deny_resolves_false(self):
        d = _delivery(owner="42")
        task = asyncio.ensure_future(d.request_approval(_Event("reqY"), source="tool"))
        await asyncio.sleep(0)
        await d.resolve_callback({"id": "c2", "data": "deny:reqY", "from": {"id": 42}})
        assert await asyncio.wait_for(task, timeout=1.0) is False

    @pytest.mark.asyncio
    async def test_no_owner_no_chat_returns_none(self):
        d = _delivery(owner="")  # no owner, no linked session → cannot prompt
        result = await d.request_approval(_Event(), source="tool")
        assert result is None

    @pytest.mark.asyncio
    async def test_only_the_owner_answers_a_prompt(self):
        """A prompt for a chat linked to a tracked group is posted in the group, where every
        member sees the buttons. A member's press must not approve what the owner's agent runs."""
        d = _delivery(owner="42")
        task = asyncio.ensure_future(d.request_approval(_Event("reqG", "rm -rf"), source="tool"))
        await asyncio.sleep(0)

        await d.resolve_callback({"id": "m1", "data": "approve:reqG", "from": {"id": 5151}})
        await d.resolve_callback({"id": "m2", "data": "approve:reqG"})  # no presser at all
        await asyncio.sleep(0)
        assert not task.done(), "a member's press answered the owner's approval"
        assert [a["text"] for a in d._api.answers[-2:]] == ["Only the owner can answer this."] * 2

        # Floor: the owner's press, on the same prompt, does answer it.
        await d.resolve_callback({"id": "o1", "data": "approve:reqG", "from": {"id": 42}})
        assert await asyncio.wait_for(task, timeout=1.0) is True
        assert d._api.answers[-1] == {"id": "o1", "text": "Recorded"}

    @pytest.mark.asyncio
    async def test_a_refused_press_is_a_security_event(self, monkeypatch):
        import telegram_runtime.delivery as mod

        events = []
        monkeypatch.setattr(mod, "sel", lambda: type("S", (), {"log_api_access": lambda self, **kw: events.append(kw)})())
        d = _delivery(owner="42")
        task = asyncio.ensure_future(d.request_approval(_Event("reqS"), source="tool"))
        await asyncio.sleep(0)
        await d.resolve_callback({"id": "m1", "data": "deny:reqS", "from": {"id": 5151}})
        assert [(e["caller"], e["outcome"], e["resources"]) for e in events] == [
            ("telegram:5151", "denied", "reqS")
        ]
        await d.resolve_callback({"id": "o1", "data": "deny:reqS", "from": {"id": 42}})
        assert await asyncio.wait_for(task, timeout=1.0) is False
        assert len(events) == 1, "the owner's press is not an event"

    @pytest.mark.asyncio
    async def test_the_prompt_shows_what_will_run_as_the_dashboard_card_does(self):
        """The prompt was "Approve: execute_bash?" and nothing else, so a command was approved on
        the phone without being seen. It shows the tool, its arguments, the purpose and what the
        call can touch, from core's brief, as they are: core masked them."""
        from telegram_runtime.format import to_markdown_v2

        d = _delivery(owner="42")
        task = asyncio.ensure_future(
            d.request_approval(_Event("reqB", "execute_bash", brief=_BRIEF), source="subagent")
        )
        await asyncio.sleep(0)
        (prompt,) = d._api.sent
        source = (
            "🔐 [subagent] Approve `execute_bash`?\n"
            "```\n"
            '{"command": "deploy --token [REDACTED: credential] --env staging"}\n'
            "```\n"
            "ship the staging build\n"
            "Can: runs a command · Risk: Destructive"
        )
        assert prompt["text"] == to_markdown_v2(source)
        assert prompt["parse_mode"] == "MarkdownV2"

        await d.resolve_callback({"id": "c", "data": "approve:reqB", "from": {"id": 42}})
        assert await asyncio.wait_for(task, timeout=1.0) is True
        # Answered, the prompt keeps what was approved, with the outcome under it.
        assert d._api.edits[-1]["text"] == to_markdown_v2(f"{source}\n✅ Approved")

    @pytest.mark.asyncio
    async def test_arguments_holding_a_code_fence_cannot_close_the_block(self):
        from telegram_runtime.format import to_markdown_v2

        d = _delivery(owner="42")
        brief = {**_BRIEF, "input": "echo ```; echo done", "purpose": "", "summary": ""}
        task = asyncio.ensure_future(d.request_approval(_Event("reqF", brief=brief), source="t"))
        await asyncio.sleep(0)
        (prompt,) = d._api.sent
        assert prompt["text"] == to_markdown_v2(
            "🔐 [t] Approve `execute_bash`?\n```\necho `\u200b`\u200b`; echo done\n```"
        )
        await d.resolve_callback({"id": "c", "data": "deny:reqF", "from": {"id": 42}})
        assert await asyncio.wait_for(task, timeout=1.0) is False

    @pytest.mark.asyncio
    async def test_a_long_prompt_is_split_like_a_reply_with_the_buttons_last(self):
        """Ledger 281: the prompt went out as one message, which Telegram refuses past 4,096
        characters, so a long command was never asked about on Telegram at all. Every line of the
        arguments arrives, the buttons on the last part."""
        from telegram_runtime.format import TELEGRAM_MAX_TEXT, utf16_len

        d = _delivery(owner="42")
        command = "\n".join(f"echo step-{i}" for i in range(700))
        task = asyncio.ensure_future(
            d.request_approval(_Event("reqL", "execute_bash", tool_input=command), source="tool")
        )
        await asyncio.sleep(0)
        sent = d._api.sent
        assert len(sent) >= 2
        assert all(utf16_len(m["text"]) <= TELEGRAM_MAX_TEXT for m in sent)
        assert [m["reply_markup"] is not None for m in sent] == [False] * (len(sent) - 1) + [True]
        # Inside a code block MarkdownV2 escapes only backslashes and backticks.
        shown = "\n".join(m["text"] for m in sent)
        for i in range(700):
            assert f"echo step-{i}\n" in shown, f"step {i} was not shown"

        await d.resolve_callback({"id": "c", "data": "approve:reqL", "from": {"id": 42}})
        assert await asyncio.wait_for(task, timeout=1.0) is True
        final = d._api.edits[-1]
        assert final["message_id"] == sent[-1]["message_id"], "the buttons' message was not the one answered"
        assert final["text"].endswith("✅ Approved") and utf16_len(final["text"]) <= TELEGRAM_MAX_TEXT

    @pytest.mark.asyncio
    async def test_callback_for_unknown_request_just_acks(self):
        d = _delivery()
        await d.resolve_callback({"id": "c3", "data": "approve:ghost"})
        assert d._api.answers[-1]["id"] == "c3"  # acked, no crash


# ── core masks what it hands this channel ──────────────────────────────────────────────────

#: A key, assembled at runtime so the literal is not in the file, and its tail, which no
#: rendering escapes: the wire is searched for the tail, since a channel's markup may escape
#: the key's punctuation and hide the key from a search for all of it.
TAIL = "A" * 20 + "B" * 20 + "C" * 15
SECRET = "sk-" + "ant-api03-" + TAIL

#: Every way core hands this channel text, each carrying a key.
_HANDED = {
    "deliver_text": lambda h: h.deliver_text("123", f"token {SECRET}"),
    "deliver_notification": lambda h: h.deliver_notification(
        "123", f"Nightly {SECRET}", f"result {SECRET}"
    ),
    "deliver_rich": lambda h: h.deliver_rich(
        "123",
        {"inline_keyboard": [[{"text": f"Open {SECRET}", "callback_data": "open"}]]},
        f"fallback {SECRET}",
    ),
    "deliver_cron_result": lambda h: h.deliver_cron_result(
        "123", f"backup {SECRET}", "job-1", f"done {SECRET}"
    ),
    "deliver_chat_mirror": lambda h: h.deliver_chat_mirror("123", f"answer {SECRET}"),
    "deliver_subagent_reply": lambda h: h.deliver_subagent_reply(
        "123", f"reply {SECRET}", elapsed_secs=1.5
    ),
    "upload_attachment": lambda h: h.upload_attachment(
        "123", "report.txt", title=f"report {SECRET}"
    ),
    "start_stream": lambda h: h.start_stream("123", initial_text=f"thinking {SECRET}"),
    "request_approval": lambda h: h.request_approval(
        _Event("reqK", f"deploy {SECRET}"), source="tool"
    ),
}


@pytest.fixture
def through_core(monkeypatch):
    """This delivery as core holds it: registered with core, and read back the way core reads it."""
    from personalclaw import channel_delivery

    monkeypatch.setattr("telegram_runtime.delivery._APPROVAL_TIMEOUT", 0.01)
    d = _delivery()
    channel_delivery.register(d, provider="telegram")
    yield d, channel_delivery.delivery_for("telegram")
    channel_delivery.register(None, provider="telegram")


class TestCoreMasksWhatItHandsThisChannel:
    """Core masks every text it hands a channel, so this app masks nothing again: a key in anything
    core sends through it never reaches the Bot API."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", sorted(_HANDED))
    async def test_no_key_reaches_the_bot_api(self, through_core, method):
        d, handle = through_core
        await _HANDED[method](handle)
        wire = repr((d._api.sent, d._api.edits, d._api.uploads))
        assert TAIL not in wire
        assert "REDACTED" in wire, "nothing was sent"
