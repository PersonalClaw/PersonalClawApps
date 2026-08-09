"""MIME body + attachment text extraction for the mail-inbox provider.

The plan's T2.3 contract (EMAIL-INBOX-AND-TRIGGERS §C3):

- **prefer ``text/plain``** — a multipart/alternative mail carries the same content
  as plain and HTML; the plain part is the safest to read and needs no sanitization;
- **HTML-only mail is sanitized** — when there is no plain part we strip tags (and
  drop ``<script>``/``<style>`` bodies wholesale) down to visible text with a small
  stdlib parser, never rendering or executing anything;
- **attachment text via the platform's existing readers** — a PDF/DOCX/PPTX attachment
  is written to a temp file and run through ``personalclaw.sdk.channel.extract_text``
  (core's ``doc_parser``), the SAME reader core uses; no new parsing here.

Everything extracted is RAW: fencing happens downstream at prompt time
(``fence_untrusted`` in EIAT-4), never here — so text is never double-fenced.
"""

from __future__ import annotations

import logging
import os
import tempfile
from email.message import Message
from html.parser import HTMLParser

from personalclaw.sdk.channel import extract_text, is_parseable_document

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


def _attachment_text(part: Message, filename: str) -> str:
    """Extract text from a document attachment via core's reader. Empty on anything else."""
    ctype = part.get_content_type()
    if not is_parseable_document(ctype, filename):
        return ""
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    suffix = os.path.splitext(filename)[1] or ""
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="mailatt_", suffix=suffix, delete=False) as fh:
            fh.write(payload)
            tmp_path = fh.name
        return extract_text(tmp_path, mimetype=ctype, filename=filename)
    except OSError:
        logger.debug("mail-inbox: attachment temp-file write failed", exc_info=True)
        return ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def extract_body(msg: Message) -> str:
    """Return the readable body text of a parsed email, per the T2.3 contract.

    Prefers ``text/plain``; falls back to sanitized ``text/html``; then appends the
    extracted text of any parseable document attachment (PDF/DOCX/PPTX). Non-multipart
    mail is handled by its single content type.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachment_texts: list[str] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        filename = part.get_filename() or ""
        disposition = str(part.get("Content-Disposition", "")).lower()
        is_attachment = "attachment" in disposition or bool(filename)

        if is_attachment:
            text = _attachment_text(part, filename)
            if text.strip():
                attachment_texts.append(text.strip())
            continue

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

    if attachment_texts:
        joined_attachments = "\n\n".join(attachment_texts)
        body = f"{body}\n\n{joined_attachments}".strip() if body else joined_attachments

    return body[:_MAX_TEXT]
