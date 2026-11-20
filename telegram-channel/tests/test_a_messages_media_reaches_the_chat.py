"""A Telegram message's media reaches the chat, and nothing in a message is dropped silently.

A message used to reach PersonalClaw's door as its text alone, and one with no text (a voice note,
a sticker, a photo with no caption) never reached it at all. Now a sender core already lets in
(the owner, or one the owner or a pairing code approved) has their files fetched and handed to the
door as the turn's attached files, a voice note transcribed too. Anyone else's message reaches the
door with a line naming each attachment and nothing fetched. A sticker or a location is a line.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from personalclaw.sdk.channel import allow_sender
from telegram_runtime import media as media_mod
from telegram_runtime.api import API_BASE, HTTPTelegramAPI, TelegramAPI, TelegramAPIError
from telegram_runtime.settings import PROVIDER
from telegram_runtime.transport import TelegramTransport

APPROVED = "42"
STRANGER = "77"


class _API(TelegramAPI):
    """The Bot API as far as a message's files: each fetch recorded, each answered with bytes."""

    def __init__(self, fail: bool = False, takes: float = 0.0) -> None:
        self.fetched: list[str] = []
        self.given: list[float] = []
        self._fail = fail
        self._takes = takes

    async def fetch_file(self, file_id, *, max_bytes, seconds=None):
        self.fetched.append(file_id)
        self.given.append(seconds)
        await asyncio.sleep(self._takes)
        if self._fail:
            raise TelegramAPIError("the file could not be downloaded (ConnectError)", method="getFile")
        return f"bytes of {file_id}".encode()

    async def get_me(self):
        return {}

    async def get_updates(self, offset=0, timeout=50, allowed_updates=None):
        return []

    async def send_message(self, chat_id, text, **kw):
        return {}

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        return {}

    async def delete_message(self, chat_id, message_id):
        return True

    async def send_document(self, chat_id, document, **kw):
        return {}

    async def send_photo(self, chat_id, photo, **kw):
        return {}

    async def answer_callback_query(self, callback_query_id, **kw):
        return True


class _Verdict:
    allowed = True
    canned_reply = ""


class _Door:
    """Core's door as this transport calls it: each message recorded as it arrives."""

    def __init__(self) -> None:
        self.messages: list = []

    async def deliver_channel_inbound(self, provider, msg, *, is_dm=True):
        self.messages.append(msg)
        return _Verdict()


def _transport(api: TelegramAPI) -> tuple[TelegramTransport, _Door]:
    t = TelegramTransport({"bot_token": "TEST"})
    door = _Door()
    t._services = door
    t._api = api
    return t, door


def _message(sender: str = APPROVED, **fields) -> dict:
    return {
        "message_id": 9, "date": 1700000000,
        "chat": {"id": sender, "type": "private"},
        "from": {"id": sender, "first_name": "Ada"},
        **fields,
    }


PHOTO = [{"file_id": "small", "file_size": 900}, {"file_id": "large", "file_size": 90_000}]


@pytest.fixture(autouse=True)
def _approved():
    allow_sender(PROVIDER, APPROVED, "Ada")


@pytest.mark.asyncio
async def test_a_voice_note_from_an_approved_sender_reaches_the_door_with_its_file_and_words(
    monkeypatch,
):
    """🔴 Red before: a message with no text never reached the door."""
    async def _yes():
        return True

    async def _words(path):
        assert open(path, "rb").read() == b"bytes of v1"
        return "buy milk on the way home"

    monkeypatch.setattr(media_mod, "stt_available", _yes)
    monkeypatch.setattr(media_mod, "transcribe_audio", _words)
    api = _API()
    t, door = _transport(api)
    await t._on_message(_message(voice={"file_id": "v1", "duration": 12, "mime_type": "audio/ogg"}))
    (msg,) = door.messages
    assert api.fetched == ["v1"]
    assert [(f.name, f.mimetype, f.data) for f in msg.files] == [
        ("voice.ogg", "audio/ogg", b"bytes of v1")
    ]
    assert msg.text.startswith("[Voice memo transcription]\nbuy milk on the way home")


@pytest.mark.asyncio
async def test_a_voice_note_with_no_speech_to_text_keeps_its_file_and_says_so(monkeypatch):
    async def _no():
        return False

    monkeypatch.setattr(media_mod, "stt_available", _no)
    t, door = _transport(_API())
    await t._on_message(_message(voice={"file_id": "v1", "duration": 75}))
    (msg,) = door.messages
    assert len(msg.files) == 1
    assert msg.text == "[voice note, 1:15, not transcribed]"


@pytest.mark.asyncio
async def test_an_audio_file_whose_name_holds_a_nul_still_reaches_the_door(monkeypatch):
    """🔴 Red before: its name became the temp file's suffix, and the error dropped the message."""
    async def _yes():
        return True

    async def _words(path):
        return "hello"

    monkeypatch.setattr(media_mod, "stt_available", _yes)
    monkeypatch.setattr(media_mod, "transcribe_audio", _words)
    t, door = _transport(_API())
    await t._on_message(_message(audio={"file_id": "a1", "file_name": "song.mp\x003"}))
    (msg,) = door.messages
    assert len(msg.files) == 1
    assert msg.text.startswith("[Voice memo transcription]\nhello")


@pytest.mark.asyncio
async def test_a_captioned_photo_arrives_as_its_largest_size_beside_its_caption():
    """🔴 Red before: the caption came alone, and the agent never saw the picture."""
    api = _API()
    t, door = _transport(api)
    await t._on_message(_message(photo=PHOTO, caption="what is this?"))
    (msg,) = door.messages
    assert api.fetched == ["large"]
    assert [(f.name, f.mimetype) for f in msg.files] == [("photo.jpg", "image/jpeg")]
    assert msg.text == "what is this?"


@pytest.mark.asyncio
async def test_a_photo_with_no_caption_arrives_with_a_line_naming_it():
    """🔴 Red before: dropped, with no reply and no record."""
    t, door = _transport(_API())
    await t._on_message(_message(photo=PHOTO))
    (msg,) = door.messages
    assert len(msg.files) == 1
    assert msg.text == "[photo]"


@pytest.mark.asyncio
async def test_a_strangers_photo_reaches_the_door_named_and_nothing_is_fetched():
    """No stranger's bytes are downloaded: their message says what it held instead."""
    api = _API()
    t, door = _transport(api)
    await t._on_message(_message(sender=STRANGER, photo=PHOTO,
                                 document={"file_id": "d1", "file_name": "deck.pdf",
                                           "file_size": 2_200_000}))
    (msg,) = door.messages
    assert api.fetched == []
    assert msg.files == []
    assert msg.text == "[photo]\n\n[file: deck.pdf, 2.1 MB]"


@pytest.mark.parametrize(
    ("fields", "line"),
    [
        ({"sticker": {"file_id": "s1", "emoji": "😀"}}, "[sticker 😀]"),
        ({"location": {"latitude": 1.0, "longitude": 2.0}}, "[a location, which I can't read yet]"),
        ({"poll": {"id": "p"}}, "[a poll, which I can't read yet]"),
    ],
)
@pytest.mark.asyncio
async def test_what_no_file_can_carry_is_said_as_a_line(fields, line):
    """🔴 Red before: a sticker or a location alone was dropped."""
    api = _API()
    t, door = _transport(api)
    await t._on_message(_message(**fields))
    (msg,) = door.messages
    assert msg.text == line
    assert api.fetched == []


@pytest.mark.asyncio
async def test_a_file_past_telegrams_limit_is_not_fetched_and_its_line_says_why():
    api = _API()
    t, door = _transport(api)
    await t._on_message(_message(document={"file_id": "big", "file_name": "film.mov",
                                           "file_size": 48 * 1024 * 1024}, caption="look"))
    (msg,) = door.messages
    assert api.fetched == []
    assert msg.text == "look\n\n[file: film.mov, 48.0 MB, too large to read]"


@pytest.mark.asyncio
async def test_a_file_that_wont_come_is_named_and_the_message_still_arrives():
    t, door = _transport(_API(fail=True))
    await t._on_message(_message(photo=PHOTO, caption="here"))
    (msg,) = door.messages
    assert msg.files == []
    assert msg.text == "here\n\n[photo, which could not be fetched]"


@pytest.mark.asyncio
async def test_a_message_with_nothing_in_it_reaches_no_door():
    t, door = _transport(_API())
    await t._on_message(_message())
    assert door.messages == []


# ── The download itself ─────────────────────────────────────────────────────────────────────────

TOKEN = "123456:SECRET-bot-token"


def _api_with(download, *, file_path="photos/file_0.jpg", file_size=10):
    """The real client: getFile answered by a mock, the download by *download*."""

    def bot(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {
            "file_id": "f", "file_path": file_path, "file_size": file_size}})

    return HTTPTelegramAPI(TOKEN, client=httpx.AsyncClient(
        base_url=f"{API_BASE}/bot{TOKEN}", transport=httpx.MockTransport(bot)))


@pytest.fixture
def downloads(monkeypatch):
    """The guarded client the download is made with, answering from *handler*."""
    seen: list[str] = []
    state = {"handler": lambda request: httpx.Response(200, content=b"\x89PNG")}

    def _client(**kw):
        def answer(request):
            seen.append(str(request.url))
            return state["handler"](request)

        return httpx.AsyncClient(transport=httpx.MockTransport(answer))

    monkeypatch.setattr("personalclaw.sdk.net.http_client", _client)
    return seen, state


@pytest.mark.asyncio
async def test_the_client_fetches_a_file_through_the_guarded_client(downloads):
    seen, _ = downloads
    data = await _api_with(None).fetch_file("f", max_bytes=1024)
    assert data == b"\x89PNG"
    assert seen == [f"{API_BASE}/file/bot{TOKEN}/photos/file_0.jpg"]


@pytest.mark.asyncio
async def test_a_failed_download_never_names_the_token(downloads, caplog):
    """🔴 Red before: the client's error, with the token in its URL, was the context."""
    _, state = downloads

    def refuse(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    state["handler"] = refuse
    with pytest.raises(TelegramAPIError) as raised:
        await _api_with(None).fetch_file("f", max_bytes=1024)
    assert TOKEN not in str(raised.value)
    # No exception it keeps holds the client's error, whose request names the address:
    # ``from None`` would hide it, yet keep it as the context a crash reporter walks.
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_a_download_past_its_cap_is_refused(downloads):
    _, state = downloads
    state["handler"] = lambda request: httpx.Response(200, content=b"x" * 5000)
    with pytest.raises(TelegramAPIError, match="larger than a message may carry"):
        await _api_with(None).fetch_file("f", max_bytes=1024)


async def _trickle():
    """A download that never ends: a byte at a time, with a pause before each."""
    while True:
        await asyncio.sleep(0.05)
        yield b"x"


@pytest.mark.asyncio
async def test_a_download_that_trickles_stops_at_its_deadline(downloads, monkeypatch):
    """🔴 Red before: the read timeout bounds a stall, not a trickle, and the channel reads its
    messages one at a time, so a file that never finished held every message behind it."""
    _, state = downloads
    state["handler"] = lambda request: httpx.Response(200, content=_trickle())
    monkeypatch.setattr("telegram_runtime.api.DOWNLOAD_SECONDS", 0.3)
    with pytest.raises(TelegramAPIError, match="ran past the 0.3 s") as raised:
        await asyncio.wait_for(_api_with(None).fetch_file("f", max_bytes=10**9), 5)
    assert TOKEN not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


@pytest.mark.asyncio
async def test_a_messages_downloads_share_one_deadline(monkeypatch):
    """🔴 Red before: each download had a deadline of its own, so a message's files together could
    hold the channel for as many deadlines as it had files."""
    monkeypatch.setattr("telegram_runtime.api.DOWNLOAD_SECONDS", 0.3)
    api = _API(takes=0.2)
    t, door = _transport(api)
    await t._on_message(_message(
        photo=PHOTO,
        document={"file_id": "d1", "file_name": "deck.pdf", "file_size": 10},
        video={"file_id": "v1", "file_size": 10},
    ))
    (msg,) = door.messages
    assert api.fetched == ["large", "d1"]
    assert api.given[0] == 0.3 and api.given[1] < 0.15
    assert msg.text.endswith("[video, past the 0.3 s one message's files may take]")


@pytest.mark.parametrize("path", ["../secret", "photos/../../x", "", "photos/a b.jpg"])
@pytest.mark.asyncio
async def test_a_file_path_telegram_would_not_name_is_never_requested(downloads, path):
    seen, _ = downloads
    with pytest.raises(TelegramAPIError, match="named no file"):
        await _api_with(None, file_path=path).fetch_file("f", max_bytes=1024)
    assert seen == []


@pytest.mark.asyncio
async def test_the_file_address_with_the_token_reaches_nothing_the_message_keeps(
    downloads, caplog
):
    """The download's address holds the bot token. Only the bytes and their name and type reach
    the door: no text, metadata, file name or log line the message leaves holds the address.

    The log lines are those the gateway's sinks show (``personalclaw.log_sinks``): every record
    of this app's own code at any level, and any library's at WARNING or above. httpx's own INFO
    line for a request names its URL, as it does for every Bot API call, and the sinks never show
    a library's INFO."""
    caplog.set_level("DEBUG")
    seen, _ = downloads
    t, door = _transport(_api_with(None, file_path="photos/file_7.jpg", file_size=4))
    await t._on_message(_message(photo=PHOTO, caption="what is this?"))
    (msg,) = door.messages
    assert seen == [f"{API_BASE}/file/bot{TOKEN}/photos/file_7.jpg"], "the file came by its address"
    shown = "\n".join(
        r.getMessage() for r in caplog.records
        if r.name.startswith("telegram_runtime") or r.levelno >= logging.WARNING
    )
    kept = [msg.text, repr(msg.metadata), *(f"{f.name}|{f.mimetype}" for f in msg.files),
            *(f.data.decode("latin-1") for f in msg.files), shown]
    for value in kept:
        assert TOKEN not in value
        assert "/file/bot" not in value
    assert [(f.name, f.mimetype, f.data) for f in msg.files] == [("photo.jpg", "image/jpeg", b"\x89PNG")]
