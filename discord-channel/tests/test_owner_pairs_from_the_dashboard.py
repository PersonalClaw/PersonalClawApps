"""The owner pairs Discord from the dashboard, and the running receiver reaches them at once.

Discord's owner id — who core DMs results and approval prompts to — was written only by
``personalclaw setup``. The channel's Configure page now mints a code (core's
``create_owner_pairing_code``); the owner sends it to the bot in a DM, the message crosses core's
guarded door, and core stores the sender under Discord's own owner key. For that to reach anyone,
Discord has to declare it can pair (``owner_pairing``) and read its owner when it needs it: the
delivery used to keep the owner it saw when the receiver started, so an owner paired afterwards got
no approval prompt until a restart.

Driven through the real transport, the real core door and the real credential store in this test's
home; only the REST client and the gateway socket are fakes.
"""

from __future__ import annotations

import asyncio
import secrets

import pytest

from personalclaw import channel_trust
from personalclaw.config import credentials
from personalclaw.sdk.channel import owner_id_credential

import discord_runtime.transport as transport_mod
from discord_runtime.api import DiscordAPI
from discord_runtime.delivery import INTERACTION_TYPE_COMPONENT
from discord_runtime.settings import CRED_DISCORD_BOT_TOKEN
from discord_runtime.transport import create_provider

_OWN = owner_id_credential("discord")
_SHARED = "PERSONALCLAW_OWNER_ID"
TOKEN = f"MTA.{secrets.token_hex(12)}"
OWNER = "99887766554433"


@pytest.fixture(autouse=True)
def _keys_restored(monkeypatch):
    for key in (_OWN, _SHARED, CRED_DISCORD_BOT_TOKEN):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv(CRED_DISCORD_BOT_TOKEN, TOKEN)


class _FakeAPI(DiscordAPI):
    """The REST client: records what the bot sends."""

    def __init__(self, token: str, **_kw) -> None:
        self.sent: list[dict] = []
        self._id = 0

    async def get_gateway_bot(self):
        return {"url": "wss://gateway.invalid", "session_start_limit": {"remaining": 1000}}

    async def create_message(self, channel_id, content, *, components=None, message_reference=None):
        self._id += 1
        self.sent.append({"channel_id": str(channel_id), "content": content, "components": components})
        return {"id": str(self._id)}

    async def edit_message(self, channel_id, message_id, content, *, components=None):
        return {"id": message_id}

    async def create_dm(self, user_id):
        return {"id": f"dm-{user_id}", "type": 1}

    async def upload_file(self, channel_id, file_path, *, filename="", content=""):
        return {"id": "0"}

    async def get_channel(self, channel_id):
        return {"id": str(channel_id)}

    async def get_user(self, user_id):
        return {"id": str(user_id)}

    async def create_interaction_response(self, interaction_id, interaction_token, *, callback_type=6):
        return None

    async def add_reaction(self, channel_id, message_id, emoji):
        return None

    async def trigger_typing(self, channel_id):
        return None

    async def close(self) -> None:
        return None


class _FakeGateway:
    """The gateway socket: connects nowhere and waits to be stopped."""

    def __init__(self, token: str, **_kw) -> None:
        pass

    async def run(self) -> None:
        await asyncio.sleep(3600)

    async def stop(self) -> None:
        return None


class _Services:
    """What the gateway hands a receiver, at the seam it calls: the REAL core door."""

    dashboard_state = None

    def register_channel_delivery(self, delivery, provider=""):
        self.delivery = delivery

    async def deliver_channel_inbound(self, provider, msg, *, is_dm=True):
        from personalclaw.channel_inbound import deliver_inbound

        async def turn_runner(state, session, text):  # a turn is not what this test is about
            return None

        return await deliver_inbound(self, provider, msg, is_dm=is_dm, turn_runner=turn_runner)


class _Event:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.title = "shell: ls"
        self.tool_meta: dict = {}


def _dm(text: str, message_id: str) -> dict:
    """A MESSAGE_CREATE in a DM: Discord marks it by the ABSENCE of ``guild_id``."""
    return {
        "id": message_id, "channel_id": f"dm-{OWNER}", "content": text,
        "author": {"id": OWNER, "username": "dana"},
    }


def test_discord_says_its_owner_can_pair_from_the_dashboard():
    assert create_provider({}).capabilities().owner_pairing is True


def test_discord_says_a_dm_is_one_conversation_so_a_handoff_continues_there():
    """Core links a handed-off chat to the DM itself only when the channel says a DM's thread is
    its channel id — which this transport's inbound mapping does for every DM message."""
    assert create_provider({}).capabilities().dm_thread_is_channel is True


@pytest.mark.asyncio
async def test_an_owner_paired_while_the_receiver_runs_gets_the_next_approval_prompt(monkeypatch):
    monkeypatch.setattr(transport_mod, "HTTPDiscordAPI", _FakeAPI)
    monkeypatch.setattr(transport_mod, "DiscordGateway", _FakeGateway)
    transport = create_provider({})
    await transport.start_inbound(_Services())
    try:
        api = transport._api
        assert await transport._delivery.request_approval(_Event("before"), source="tool") is None, (
            "vacuity floor: with no owner, there is nobody to prompt"
        )

        # The Configure page mints the code; the owner sends it to the bot.
        code = channel_trust.create_owner_pairing_code("discord")
        await transport._on_message_create(_dm(code, message_id="701"))

        assert credentials.get_credential(_OWN) == OWNER
        assert api.sent[-1]["channel_id"] == f"dm-{OWNER}"
        assert "owner" in api.sent[-1]["content"], "the bot says the pairing made them the owner"

        # No restart: the running receiver's approval prompt goes to the owner just paired.
        task = asyncio.ensure_future(transport._delivery.request_approval(_Event("after"), source="tool"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        prompt = api.sent[-1]
        assert prompt["channel_id"] == f"dm-{OWNER}" and prompt["components"], (
            "no approval prompt reached the owner paired after the receiver started"
        )
        buttons = prompt["components"][0]["components"]
        assert {b["custom_id"] for b in buttons} == {"approve:after", "deny:after"}
        await transport._delivery.resolve_interaction(
            {"type": INTERACTION_TYPE_COMPONENT, "id": "i1", "token": "t1", "data": {"custom_id": "approve:after"},
             "user": {"id": OWNER}}
        )
        assert await asyncio.wait_for(task, timeout=1.0) is True
    finally:
        await transport.stop_inbound()
