"""A mail's body and its attachments.

Multipart mail reads its text/plain part; HTML-only mail is sanitized; an attachment is a
file handed to core as it came, never text appended to the body. A PDF quote's text once
read as the message, with no attachment listed anywhere.
"""

from __future__ import annotations

import email
import email.policy

from mail_inbox_runtime.mime import attachments, extract_body, html_to_text

from _fakes import build_message


def _parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


def test_html_to_text_strips_tags_and_drops_scripts():
    html = (
        "<html><head><title>t</title><style>.x{color:red}</style></head>"
        "<body><p>Hello <b>world</b></p><script>alert(1)</script>"
        "<div>Second line</div></body></html>"
    )
    text = html_to_text(html)
    assert "Hello" in text and "world" in text and "Second line" in text
    assert "alert(1)" not in text  # script body dropped
    assert "color:red" not in text  # style body dropped
    assert "<" not in text and ">" not in text  # no tags survive


def test_multipart_prefers_text_plain():
    raw = build_message(plain="PLAIN VERSION", html="<p>HTML VERSION</p>")
    body = extract_body(_parse(raw))
    assert "PLAIN VERSION" in body
    assert "HTML VERSION" not in body  # plain wins over the html alternative


def test_html_only_mail_is_sanitized():
    raw = build_message(plain=None, html="<p>Only <i>HTML</i> here</p><script>x()</script>")
    body = extract_body(_parse(raw))
    assert "Only" in body and "HTML" in body and "here" in body
    assert "x()" not in body and "<" not in body


QUOTE_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def test_an_attachment_is_a_file_and_never_body_text():
    raw = build_message(
        plain="The revised quote is attached.",
        attachments=[("revised-quote.pdf", "application/pdf", QUOTE_PDF)],
    )
    msg = _parse(raw)
    assert extract_body(msg) == "The revised quote is attached."
    [quote] = attachments(msg)
    assert (quote.name, quote.mimetype, quote.data) == (
        "revised-quote.pdf",
        "application/pdf",
        QUOTE_PDF,
    )


def test_every_attachment_is_handed_over_whatever_its_type():
    raw = build_message(
        plain="just the body",
        attachments=[
            ("photo.png", "image/png", b"\x89PNG\r\n"),
            ("notes.txt", "text/plain", b"attached text"),
        ],
    )
    msg = _parse(raw)
    assert extract_body(msg).strip() == "just the body"  # an attached text file is not the body
    assert [(a.name, a.mimetype) for a in attachments(msg)] == [
        ("photo.png", "image/png"),
        ("notes.txt", "text/plain"),
    ]


def test_a_mail_with_no_attachment_hands_over_none():
    assert attachments(_parse(build_message(plain="hello"))) == []


def test_plain_text_charset_is_honored():
    raw = build_message(plain="café résumé", html=None)
    body = extract_body(_parse(raw))
    assert "café" in body and "résumé" in body
