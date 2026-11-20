"""A Discord message's attachments reach the chat, and nothing in a message is dropped silently.

A message used to reach PersonalClaw's door as its text alone, and one with no text (an image, a
file, a voice message, a sticker) never reached it at all. Now a sender core already lets in (the
owner, or one the owner or a pairing code approved) has their attachments fetched from Discord's
CDN and handed to the door as the turn's attached files, a voice message transcribed too. Anyone
else's message reaches the door with a line naming each attachment and nothing fetched.
"""

from __future__ import annotations

import httpx
import pytest

from discord_runtime import media as media_mod
from discord_runtime.settings import PROVIDER
from discord_runtime.transport import DiscordTransport
from personalclaw.sdk.channel import allow_sender

APPROVED = "42"
STRANGER = "77"
CDN = "https://cdn.discordapp.com/attachments/1/2"


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


@pytest.fixture
def cdn(monkeypatch):
    """The guarded client attachments are fetched with: each request recorded."""
    seen: list[str] = []
    state = {"handler": lambda request: httpx.Response(200, content=b"bytes")}

    def _client(**kw):
        def answer(request):
            seen.append(str(request.url))
            return state["handler"](request)

        return httpx.AsyncClient(transport=httpx.MockTransport(answer))

    monkeypatch.setattr("personalclaw.sdk.net.http_client", _client)
    return seen, state


@pytest.fixture(autouse=True)
def _approved():
    allow_sender(PROVIDER, APPROVED, "Ada")


def _transport() -> tuple[DiscordTransport, _Door]:
    t = DiscordTransport({"bot_token": "TEST"})
    door = _Door()
    t._services = door
    return t, door


def _message(sender: str = APPROVED, content: str = "", **fields) -> dict:
    return {"id": "9", "channel_id": "500", "content": content,
            "author": {"id": sender, "username": "ada"}, **fields}


def _attachment(name="cat.png", kind="image/png", size=1200, url=f"{CDN}/cat.png", **extra):
    return {"filename": name, "content_type": kind, "size": size, "url": url, **extra}


@pytest.mark.asyncio
async def test_an_image_with_no_text_from_an_approved_sender_reaches_the_door_with_its_file(cdn):
    """🔴 Red before: an attachment with no text never reached the door."""
    seen, _ = cdn
    t, door = _transport()
    await t._on_message_create(_message(attachments=[_attachment()]))
    (msg,) = door.messages
    assert seen == [f"{CDN}/cat.png"]
    assert [(f.name, f.mimetype, f.data) for f in msg.files] == [("cat.png", "image/png", b"bytes")]
    assert msg.text == "[image: cat.png]"


@pytest.mark.asyncio
async def test_an_attachment_beside_words_arrives_with_them(cdn):
    """🔴 Red before: the words came alone, and the agent never saw the file."""
    t, door = _transport()
    await t._on_message_create(_message(content="what is this?", attachments=[_attachment()]))
    (msg,) = door.messages
    assert len(msg.files) == 1
    assert msg.text == "what is this?"


@pytest.mark.asyncio
async def test_a_voice_message_is_transcribed(cdn, monkeypatch):
    async def _yes():
        return True

    async def _words(path):
        return "call me back"

    monkeypatch.setattr(media_mod, "stt_available", _yes)
    monkeypatch.setattr(media_mod, "transcribe_audio", _words)
    t, door = _transport()
    voice = _attachment("voice-message.ogg", "audio/ogg", url=f"{CDN}/v.ogg", duration_secs=4.2)
    await t._on_message_create(_message(flags=media_mod.VOICE_MESSAGE, attachments=[voice]))
    (msg,) = door.messages
    assert len(msg.files) == 1
    assert msg.text == "[Voice memo transcription]\ncall me back\n[End of transcription]"


@pytest.mark.asyncio
async def test_a_strangers_attachment_is_named_and_nothing_is_fetched(cdn):
    seen, _ = cdn
    t, door = _transport()
    await t._on_message_create(_message(sender=STRANGER, attachments=[
        _attachment(), _attachment("notes.pdf", "application/pdf", 2_200_000, f"{CDN}/n.pdf")]))
    (msg,) = door.messages
    assert seen == []
    assert msg.files == []
    assert msg.text == "[image: cat.png]\n\n[file: notes.pdf, 2.1 MB]"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/attachments/cat.png",
        "http://cdn.discordapp.com/attachments/1/2/cat.png",
        "https://cdn.discordapp.com.example.com/cat.png",
    ],
)
@pytest.mark.asyncio
async def test_an_attachment_address_off_discords_cdn_is_never_fetched(cdn, url):
    """A message names its attachments' addresses, so only Discord's own CDN is fetched from."""
    seen, _ = cdn
    t, door = _transport()
    await t._on_message_create(_message(content="look", attachments=[_attachment(url=url)]))
    (msg,) = door.messages
    assert seen == []
    assert msg.files == []
    assert msg.text == "look\n\n[image: cat.png, which could not be fetched]"


@pytest.mark.asyncio
async def test_a_sticker_alone_is_said_as_a_line(cdn):
    """🔴 Red before: a sticker alone was dropped."""
    t, door = _transport()
    await t._on_message_create(_message(sticker_items=[{"id": "1", "name": "wave"}]))
    (msg,) = door.messages
    assert msg.text == "[sticker wave]"


@pytest.mark.asyncio
async def test_an_attachment_past_the_limit_is_not_fetched_and_its_line_says_why(cdn):
    seen, _ = cdn
    t, door = _transport()
    big = _attachment("film.mov", "video/quicktime", 48 * 1024 * 1024, f"{CDN}/film.mov")
    await t._on_message_create(_message(attachments=[big]))
    (msg,) = door.messages
    assert seen == []
    assert msg.text == "[video: film.mov, 48.0 MB, too large to read]"


@pytest.mark.asyncio
async def test_a_download_past_its_cap_is_refused(cdn):
    """The size a message claims is the sender's to write: the bytes are counted too."""
    _, state = cdn
    state["handler"] = lambda request: httpx.Response(200, content=b"x" * 5000)
    with pytest.raises(media_mod.FetchRefused, match="larger than a message may carry"):
        await media_mod.fetch(f"{CDN}/x", max_bytes=1024)


@pytest.mark.asyncio
async def test_an_embed_alone_reaches_no_door(cdn):
    t, door = _transport()
    await t._on_message_create(_message(embeds=[{"title": "x"}]))
    assert door.messages == []
