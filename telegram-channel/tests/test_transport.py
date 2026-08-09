"""TelegramTransport — capabilities, ChannelMessage mapping, trust hooks, offset.

Trust is exercised against the REAL core seam (``channel_trust``) writing into the
isolated tmp home (conftest sets ``PERSONALCLAW_HOME``), so the DM-pairing /
group-tracked-only / fencing behaviour is the exact contract core enforces — not a
re-implementation. Routing into a session is faked (a stand-in dashboard state +
a patched ``_run_chat``) so the test observes what text reaches the session."""

from __future__ import annotations

import asyncio

import pytest

from personalclaw.sdk.channel import allow_sender, track
from telegram_runtime.transport import TelegramTransport, create_provider


class FakeDelivery:
    def __init__(self):
        self.texts: list[tuple[str, str]] = []

    async def deliver_text(self, channel, text, *a, **k):
        self.texts.append((channel, text))
        return "1"


class FakeSession:
    def __init__(self, key="telegram-1"):
        self.key = key
        self.running = False
        self.task = None
        self.appended: list[tuple] = []

    def append(self, role, text, cls):
        self.appended.append((role, text, cls))


class FakeState:
    def __init__(self):
        self.session = FakeSession()
        self.linked: dict = {}
        self._background_tasks: set = set()
        self.notified: list = []

    def get_linked_session(self, thread_key):
        return self.linked.get(thread_key)

    def get_or_create_session(self, app=""):
        self.linked_app = app
        return self.session

    def link_channel(self, key, thread_key, channel_id):
        self.linked[thread_key] = self.session

    def notify(self, *a, **k):
        self.notified.append((a, k))


class FakeServices:
    def __init__(self, state):
        self.dashboard_state = state


def _msg(text="hi", chat_id="500", chat_type="private", from_id="42", first="Ada"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 9,
            "date": 1700000000,
            "text": text,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": from_id, "first_name": first, "username": "ada"},
        },
    }


@pytest.fixture
def transport_with_capture(monkeypatch):
    """A transport wired to a fake state + delivery, with _run_chat captured."""
    captured: dict = {}

    async def fake_run_chat(state, session, text, *a, **k):
        captured["state"] = state
        captured["session"] = session
        captured["text"] = text

    monkeypatch.setattr("personalclaw.sdk.channel._run_chat", fake_run_chat)

    t = TelegramTransport({"bot_token": "TEST"})
    state = FakeState()
    t._services = FakeServices(state)
    t._delivery = FakeDelivery()
    return t, state, captured


class TestCapabilities:
    def test_honest_capabilities(self):
        c = TelegramTransport().capabilities()
        assert c.inbound is True
        assert c.threads is True
        assert c.attachments is True
        assert c.edits is True
        assert c.rich_text is True
        assert c.reactions is False  # Telegram bot reactions not implemented — honest
        assert c.typing_indicator is False
        assert c.max_text_len == 4096  # Bot API hard cap

    def test_name_and_display(self):
        t = TelegramTransport()
        assert t.name == "telegram"
        assert t.display_name == "Telegram"

    def test_create_provider_returns_transport(self):
        assert type(create_provider({})).__name__ == "TelegramTransport"

    def test_connected_from_token(self):
        assert TelegramTransport({"bot_token": "x"}).connected is True
        assert TelegramTransport({}).connected is False

    @pytest.mark.asyncio
    async def test_health_reflects_token(self):
        assert (await TelegramTransport({"bot_token": "x"}).health())["state"] == "ready"
        assert (await TelegramTransport({}).health())["state"] == "offline"


class TestChannelMessageMapping:
    def test_maps_fields(self):
        t = TelegramTransport()
        cm = t._to_channel_message(_msg(text="yo", chat_id="88", from_id="7")["message"])
        assert cm.channel_id == "88"
        assert cm.text == "yo"
        assert cm.sender == "7"
        assert cm.thread_id == "88"
        assert cm.message_id == "9"
        assert cm.metadata["chat_type"] == "private"
        assert cm.metadata["sender_name"] == "Ada"

    def test_caption_falls_back_for_text(self):
        t = TelegramTransport()
        m = _msg()["message"]
        del m["text"]
        m["caption"] = "a photo caption"
        cm = t._to_channel_message(m)
        assert cm.text == "a photo caption"

    def test_sender_name_falls_back_to_username(self):
        t = TelegramTransport()
        m = _msg()["message"]
        m["from"] = {"id": "3", "username": "nofirst"}
        cm = t._to_channel_message(m)
        assert cm.metadata["sender_name"] == "nofirst"


class TestTrustHooks:
    @pytest.mark.asyncio
    async def test_allowed_dm_routes_to_session(self, transport_with_capture):
        t, state, captured = transport_with_capture
        allow_sender("telegram", "42")  # owner/paired sender
        await t._on_message(_msg(text="hello", chat_id="500", from_id="42")["message"])
        await asyncio.sleep(0)
        assert captured.get("text") == "hello"
        assert state.linked_app == "telegram"

    @pytest.mark.asyncio
    async def test_unknown_dm_sender_gets_canned_reply_not_routed(self, transport_with_capture):
        t, state, captured = transport_with_capture
        # Default DM policy is "pairing"; an unknown sender is denied with the canned reply.
        await t._on_message(_msg(text="hi", chat_id="500", from_id="99")["message"])
        await asyncio.sleep(0)
        assert "text" not in captured  # never routed
        assert t._delivery.texts  # a canned reply was delivered
        assert "pairing code" in t._delivery.texts[-1][1]

    @pytest.mark.asyncio
    async def test_dm_activation_off_short_circuits(self, transport_with_capture):
        from telegram_runtime.settings import reload_settings
        from personalclaw.sdk.channel import ProviderSettings

        t, state, captured = transport_with_capture
        allow_sender("telegram", "42")
        ProviderSettings.save("telegram-channel", {"dm_activation": "off"})
        reload_settings()
        await t._on_message(_msg(text="hi", chat_id="500", from_id="42")["message"])
        await asyncio.sleep(0)
        assert "text" not in captured  # off → nothing routed
        assert t._delivery.texts == []  # and no reply

    @pytest.mark.asyncio
    async def test_tracked_group_routes_fenced_text(self, transport_with_capture):
        t, state, captured = transport_with_capture
        track("telegram", "-100777", "Team Room")
        await t._on_message(
            _msg(text="deploy now", chat_id="-100777", chat_type="supergroup", from_id="55")["message"]
        )
        await asyncio.sleep(0)
        routed = captured.get("text", "")
        assert "deploy now" in routed
        # non-owner group content is fenced before it enters the session
        assert "<untrusted_content" in routed

    @pytest.mark.asyncio
    async def test_untracked_group_is_dropped_silently(self, transport_with_capture):
        t, state, captured = transport_with_capture
        await t._on_message(
            _msg(text="spam", chat_id="-100000", chat_type="group", from_id="66")["message"]
        )
        await asyncio.sleep(0)
        assert "text" not in captured
        assert t._delivery.texts == []  # tracked_only drops silently, no owner spam

    @pytest.mark.asyncio
    async def test_empty_text_ignored(self, transport_with_capture):
        t, state, captured = transport_with_capture
        allow_sender("telegram", "42")
        m = _msg(chat_id="500", from_id="42")["message"]
        m["text"] = "   "
        await t._on_message(m)
        assert "text" not in captured


class TestPollOffset:
    @pytest.mark.asyncio
    async def test_dispatch_routes_callback_to_delivery(self, transport_with_capture):
        t, state, captured = transport_with_capture
        seen = {}

        async def resolve_callback(cq):
            seen["cq"] = cq

        t._delivery.resolve_callback = resolve_callback
        await t._dispatch({"update_id": 2, "callback_query": {"id": "c1", "data": "approve:r1"}})
        assert seen["cq"]["data"] == "approve:r1"

    def test_offset_persists_and_reloads(self):
        t = TelegramTransport({"bot_token": "x"})
        assert t._load_offset() == 0  # nothing saved yet
        t._save_offset(123)
        assert t._load_offset() == 123
