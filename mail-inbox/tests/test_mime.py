"""MIME extraction — text/plain preference, HTML sanitization, attachment text.

Covers T2.3: multipart mail extracts text/plain; HTML-only mail is sanitized; a PDF
attachment contributes extracted text via the platform's existing document readers.
"""

from __future__ import annotations

import email
import email.policy

from mail_inbox_runtime.mime import extract_body, html_to_text

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


def test_pdf_attachment_contributes_extracted_text():
    # A minimal PDF with an uncompressed text stream — core's binary-scan reader pulls it.
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        b"stream\nBT (ATTACHMENT_TEXT_MARKER) Tj ET\nendstream\n"
        b"%%EOF\n"
    )
    raw = build_message(plain="body text", attachments=[("report.pdf", "application/pdf", pdf)])
    body = extract_body(_parse(raw))
    assert "body text" in body
    assert "ATTACHMENT_TEXT_MARKER" in body  # extracted via core doc_parser


def test_non_document_attachment_is_ignored():
    raw = build_message(
        plain="just the body", attachments=[("photo.png", "image/png", b"\x89PNG\r\n")]
    )
    body = extract_body(_parse(raw))
    assert body.strip() == "just the body"  # image contributes no text


def test_plain_text_charset_is_honored():
    raw = build_message(plain="café résumé", html=None)
    body = extract_body(_parse(raw))
    assert "café" in body and "résumé" in body
