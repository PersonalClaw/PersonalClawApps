"""A schedule can send its results to a Telegram chat, and Telegram says which ids it takes.

Core used to accept only one other platform's channel-id shape for a schedule's "Notify channel",
so a Telegram chat id (``4242``, ``-1001234567890``) was refused there. Core now asks the channel
(``ChannelTransportProvider.validate_target``), and this is Telegram's answer: what ``sendMessage``
takes as ``chat_id``.
"""

from __future__ import annotations

import pytest

from telegram_runtime.transport import TelegramTransport

REFUSAL = (
    "A Telegram chat id is a number, like 4242, or -1001234567890 for a group, or a public "
    "channel's @username."
)


@pytest.mark.parametrize("target", ["4242", "-1001234567890", "-4242", "@launch_news"])
def test_a_chat_id_or_a_public_channel_is_taken(target):
    assert TelegramTransport({}).validate_target(target) == ""


@pytest.mark.parametrize(
    "target", ["C0123456789", "launch_news", "@abc", "42 42", "", "@9starts_with_a_digit"]
)
def test_anything_else_is_refused_in_words(target):
    assert TelegramTransport({}).validate_target(target) == REFUSAL
