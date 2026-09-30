"""A mail's body and its attachments, for the mail-inbox provider.

- **prefer ``text/plain``** — a multipart/alternative mail carries the same content
  as plain and HTML; the plain part is the safest to read and needs no sanitization;
- **HTML-only mail is sanitized** — when there is no plain part we strip tags (and
  drop ``<script>``/``<style>`` bodies wholesale) down to visible text with a small
  stdlib parser, never rendering or executing anything;
- **attachments are files, not body text** — each attached part is handed to core as it
  came (:func:`attachments`, ``personalclaw.sdk.inbox.Attachment``: its name, its type,
  its bytes). Core keeps them with the Inbox row, lists them there with a download, and
  gives an agent reading the message each one's text inside a fence. An attachment's text
  was once appended to the body, so a PDF quote read as the message and no attachment was
  listed anywhere.

Everything extracted is RAW: fencing happens downstream at prompt time
(``fence_untrusted``), never here — so text is never double-fenced.
"""

from __future__ import annotations

import logging
import mimetypes
from email.message import Message
from html.parser import HTMLParser

from personalclaw.sdk.inbox import Attachment

logger = logging.getLogger(__name__)

# Tags whose *content* is program/style text, not human-visible prose — drop the body.
_DROP_CONTENT_TAGS = frozenset({"script", "style", "head", "title"})
# Block-level tags that should force a line break so stripped text stays readable.
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
)
# Cap extracted text so a pathological mail can't blow up the inbox item / event.
_MAX_TEXT = 100_000


class _HTMLToText(HTMLParser):
    """Collapse HTML to visible text: drop script/style bodies, break on block tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._suppress_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._suppress_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT_TAGS and self._suppress_depth > 0:
            self._suppress_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        # Collapse runs of blank lines / trailing whitespace the tag-breaks introduce.
        raw = "".join(self._parts)
        lines = [ln.strip() for ln in raw.splitlines()]
        out: list[str] = []
        for ln in lines:
            if ln or (out and out[-1]):
                out.append(ln)
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    """Sanitize HTML to visible text — no tags, no script/style bodies, ever rendered."""
    parser = _HTMLToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # a malformed fragment must not crash the poll loop
        logger.debug("mail-inbox: HTML parse failed; returning best-effort text", exc_info=True)
    return parser.text()


def _decode_part(part: Message) -> str:
    """Decode one part's payload to str, honoring its declared charset (utf-8 fallback)."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", errors="replace")


def _is_attachment(part: Message) -> bool:
    """A part is an attachment when it says so or names a file; the body is what is left."""
    disposition = str(part.get("Content-Disposition", "")).lower()
    return "attachment" in disposition or bool(part.get_filename())


def attachments(msg: Message) -> list[Attachment]:
    """The files a parsed email came with, in the order it carries them.

    Each is its part's file name (or ``attachment-<n>`` with the extension its type suggests,
    for a part that names none), its declared type and its decoded bytes. What size or count
    is kept is core's decision (``personalclaw.attachments``), made the same way for every
    source, so nothing is dropped here."""
    found: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart() or not _is_attachment(part):
            continue
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        ctype = part.get_content_type()
        name = (part.get_filename() or "").strip()
        if not name:
            name = f"attachment-{len(found) + 1}{mimetypes.guess_extension(ctype) or ''}"
        found.append(Attachment(name=name, mimetype=ctype, data=data))
    return found


def extract_body(msg: Message) -> str:
    """Return the readable body text of a parsed email.

    Prefers ``text/plain``; falls back to sanitized ``text/html``. An attachment is not
    body text (:func:`attachments`). Non-multipart mail is handled by its single content type.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in msg.walk():
        if part.is_multipart() or _is_attachment(part):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain_parts.append(_decode_part(part))
        elif ctype == "text/html":
            html_parts.append(_decode_part(part))

    if plain_parts:
        body = "\n".join(p for p in plain_parts if p.strip()).strip()
    elif html_parts:
        body = html_to_text("\n".join(html_parts))
    else:
        body = ""
    return body[:_MAX_TEXT]
