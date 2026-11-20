"""A Discord message's attachments reach the chat, and nothing in a message is dropped silently.

A message used to reach PersonalClaw's door as its text alone, and one with no text (an image, a
file, a voice message, a sticker) never reached it at all. Now a sender core already lets in (the
owner, or one the owner or a pairing code approved) has their attachments fetched from Discord's
CDN and handed to the door as the turn's attached files, a voice message transcribed too. Anyone
else's message reaches the door with a line naming each attachment and nothing fetched.
"""

from __future__ import annotations

import asyncio

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


MB = 1024 * 1024


def _sized(request: httpx.Request) -> httpx.Response:
    """Each attachment answered with as many bytes as its address names: ``…/10mb-a.png``."""
    megabytes = int(request.url.path.rsplit("/", 1)[1].split("mb")[0])
    return httpx.Response(200, content=b"x" * (megabytes * MB))


@pytest.mark.asyncio
async def test_one_messages_files_take_at_most_25_mb_in_all(cdn):
    """🔴 Red before: each file was capped, the message wasn't, so ten 20 MB files came in."""
    seen, state = cdn
    state["handler"] = _sized
    t, door = _transport()
    files = [_attachment(f"{n}.png", size=10 * MB, url=f"{CDN}/10mb-{n}.png") for n in "abc"]
    await t._on_message_create(_message(attachments=files))
    (msg,) = door.messages
    assert seen == [f"{CDN}/10mb-a.png", f"{CDN}/10mb-b.png"]
    assert [f.name for f in msg.files] == ["a.png", "b.png"]
    assert msg.text == "[image: c.png, 10.0 MB, past the 25 MB one message's files may take]"


@pytest.mark.asyncio
async def test_a_message_whose_sizes_lie_still_takes_at_most_25_mb(cdn):
    """The sizes are the sender's to write: the bytes are counted against what's left."""
    seen, state = cdn
    state["handler"] = _sized
    t, door = _transport()
    files = [_attachment(f"{n}.png", url=f"{CDN}/10mb-{n}.png") for n in "abc"]
    await t._on_message_create(_message(attachments=files))
    (msg,) = door.messages
    assert len(seen) == 3
    assert [f.name for f in msg.files] == ["a.png", "b.png"]
    assert sum(len(f.data) for f in msg.files) == 20 * MB
    assert msg.text == "[image: c.png, which could not be fetched]"


@pytest.mark.asyncio
async def test_an_audio_file_whose_name_holds_a_nul_still_reaches_the_door(cdn, monkeypatch):
    """🔴 Red before: its name became the temp file's suffix, and the error dropped the message."""
    async def _yes():
        return True

    async def _words(path):
        return "hello"

    monkeypatch.setattr(media_mod, "stt_available", _yes)
    monkeypatch.setattr(media_mod, "transcribe_audio", _words)
    t, door = _transport()
    await t._on_message_create(_message(attachments=[
        _attachment("song.mp\x003", "audio/mpeg", url=f"{CDN}/song.mp3")]))
    (msg,) = door.messages
    assert len(msg.files) == 1
    assert msg.text == "[Voice memo transcription]\nhello\n[End of transcription]"


@pytest.mark.asyncio
async def test_a_failed_download_keeps_no_exception_that_names_its_address(cdn):
    """🔴 Red before: ``from None`` hid the client's error but kept it as the context."""
    _, state = cdn

    def refuse(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    state["handler"] = refuse
    with pytest.raises(media_mod.FetchRefused) as raised:
        await media_mod.fetch(f"{CDN}/signed.png?ex=1&hm=SIGNATURE", max_bytes=1024)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert "SIGNATURE" not in str(raised.value)


async def _trickle():
    """A download that never ends: a byte at a time, with a pause before each."""
    while True:
        await asyncio.sleep(0.05)
        yield b"x"


@pytest.mark.asyncio
async def test_a_download_that_trickles_stops_at_its_deadline(cdn, monkeypatch):
    """🔴 Red before: the read timeout bounds a stall, not a trickle, and the channel waits on a
    message's files before it hands the message on."""
    _, state = cdn
    state["handler"] = lambda request: httpx.Response(200, content=_trickle())
    monkeypatch.setattr(media_mod, "DOWNLOAD_SECONDS", 0.3)
    with pytest.raises(media_mod.FetchRefused, match="ran past the 0.3 s") as raised:
        await asyncio.wait_for(media_mod.fetch(f"{CDN}/slow.png", max_bytes=10**9), 5)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


@pytest.mark.asyncio
async def test_a_messages_downloads_share_one_deadline(cdn, monkeypatch):
    """🔴 Red before: each attachment had a deadline of its own, so a message of ten could hold its
    handling for ten deadlines. Now the first takes its time, the second gets what is left of the
    message's and is stopped there, and the third is named past it."""
    seen, state = cdn

    async def _slow():
        for _ in range(2):
            await asyncio.sleep(0.05)
            yield b"x"

    state["handler"] = lambda request: httpx.Response(
        200, content=_slow() if request.url.path.endswith("/a.png") else _trickle())
    monkeypatch.setattr(media_mod, "DOWNLOAD_SECONDS", 0.3)
    given: list[float | None] = []
    fetch = media_mod.fetch

    async def _fetch(url, *, max_bytes, deadline=None):
        given.append(deadline)
        return await fetch(url, max_bytes=max_bytes, deadline=deadline)

    monkeypatch.setattr(media_mod, "fetch", _fetch)
    t, door = _transport()
    files = [_attachment(f"{n}.png", url=f"{CDN}/{n}.png") for n in "abc"]
    await asyncio.wait_for(t._on_message_create(_message(attachments=files)), 5)
    (msg,) = door.messages
    assert seen == [f"{CDN}/a.png", f"{CDN}/b.png"]
    assert given[0] is not None and given[0] == given[1]
    assert [f.name for f in msg.files] == ["a.png"]
    assert msg.text == "\n\n".join([
        "[image: b.png, which could not be fetched]",
        "[image: c.png, past the 0.3 s one message's files may take]",
    ])


def test_a_messages_downloads_end_inside_one_heartbeat():
    """🔴 Red before: the budget was 120 s. The gateway's receive loop waits on a message's files
    before it reads the next frame, a heartbeat's ACK included, and a beat still unacknowledged
    when the next is due closes the connection (``gateway._beat_once``). Discord's interval is
    about 41 s (41250 ms, the gateway's default before HELLO says)."""
    assert media_mod.DOWNLOAD_SECONDS < 41.25


@pytest.mark.asyncio
async def test_a_transcription_that_never_ends_stops_at_the_messages_deadline(cdn, monkeypatch):
    """🔴 Red before: a voice message's transcription ran outside the deadline, on the gateway's
    receive loop, for as long as it took. Now it gets what is left of the message's time, and past
    it the file still arrives, said as not transcribed."""
    async def _yes():
        return True

    async def _forever(path):
        await asyncio.sleep(30)
        return "never"

    monkeypatch.setattr(media_mod, "stt_available", _yes)
    monkeypatch.setattr(media_mod, "transcribe_audio", _forever)
    monkeypatch.setattr(media_mod, "DOWNLOAD_SECONDS", 0.3)
    t, door = _transport()
    voice = _attachment("voice-message.ogg", "audio/ogg", url=f"{CDN}/v.ogg", duration_secs=4.2)
    await asyncio.wait_for(
        t._on_message_create(_message(flags=media_mod.VOICE_MESSAGE, attachments=[voice])), 5)
    (msg,) = door.messages
    assert len(msg.files) == 1
    assert msg.text == "[voice message, 0:04, not transcribed]"
