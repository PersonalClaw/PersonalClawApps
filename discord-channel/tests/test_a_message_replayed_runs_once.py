"""A message Discord sends again runs one turn.

A gateway session that resumes is sent the events it missed, and one this receiver had already
handed on before the connection dropped can come again; a gateway restarted in the middle of one
is in the same place. Each message crosses PersonalClaw's guarded door with Discord's own id for it
and its channel's id. The door takes each message once and answers a delivery of it made again as
received: the replayed message runs no turn, and this bundle's automations hear of it once.
"""

from __future__ import annotations

import asyncio

import pytest

from discord_runtime import inbound_tap
from discord_runtime.transport import DiscordTransport
from personalclaw.sdk.channel import allow_sender

OWNER = "42"


class _Session:
    def __init__(self) -> None:
        self.key = "discord-1"
        self.running = False
        self.task = None

    def append(self, *a, **k) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.session = _Session()
        self.linked: dict = {}
        self._background_tasks: set = set()

    def get_linked_session(self, thread_key):
        return self.linked.get(thread_key)

    def get_or_create_session(self, app=""):
        return self.session

    def link_channel(self, key, thread_key, channel_id, *, provider):
        self.linked[thread_key] = self.session

    def notify(self, *a, **k) -> None:
        return None


class _Services:
    """The door the transport delivers to: core's own, with the turn it starts recorded."""

    def __init__(self) -> None:
        self.dashboard_state = _State()
        self.turns: list[str] = []

    async def deliver_channel_inbound(self, provider, msg, *, is_dm=True):
        from personalclaw.channel_inbound import deliver_inbound

        async def turn_runner(state, session, text):
            self.turns.append(text)

        return await deliver_inbound(self, provider, msg, is_dm=is_dm, turn_runner=turn_runner)


class _Delivery:
    async def deliver_text(self, *a, **k):
        return "1"


def _message(text: str, *, message_id: str = "1290000000000000009") -> dict:
    """A direct message's MESSAGE_CREATE: no ``guild_id``."""
    return {
        "id": message_id,
        "channel_id": "500",
        "content": text,
        "author": {"id": OWNER, "username": "ada", "global_name": "Ada", "bot": False},
    }


async def _deliver(transport, payload) -> None:
    """Hand *payload* to the receiver as the platform does, and let the turn it starts run."""
    await transport._on_message_create(payload)
    state = transport._services.dashboard_state
    await asyncio.gather(*list(state._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)


@pytest.fixture
def transport():
    allow_sender("discord", OWNER, "Ada")
    t = DiscordTransport({"bot_token": "TEST"})
    t._services = _Services()
    t._delivery = _Delivery()
    return t


@pytest.mark.asyncio
async def test_a_message_sent_again_runs_one_turn_and_is_published_once(transport):
    heard: list[str] = []

    def observer(cm, *, is_dm):
        heard.append(cm.text)

    inbound_tap.subscribe(observer)
    try:
        await _deliver(transport, _message("What's on today?"))
        await _deliver(transport, _message("What's on today?"))
    finally:
        inbound_tap.unsubscribe(observer)

    assert transport._services.turns == ["What's on today?"]
    assert heard == ["What's on today?"], "the automations heard the replayed message"


@pytest.mark.asyncio
async def test_two_messages_with_the_same_words_are_two_turns(transport):
    """The partner: Discord's ids tell the messages apart, whatever they say."""
    await _deliver(transport, _message("yes", message_id="1290000000000000009"))
    await _deliver(transport, _message("yes", message_id="1290000000000000010"))

    assert transport._services.turns == ["yes", "yes"]
