"""What a Discord message holds besides its words, and the files its turn gets from it.

A message used to reach core's door as its text alone, and a message with no text (an image, a
file, a voice message, a sticker) never reached it at all. Now:

* **For a sender core already lets in** (this channel's owner, or a sender the owner or a
  pairing code approved: ``owner_id_for``, ``is_allowed_sender``, the store the door reads),
  each attachment is fetched from Discord's CDN and handed to the door as the turn's attached
  files (``ChannelMessage.files``). A voice message is transcribed too, its words leading.
* **For everyone else**, nothing is fetched. The message reaches the door with a line per
  attachment that names it, so someone new's image is an Inbox row that says an image came.
* A sticker is said as a line, for anyone.

An attachment's address is taken from the message, so it is fetched only from Discord's own
CDN over https (:data:`CDN_HOSTS`), through the egress guard. The door still judges every message
exactly as before: this decides only what is fetched.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from personalclaw.sdk.channel import (
    Attachment,
    is_allowed_sender,
    owner_id_for,
    redact_credentials,
    redact_exfiltration_urls,
    stt_available,
    transcribe_audio,
)

logger = logging.getLogger(__name__)

#: The most of one attachment a turn takes: Telegram's bot limit, kept for both channel apps,
#: inside core's own cap on an attachment (``attachments.MAX_BYTES``).
MAX_FILE_BYTES = 20 * 1024 * 1024
#: Core's cap on a message's attachments (``attachments.MAX_COUNT``).
MAX_FILES = 20
#: The most one message's files take in all, so a message costs the gateway one file's worth
#: (core's ``attachments.MAX_BYTES``) however many it carries: Discord allows ten a message.
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
#: The longest one message's downloads may take in all, start to end. The read timeout ends a
#: download that stalls; this ends one that trickles, since a message waits on its files before it
#: is handed on.
DOWNLOAD_SECONDS = 120.0
#: The hosts Discord serves attachments from. A message names its attachments' addresses, so
#: an address anywhere else is never fetched.
CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
#: A message's ``flags`` bit for a voice message.
VOICE_MESSAGE = 1 << 13


class FetchRefused(Exception):
    """An attachment that is not fetched, in words safe to show and log."""


@dataclass(frozen=True)
class Media:
    """One attachment: where Discord serves it, and what the turn is told of it."""

    name: str
    mimetype: str
    url: str
    size: int = 0
    duration: float = 0.0
    voice: bool = False

    @property
    def kind(self) -> str:
        if self.voice:
            return "voice"
        top = self.mimetype.split("/", 1)[0]
        return top if top in ("image", "audio", "video") else "file"

    def line(self, why: str = "", *, with_size: bool = False) -> str:
        """How the turn is told of it when its bytes don't come: "[image: cat.png]"."""
        label = {"voice": "voice message"}.get(self.kind, f"{self.kind}: {self.name}")
        parts = [label]
        if self.duration:
            secs = int(self.duration)
            parts.append(f"{secs // 60}:{secs % 60:02d}")
        if self.size and (self.kind == "file" or with_size):
            parts.append(_size(self.size))
        if why:
            parts.append(why)
        return "[" + ", ".join(parts) + "]"


def _size(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def media_of(message: dict[str, Any]) -> tuple[list[Media], list[str]]:
    """The attachments a message carries, and a line for each thing in it no file can carry."""
    files: list[Media] = []
    voice = bool(int(message.get("flags") or 0) & VOICE_MESSAGE)
    for item in message.get("attachments") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        files.append(Media(
            name=str(item.get("filename") or "file")[:200],
            mimetype=str(item.get("content_type") or "application/octet-stream")[:100],
            url=str(item["url"]),
            size=int(item.get("size") or 0),
            duration=float(item.get("duration_secs") or 0),
            voice=voice,
        ))
    lines = [f"[sticker {s.get('name') or ''}]".replace(" ]", "]")
             for s in message.get("sticker_items") or [] if isinstance(s, dict)]
    return files, lines


def lets_in(provider: str, sender: str) -> bool:
    """Whether core's trust store already lets *sender* in: the owner, or an approved sender."""
    if not sender:
        return False
    try:
        return sender == str(owner_id_for(provider) or "") or is_allowed_sender(provider, sender)
    except Exception:  # noqa: BLE001 — a store that can't be read lets no one's files in
        logger.debug("discord: the trust store could not be read", exc_info=True)
        return False


async def fetch(url: str, *, max_bytes: int, seconds: float | None = None) -> bytes:
    """One attachment from Discord's CDN through the egress guard, refused past *max_bytes* or
    *seconds* (all of :data:`DOWNLOAD_SECONDS` when not given)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in CDN_HOSTS:
        raise FetchRefused("its address is not on Discord's CDN")
    from personalclaw.sdk.net import http_client

    data = bytearray()
    failed = ""
    try:
        async with asyncio.timeout(DOWNLOAD_SECONDS if seconds is None else seconds):
            async with http_client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        raise FetchRefused(f"Discord answered {resp.status_code}")
                    async for chunk in resp.aiter_bytes():
                        data += chunk
                        if len(data) > max_bytes:
                            raise FetchRefused("it is larger than a message may carry")
    except FetchRefused:
        raise
    except TimeoutError:
        failed = f"it ran past the {DOWNLOAD_SECONDS:g} s a message's files may take"
    except Exception as exc:  # noqa: BLE001 — told in its own words; the address is signed
        failed = f"it could not be downloaded ({type(exc).__name__})"
    if failed:
        # Raised past the except block, so no exception it keeps holds the client's error, whose
        # request names the signed address: ``from None`` hides that, yet keeps it as context.
        raise FetchRefused(failed)
    return bytes(data)


async def gather(provider: str, sender: str, message: dict[str, Any],
                 text: str) -> tuple[str, list[Attachment]]:
    """The text and files the door gets for *message*: its words, what it holds, and the files
    of a sender core lets in."""
    found, lines = media_of(message)
    trusted = lets_in(provider, sender)
    attachments: list[Attachment] = []
    spoken: list[str] = []
    left = MAX_MESSAGE_BYTES
    # A message's downloads share one deadline, as they share one byte budget.
    budget = DOWNLOAD_SECONDS
    clock = asyncio.get_running_loop().time
    spent = 0.0
    for media in found:
        if not trusted:
            lines.append(media.line())
            continue
        if len(attachments) >= MAX_FILES:
            lines.append(media.line(f"past the {MAX_FILES} files a message keeps"))
            continue
        if media.size > MAX_FILE_BYTES:
            lines.append(media.line("too large to read", with_size=True))
            continue
        if left <= 0 or media.size > left:
            lines.append(media.line(f"past the {MAX_MESSAGE_BYTES // (1024 * 1024)} MB one "
                                    "message's files may take", with_size=True))
            continue
        if spent >= budget:
            lines.append(media.line(f"past the {budget:g} s one message's files may take"))
            continue
        started = clock()
        try:
            data = await fetch(media.url, max_bytes=min(MAX_FILE_BYTES, left),
                               seconds=budget - spent)
        except FetchRefused as exc:
            logger.warning("discord: an attachment was not fetched: %s", exc)
            lines.append(media.line("which could not be fetched"))
            continue
        finally:
            spent += clock() - started
        left -= len(data)
        attachments.append(Attachment(name=media.name, mimetype=media.mimetype, data=data))
        if media.voice or media.kind == "audio":
            words = await _transcribed(media, data)
            if words:
                spoken.append(words)
            elif media.voice:
                lines.append(media.line("not transcribed"))
    if not text.strip() and attachments and not spoken and not lines:
        lines = [m.line() for m in found]
    parts = [*spoken, text.strip(), *lines]
    return "\n\n".join(p for p in parts if p), attachments


async def _transcribed(media: Media, data: bytes) -> str:
    """A voice message's words inside the markers Slack's voice memos use, or ``""``."""
    try:
        if not await stt_available():
            return ""
    except Exception:  # noqa: BLE001
        return ""
    path = ""
    try:
        fd, path = tempfile.mkstemp(suffix=_suffix(media.name, ".ogg"))
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        words = await transcribe_audio(path)
    except Exception:  # noqa: BLE001 — no transcript keeps the file and says so
        logger.warning("discord: a voice message could not be transcribed", exc_info=True)
        return ""
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    words = (words or "").strip()
    if not words:
        return ""
    words, _ = redact_exfiltration_urls(words)
    words, _ = redact_credentials(words)
    return f"[Voice memo transcription]\n{words}\n[End of transcription]"


#: An extension a temp file may take from a sender's file name: a dot and a few plain characters.
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,7}")


def _suffix(name: str, default: str) -> str:
    """*name*'s extension when it is a plain one, else *default*: the name is the sender's."""
    ext = os.path.splitext(name)[1]
    return ext if _SUFFIX.fullmatch(ext) else default
