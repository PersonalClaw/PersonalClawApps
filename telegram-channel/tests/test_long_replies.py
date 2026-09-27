"""A long reply reaches Telegram whole: every part is MarkdownV2 the Bot API accepts.

The delivery converted a reply to MarkdownV2 and THEN cut the result at 4096 characters. A cut
inside a code block left the first part with an open ``` and the next with code that is no
longer inside one, and a cut can fall between an escape's backslash and its character. The Bot
API refuses such a part with ``400 Bad Request: can't parse entities``, and the delivery raised
on the first refusal, so an 8,159-character reply with a code block arrived as nothing at all.

The API here answers ``sendMessage`` the way Telegram does, judged by an independent MarkdownV2
parser (``_v2.py``) rather than by the app's own renderer: an unparseable text is refused, the
4096 limit applies to the parsed text counted in UTF-16 code units, and an empty one is refused.
"""

from __future__ import annotations

import logging

import pytest

from personalclaw.sdk.channel import OutboundMessage

from telegram_runtime.api import TelegramAPI, TelegramAPIError
from telegram_runtime.delivery import TelegramDelivery
from telegram_runtime.transport import TelegramTransport

from _v2 import MAX_TEXT, ParseError, in_entity, parse_markdown_v2, utf16_len

PROSE = "Here is the migration, step by step."
CODE = "op.add_column('shipments', sa.Column('eta_confidence', sa.Numeric(4, 3)))"
#: The reply that never arrived (fakes/probes.py): 100 prose lines, a 60-line code block, a
#: closing line — 8,159 characters.
REPLY = (PROSE + "\n") * 100 + "```python\n" + (CODE + "\n") * 60 + "```\nDone."


class BotAPI(TelegramAPI):
    """``sendMessage`` as the Bot API answers it. ``refuse_v2`` / ``refuse_plain`` force a refusal
    for a text they match (a formatting Telegram rejects for a reason no local parser models, and
    a chat that refuses the message whatever its formatting)."""

    def __init__(self, *, refuse_v2=None, refuse_plain=None) -> None:
        self.calls: list[dict] = []  # every sendMessage, accepted or not
        self.sent: list[dict] = []  # what the chat shows
        self._refuse_v2 = refuse_v2 or (lambda text: False)
        self._refuse_plain = refuse_plain or (lambda text: False)
        self._mid = 0

    async def get_me(self):
        return {"id": 1, "username": "bot"}

    async def get_updates(self, offset=0, timeout=50, allowed_updates=None):
        return []

    async def send_message(self, chat_id, text, *, parse_mode=None, reply_to_message_id=None,
                           reply_markup=None, disable_web_page_preview=None):
        call = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                "reply_markup": reply_markup, "reply_to_message_id": reply_to_message_id}
        self.calls.append(call)
        if parse_mode == "MarkdownV2":
            if self._refuse_v2(text):
                raise TelegramAPIError(
                    "Bad Request: can't parse entities: Unsupported start tag", error_code=400,
                    method="sendMessage",
                )
            try:
                plain, entities = parse_markdown_v2(text)
            except ParseError as exc:
                raise TelegramAPIError(f"Bad Request: {exc}", error_code=400, method="sendMessage") from None
        else:
            if self._refuse_plain(text):
                raise TelegramAPIError("Bad Request: chat not found", error_code=400, method="sendMessage")
            plain, entities = text, []
        if not plain.strip():
            raise TelegramAPIError("Bad Request: message text is empty", error_code=400, method="sendMessage")
        if utf16_len(plain) > MAX_TEXT:
            raise TelegramAPIError("Bad Request: message is too long", error_code=400, method="sendMessage")
        self._mid += 1
        self.sent.append({**call, "plain": plain, "entities": entities, "message_id": self._mid})
        return {"message_id": self._mid}

    async def edit_message_text(self, chat_id, message_id, text, *, parse_mode=None,
                                reply_markup=None, disable_web_page_preview=None):
        return {"message_id": message_id}

    async def send_document(self, chat_id, file_path, *, caption=None, reply_to_message_id=None):
        return {}

    async def send_photo(self, chat_id, file_path, *, caption=None, reply_to_message_id=None):
        return {}

    async def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False):
        return True


def _delivery(api: BotAPI) -> TelegramDelivery:
    return TelegramDelivery(api, lambda: "42")


def _shown(api: BotAPI) -> str:
    """What the chat shows, message after message."""
    return "\n".join(m["plain"] for m in api.sent)


def _assert_the_reply_arrived_whole(api: BotAPI) -> None:
    assert len(api.sent) >= 2, "an 8,159-character reply cannot be one Telegram message"
    for m in api.sent:
        assert m["parse_mode"] == "MarkdownV2", f"a part fell back to plain text: {m['text'][:80]!r}"
        assert utf16_len(m["text"]) <= MAX_TEXT
    shown = _shown(api)
    assert shown.count(PROSE) == 100
    assert shown.count(CODE) == 60
    assert shown.rstrip().endswith("Done.")
    for m in api.sent:
        if CODE in m["plain"]:
            assert in_entity(m["plain"], m["entities"], CODE, "pre"), (
                "a code line arrived outside a code block"
            )


class TestTheReplyThatNeverArrived:
    def test_the_repro_is_the_one_measured(self):
        assert len(REPLY) == 8159

    @pytest.mark.asyncio
    async def test_a_mirrored_answer_arrives_whole(self):
        api = BotAPI()
        await _delivery(api).deliver_chat_mirror("42", REPLY)
        _assert_the_reply_arrived_whole(api)

    @pytest.mark.asyncio
    async def test_a_delivered_text_arrives_whole(self):
        api = BotAPI()
        last = await _delivery(api).deliver_text("42", REPLY)
        _assert_the_reply_arrived_whole(api)
        assert last == str(api.sent[-1]["message_id"])

    @pytest.mark.asyncio
    async def test_a_subagent_reply_arrives_whole_before_its_footer(self):
        api = BotAPI()
        await _delivery(api).deliver_subagent_reply("42", REPLY, elapsed_secs=2.5)
        footer = api.sent.pop()
        assert footer["plain"] == "took 2.5s"
        _assert_the_reply_arrived_whole(api)


class TestARefusedPartIsNeverDropped:
    @pytest.mark.asyncio
    async def test_a_part_refused_as_markdown_goes_out_as_plain_text(self, caplog):
        body = ("First paragraph line.\n" * 150) + ("REFUSED here.\n" * 200) + ("Last line.\n" * 150)
        api = BotAPI(refuse_v2=lambda text: "REFUSED" in text)
        with caplog.at_level(logging.WARNING):
            await _delivery(api).deliver_text("42", body)
        shown = _shown(api)
        assert shown.count("First paragraph line.") == 150
        assert shown.count("REFUSED here.") == 200
        assert shown.count("Last line.") == 150
        fallback = [m for m in api.sent if m["parse_mode"] is None]
        assert fallback and all("REFUSED here." in m["text"] for m in fallback)
        assert all("\\" not in m["text"] for m in fallback), "the plain fallback is the source, not the escaped text"
        assert any("plain text" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    @pytest.mark.asyncio
    async def test_a_part_refused_as_plain_text_too_raises(self, caplog):
        api = BotAPI(refuse_v2=lambda text: True, refuse_plain=lambda text: True)
        with caplog.at_level(logging.WARNING), pytest.raises(TelegramAPIError):
            await _delivery(api).deliver_text("42", "Hello there.")
        assert [c["parse_mode"] for c in api.calls] == ["MarkdownV2", None], (
            "the plain re-send was never tried"
        )
        assert any("not delivered" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    @pytest.mark.asyncio
    async def test_a_refusal_that_is_not_about_formatting_is_not_retried(self):
        class Blocked(BotAPI):
            async def send_message(self, chat_id, text, **kw):
                self.calls.append({"parse_mode": kw.get("parse_mode")})
                raise TelegramAPIError("Forbidden: bot was blocked by the user", error_code=403)

        api = Blocked()
        with pytest.raises(TelegramAPIError):
            await _delivery(api).deliver_text("42", "Hello there.")
        assert [c["parse_mode"] for c in api.calls] == ["MarkdownV2"]


class TestTheLimitIsTelegrams:
    @pytest.mark.asyncio
    async def test_emoji_count_twice_toward_the_limit(self):
        """3,060 characters, but 6,060 UTF-16 code units: Telegram's count, not Python's."""
        text = ("🙂" * 50 + "\n") * 60
        assert len(text) < MAX_TEXT < utf16_len(text)
        api = BotAPI()
        await _delivery(api).deliver_text("42", text)
        assert len(api.sent) >= 2
        assert _shown(api).count("🙂") == 3000

    @pytest.mark.asyncio
    async def test_a_keyboard_rides_the_last_part_only(self):
        markup = {"inline_keyboard": [[{"text": "Open", "callback_data": "opt:0"}]]}
        api = BotAPI()
        await _delivery(api).deliver_rich("42", markup, "A long result line.\n" * 400)
        assert len(api.sent) >= 2
        assert [m["reply_markup"] for m in api.sent[:-1]] == [None] * (len(api.sent) - 1)
        assert api.sent[-1]["reply_markup"] == markup
        assert _shown(api).count("A long result line.") == 400

    @pytest.mark.asyncio
    async def test_a_long_cron_result_keeps_its_header_first(self):
        api = BotAPI()
        await _delivery(api).deliver_cron_result("42", "nightly", "job-1", REPLY)
        assert api.sent[0]["plain"].startswith("⏰ Cron: nightly")
        assert sum(m["plain"].count("⏰ Cron: nightly") for m in api.sent) == 1
        assert _shown(api).count(CODE) == 60

    @pytest.mark.asyncio
    async def test_a_long_notification_arrives_whole(self):
        api = BotAPI()
        await _delivery(api).deliver_notification("42", "Weekly report", REPLY)
        assert api.sent[0]["plain"].startswith("💓 Weekly report")
        assert _shown(api).count(PROSE) == 100 and _shown(api).count(CODE) == 60


class TestLinesLongerThanAPart:
    @pytest.mark.asyncio
    async def test_one_long_paragraph_is_cut_between_words(self):
        text = "word " * 2000
        api = BotAPI()
        await _delivery(api).deliver_text("42", text)
        assert len(api.sent) >= 3
        for m in api.sent:
            assert all(token == "word" for token in m["plain"].split())
        assert sum(len(m["plain"].split()) for m in api.sent) == 2000

    @pytest.mark.asyncio
    async def test_a_code_line_longer_than_a_part_stays_code_in_every_part(self):
        text = "Build output:\n```\n" + "x" * 9000 + "\n```\nThat is all."
        api = BotAPI()
        await _delivery(api).deliver_text("42", text)
        assert len(api.sent) >= 3
        assert _shown(api).count("x") == 9000
        for m in api.sent:
            if "x" * 10 in m["plain"]:
                start = m["plain"].index("x")
                run = m["plain"][start:].split("\n", 1)[0]
                assert in_entity(m["plain"], m["entities"], run, "pre")


class TestTheGenericSend:
    @pytest.mark.asyncio
    async def test_a_long_message_from_the_channels_page_arrives_in_parts(self):
        api = BotAPI()
        transport = TelegramTransport({"bot_token": "123:test"})
        transport._api = api
        ok = await transport.send(OutboundMessage(channel_id="42", text=REPLY, thread_id="77"))
        assert ok is True
        _assert_the_reply_arrived_whole(api)
        assert [m["reply_to_message_id"] for m in api.sent] == [77] + [None] * (len(api.sent) - 1)
