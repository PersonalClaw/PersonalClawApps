"""An update Telegram serves again runs one turn.

Telegram serves an update again until a poll confirms it, and this receiver saves its place after
each batch, so a gateway stopped in the middle of one is served the batch again when it starts.
Each message crosses PersonalClaw's guarded door with Telegram's own id for it and its chat's id
(Telegram numbers each chat's messages from one, so an id names a message only in its chat). The
door takes each message once and answers a delivery of it made again as received: the second
serving runs no turn, and this bundle's automations hear of the message once.
"""

from __future__ import annotations

import asyncio

import pytest

from personalclaw.sdk.channel import allow_sender, track
from telegram_runtime import inbound_tap
from telegram_runtime.transport import TelegramTransport

OWNER = "42"


class _Session:
    def __init__(self) -> None:
        self.key = "telegram-1"
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


def _update(text: str, *, message_id: int = 9, chat: str = OWNER, sender: str = OWNER,
            chat_type: str = "private", update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1700000000,
            "text": text,
            "chat": {"id": chat, "type": chat_type, "title": "Family"},
            "from": {"id": sender, "first_name": "Ada"},
        },
    }


async def _deliver(transport, payload) -> None:
    """Hand *payload* to the receiver as the platform does, and let the turn it starts run."""
    await transport._dispatch(payload)
    state = transport._services.dashboard_state
    await asyncio.gather(*list(state._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)


@pytest.fixture
def transport():
    allow_sender("telegram", OWNER, "Ada")
    t = TelegramTransport({"bot_token": "TEST"})
    t._services = _Services()
    t._delivery = _Delivery()
    return t


@pytest.mark.asyncio
async def test_an_update_served_twice_runs_one_turn_and_is_published_once(transport):
    heard: list[str] = []

    def observer(cm, *, is_dm):
        heard.append(cm.text)

    inbound_tap.subscribe(observer)
    try:
        await _deliver(transport, _update("What's on today?"))
        await _deliver(transport, _update("What's on today?"))
    finally:
        inbound_tap.unsubscribe(observer)

    assert transport._services.turns == ["What's on today?"]
    assert heard == ["What's on today?"], "the automations heard the update served again"


@pytest.mark.asyncio
async def test_one_message_id_in_two_chats_is_two_messages(transport):
    """The partner: the owner's message 9 in her DM and message 9 in a group she is in are two
    messages, each its own turn."""
    track("telegram", "-100777", "Family")
    await _deliver(transport, _update("hello", update_id=1))
    await _deliver(
        transport,
        _update("hello from the group", chat="-100777", chat_type="supergroup", update_id=2),
    )

    assert transport._services.turns == ["hello", "hello from the group"]
