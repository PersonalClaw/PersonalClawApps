"""What a Telegram message holds besides its words, and the files its turn gets from it.

A message used to reach core's door as its text alone, and a message with no text (a voice note,
a sticker, a photo with no caption) never reached it at all. Now:

* **For a sender core already lets in** (this channel's owner, or a sender the owner or a
  pairing code approved: ``owner_id_for``, ``is_allowed_sender``, the store the door reads),
  each file is fetched from Telegram and handed to the door as the turn's attached files
  (``ChannelMessage.files``). A voice note is transcribed too, and its words lead the text.
* **For everyone else**, nothing is fetched. The message reaches the door with a line per
  attachment that names it, so someone new's photo is an Inbox row that says a photo came,
  not nothing, and no stranger's 20 MB is ever downloaded.
* What can't be read yet (a sticker, a location, a poll) is said as a line, for anyone.

The door still judges every message, exactly as before: this decides only what is fetched.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any

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

#: The largest file the Bot API hands a bot (``getFile``), inside core's own cap on an attachment.
MAX_FILE_BYTES = 20 * 1024 * 1024
#: Core's cap on a message's attachments (``attachments.MAX_COUNT``).
MAX_FILES = 20

# Telegram's keys for what it can't hand over as a file, and how each is said.
_UNREAD = {
    "location": "a location",
    "venue": "a place",
    "contact": "a contact",
    "poll": "a poll",
    "dice": "a dice roll",
    "game": "a game",
    "story": "a story",
}


@dataclass(frozen=True)
class Media:
    """One file a message carries: what Telegram calls it, and what the turn is told."""

    kind: str  # photo | document | voice | audio | video | video_note
    file_id: str
    name: str
    mimetype: str
    size: int = 0
    duration: int = 0

    @property
    def spoken(self) -> bool:
        return self.kind in ("voice", "audio")

    def line(self, why: str = "", *, with_size: bool = False) -> str:
        """How the turn is told of it when its bytes don't come: "[voice note, 0:12]"."""
        label = {
            "photo": "photo",
            "voice": "voice note",
            "audio": f"audio: {self.name}",
            "video": "video",
            "video_note": "video message",
        }.get(self.kind, f"file: {self.name}")
        parts = [label]
        if self.duration:
            parts.append(f"{self.duration // 60}:{self.duration % 60:02d}")
        if self.size and (self.kind == "document" or with_size):
            parts.append(_size(self.size))
        if why:
            parts.append(why)
        return "[" + ", ".join(parts) + "]"


def _size(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def media_of(message: dict[str, Any]) -> tuple[list[Media], list[str]]:
    """The files a message carries, and a line for each thing in it no file can carry."""
    files: list[Media] = []
    lines: list[str] = []
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        # Telegram lists each size of one photo, smallest first.
        big = photos[-1] if isinstance(photos[-1], dict) else {}
        if big.get("file_id"):
            files.append(Media("photo", str(big["file_id"]), "photo.jpg", "image/jpeg",
                               int(big.get("file_size") or 0)))
    for kind, default_name, default_type in (
        ("document", "file", "application/octet-stream"),
        ("voice", "voice.ogg", "audio/ogg"),
        ("audio", "audio", "audio/mpeg"),
        ("video", "video.mp4", "video/mp4"),
        ("video_note", "video_message.mp4", "video/mp4"),
    ):
        item = message.get(kind)
        if isinstance(item, dict) and item.get("file_id"):
            files.append(Media(
                kind, str(item["file_id"]),
                str(item.get("file_name") or default_name)[:200],
                str(item.get("mime_type") or default_type)[:100],
                int(item.get("file_size") or 0), int(item.get("duration") or 0),
            ))
    sticker = message.get("sticker")
    if isinstance(sticker, dict):
        lines.append(f"[sticker {sticker.get('emoji') or ''}]".replace(" ]", "]"))
    for key, said in _UNREAD.items():
        if key in message:
            lines.append(f"[{said}, which I can't read yet]")
    return files, lines


def lets_in(provider: str, sender: str) -> bool:
    """Whether core's trust store already lets *sender* in: the owner, or an approved sender."""
    if not sender:
        return False
    try:
        return sender == str(owner_id_for(provider) or "") or is_allowed_sender(provider, sender)
    except Exception:  # noqa: BLE001 — a store that can't be read lets no one's files in
        logger.debug("telegram: the trust store could not be read", exc_info=True)
        return False


async def gather(api: Any, provider: str, sender: str, message: dict[str, Any],
                 text: str) -> tuple[str, list[Attachment]]:
    """The text and files the door gets for *message*: its words, what it holds, and the files
    of a sender core lets in."""
    found, lines = media_of(message)
    trusted = lets_in(provider, sender)
    attachments: list[Attachment] = []
    spoken: list[str] = []
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
        try:
            data = await api.fetch_file(media.file_id, max_bytes=MAX_FILE_BYTES)
        except Exception as exc:  # noqa: BLE001 — one file that won't come never costs the message
            # The error is the client's own sentence, which never holds the file's URL.
            logger.warning("telegram: a %s could not be fetched: %s", media.kind, exc)
            lines.append(media.line("which could not be fetched"))
            continue
        attachments.append(Attachment(name=media.name, mimetype=media.mimetype, data=data))
        if media.spoken:
            words = await _transcribed(media, data)
            if words:
                spoken.append(words)
            else:
                lines.append(media.line("not transcribed"))
    if not text.strip() and attachments and not spoken and not lines:
        lines = [m.line() for m in found if m.kind in ("photo", "video", "video_note", "document")]
    parts = [*spoken, text.strip(), *lines]
    return "\n\n".join(p for p in parts if p), attachments


async def _transcribed(media: Media, data: bytes) -> str:
    """A voice note's words inside the markers Slack's voice memos use, or ``""``."""
    try:
        if not await stt_available():
            return ""
    except Exception:  # noqa: BLE001
        return ""
    suffix = ".ogg" if media.kind == "voice" else _suffix(media.name, ".audio")
    path = ""
    try:
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        words = await transcribe_audio(path)
    except Exception:  # noqa: BLE001 — no transcript keeps the file and says so
        logger.warning("telegram: a voice note could not be transcribed", exc_info=True)
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
