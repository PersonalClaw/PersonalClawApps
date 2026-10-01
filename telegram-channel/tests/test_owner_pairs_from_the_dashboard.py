"""The owner pairs Telegram from the dashboard, and the running receiver reaches them at once.

Telegram's owner id — who core DMs results and approval prompts to — was written only by
``personalclaw setup``. The channel's Configure page now mints a code (core's
``create_owner_pairing_code``); the owner sends it to the bot in a DM, the message crosses core's
guarded door, and core stores the sender under Telegram's own owner key. For that to reach anyone,
Telegram has to declare it can pair (``owner_pairing``) and read its owner when it needs it: the
delivery used to keep the owner it saw when the receiver started, so an owner paired afterwards got
no approval prompt until a restart.

Driven through the real transport, the real core door and the real credential store in this test's
home; only the Bot API is a fake.
"""

from __future__ import annotations

import asyncio
import secrets

import pytest

from personalclaw import channel_trust
from personalclaw.config import credentials
from personalclaw.sdk.channel import owner_id_credential

import telegram_runtime.transport as transport_mod
from telegram_runtime.api import TelegramAPI
from telegram_runtime.settings import CRED_TELEGRAM_BOT_TOKEN
from telegram_runtime.transport import create_provider

_OWN = owner_id_credential("telegram")
_SHARED = "PERSONALCLAW_OWNER_ID"
TOKEN = f"123456:{secrets.token_hex(12)}"
OWNER = 4242


@pytest.fixture(autouse=True)
def _keys_restored(monkeypatch):
    for key in (_OWN, _SHARED, CRED_TELEGRAM_BOT_TOKEN):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv(CRED_TELEGRAM_BOT_TOKEN, TOKEN)


class _FakeAPI(TelegramAPI):
    """The Bot API: records what the bot sends; the long poll waits to be stopped."""

    def __init__(self, token: str, **_kw) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self._mid = 0

    async def get_me(self):
        return {"id": 1, "username": "bot"}

    async def get_updates(self, offset=0, timeout=50, allowed_updates=None):
        await asyncio.sleep(3600)
        return []

    async def send_message(self, chat_id, text, *, parse_mode=None, reply_to_message_id=None,
                           reply_markup=None, disable_web_page_preview=None):
        self._mid += 1
        self.sent.append({"chat_id": str(chat_id), "text": text, "reply_markup": reply_markup})
        return {"message_id": self._mid}

    async def edit_message_text(self, chat_id, message_id, text, *, parse_mode=None,
                                reply_markup=None, disable_web_page_preview=None):
        self.edits.append({"chat_id": str(chat_id), "text": text})
        return {"message_id": message_id}

    async def delete_message(self, chat_id, message_id):
        return True

    async def send_document(self, chat_id, file_path, *, caption=None, reply_to_message_id=None):
        return {"message_id": 0}

    async def send_photo(self, chat_id, file_path, *, caption=None, reply_to_message_id=None):
        return {"message_id": 0}

    async def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False):
        return True

    async def close(self) -> None:
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


def _dm(text: str, message_id: int) -> dict:
    return {
        "message_id": message_id, "date": 1700000000, "text": text,
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "first_name": "Dana"},
    }


def test_telegram_says_its_owner_can_pair_from_the_dashboard():
    assert create_provider({}).capabilities().owner_pairing is True


def test_telegram_says_a_dm_is_one_conversation_so_a_handoff_continues_there():
    """Core links a handed-off chat to the DM itself only when the channel says a DM's thread is
    its channel id — which this transport's inbound mapping does for every DM message."""
    assert create_provider({}).capabilities().dm_thread_is_channel is True


@pytest.mark.asyncio
async def test_an_owner_paired_while_the_receiver_runs_gets_the_next_approval_prompt(monkeypatch):
    monkeypatch.setattr(transport_mod, "HTTPTelegramAPI", _FakeAPI)
    transport = create_provider({})
    await transport.start_inbound(_Services())
    try:
        api = transport._api
        assert await transport._delivery.request_approval(_Event("before"), source="tool") is None, (
            "vacuity floor: with no owner, there is nobody to prompt"
        )

        # The Configure page mints the code; the owner sends it to the bot.
        code = channel_trust.create_owner_pairing_code("telegram")
        await transport._on_message(_dm(code, message_id=11))

        assert credentials.get_credential(_OWN) == str(OWNER)
        assert api.sent[-1]["chat_id"] == str(OWNER)
        assert "owner" in api.sent[-1]["text"], "the bot says the pairing made them the owner"

        # No restart: the running receiver's approval prompt goes to the owner just paired.
        task = asyncio.ensure_future(transport._delivery.request_approval(_Event("after"), source="tool"))
        await asyncio.sleep(0)
        prompt = api.sent[-1]
        assert prompt["chat_id"] == str(OWNER) and prompt["reply_markup"], (
            "no approval prompt reached the owner paired after the receiver started"
        )
        assert [row[0]["callback_data"] for row in prompt["reply_markup"]["inline_keyboard"]] == [
            "a0:after", "a1:after",
        ]
        await transport._delivery.resolve_callback({"id": "cb", "data": "a0:after", "from": {"id": OWNER}})
        assert await asyncio.wait_for(task, timeout=1.0) is True
    finally:
        await transport.stop_inbound()
