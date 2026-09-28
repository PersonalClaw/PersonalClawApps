"""Email says which ids it takes: an address, and nothing else.

Core asks every chat channel set up whether an id sent without naming its channel is one of its
own (``ChannelTransportProvider.validate_target``), and sends it on the one that says yes. Email
kept the default check, which takes any id at all, so with email set up beside Slack or Telegram
every chat or channel id looked like it might be email's, and core could not tell where it went.
"""

from __future__ import annotations

import pytest

from email_runtime.transport import EmailTransport

REFUSAL = "An email address is what email sends to, like someone@example.com."


@pytest.mark.parametrize(
    "target", ["someone@example.com", "owner+alerts@example.org", "  someone@example.com "]
)
def test_an_address_is_taken(target):
    assert EmailTransport({}).validate_target(target) == ""


@pytest.mark.parametrize(
    "target",
    [
        "C0123456789",  # a Slack channel
        "-1001234567890",  # a Telegram chat
        "123456789012345678",  # a Discord channel
        "someone",
        "@example.com",
        "someone@",
        "a@b@example.com",
        "Someone <someone@example.com>",
        "someone@example.com, other@example.com",
        "some one@example.com",
        "",
    ],
)
def test_anything_else_is_refused_in_words(target):
    assert EmailTransport({}).validate_target(target) == REFUSAL
