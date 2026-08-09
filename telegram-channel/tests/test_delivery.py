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
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {"message_id": message_id}

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
    return TelegramDelivery(FakeAPI(), owner)


class TestTextDelivery:
    @pytest.mark.asyncio
    async def test_deliver_text_renders_markdownv2(self):
        d = _delivery()
        await d.deliver_text("123", "Hello. **bold**")
        sent = d._api.sent[0]
        assert sent["parse_mode"] == "MarkdownV2"
        assert sent["text"] == r"Hello\. *bold*"

    @pytest.mark.asyncio
    async def test_deliver_text_redacts_credentials(self):
        d = _delivery()
        await d.deliver_text("123", "token sk-ABC123DEF456GHI789JKL012MNO345PQR")
        # the raw secret must not survive into the wire text
        assert "sk-ABC123DEF456GHI789JKL012MNO345PQR" not in d._api.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_deliver_text_splits_long_body(self):
        d = _delivery()
        await d.deliver_text("123", "x" * 5000)
        assert len(d._api.sent) == 2

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


class _Event:
    def __init__(self, request_id="req1", title="delete files"):
        self.request_id = request_id
        self.title = title


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
        await d.resolve_callback({"id": "cbq1", "data": "approve:reqX"})
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
        await d.resolve_callback({"id": "c2", "data": "deny:reqY"})
        assert await asyncio.wait_for(task, timeout=1.0) is False

    @pytest.mark.asyncio
    async def test_no_owner_no_chat_returns_none(self):
        d = _delivery(owner="")  # no owner, no linked session → cannot prompt
        result = await d.request_approval(_Event(), source="tool")
        assert result is None

    @pytest.mark.asyncio
    async def test_callback_for_unknown_request_just_acks(self):
        d = _delivery()
        await d.resolve_callback({"id": "c3", "data": "approve:ghost"})
        assert d._api.answers[-1]["id"] == "c3"  # acked, no crash
