"""A schedule can send its results to a Slack channel, and Slack says which ids it takes.

``^[CDGW][A-Z0-9]+$`` was core's rule for every channel's "Notify channel", which refused every
other platform's chats. It is Slack's rule, so it lives here now, behind
``ChannelTransportProvider.validate_target``.
"""

from __future__ import annotations

import pytest

from slack_runtime.transport import SlackTransport

REFUSAL = (
    "A Slack channel id starts with C, D, G or W, like C0123456789. It's at the bottom of the "
    "channel's About tab."
)


@pytest.mark.parametrize("target", ["C0123456789", "D0AP77JJSN6", "G01ABC", "W012345"])
def test_a_conversation_id_is_taken(target):
    assert SlackTransport({}).validate_target(target) == ""


@pytest.mark.parametrize("target", ["4242", "-1001234567890", "#general", "c0123456789", ""])
def test_anything_else_is_refused_in_words(target):
    assert SlackTransport({}).validate_target(target) == REFUSAL
