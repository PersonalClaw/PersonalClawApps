"""A link in a reply reaches Telegram as a link only when it points at a web or mail address.

Telegram reads a link to ``tg://user?id=…`` as a mention, and in a group the person it names is
notified. A reply is a model's text, and a model may only be repeating something it read. So a
link to anything but the web or mail is shown as the text that was written. Every other character
of a reply is already escaped by the place it lands in, so no text can form markup of its own.
"""

from __future__ import annotations

from telegram_runtime.format import to_markdown_v2


def test_a_web_link_is_still_a_link():
    assert to_markdown_v2("Read [the guide](https://example.com/guide)") == (
        "Read [the guide](https://example.com/guide)"
    )
    assert to_markdown_v2("Write to [Ana](mailto:ana@example.com)") == (
        "Write to [Ana](mailto:ana@example.com)"
    )


def test_a_link_to_a_person_is_shown_as_written():
    assert to_markdown_v2("Ask [Ana](tg://user?id=42) today") == (
        r"Ask \[Ana\]\(tg://user?id\=42\) today"
    )
