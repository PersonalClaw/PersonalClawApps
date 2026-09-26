"""A schedule can send its results to a Discord channel, and Discord says which ids it takes.

Core used to accept only one other platform's channel-id shape for a schedule's "Notify channel",
so no Discord channel could be named. Core now asks the channel
(``ChannelTransportProvider.validate_target``), and this is Discord's answer: a snowflake.
"""

from __future__ import annotations

import pytest

from discord_runtime.transport import DiscordTransport

REFUSAL = (
    "A Discord channel id is a long number, like 1234567890123456789. With Developer Mode on, "
    "right-click the channel and pick Copy Channel ID."
)


@pytest.mark.parametrize("target", ["123456789012345678", "1234567890123456789", "12345678901234567890"])
def test_a_snowflake_is_taken(target):
    assert DiscordTransport({}).validate_target(target) == ""


@pytest.mark.parametrize("target", ["4242", "-1001234567890", "C0123456789", "#general", ""])
def test_anything_else_is_refused_in_words(target):
    assert DiscordTransport({}).validate_target(target) == REFUSAL
