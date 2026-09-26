"""A Bot Token saved on the Apps page reaches the inbox source without a restart.

The registry builds the ``inbox`` provider once, from the app store as it is when the app is
enabled, and the Apps page's Configure → Save (``PUT /api/apps/{name}/config``) re-cycles nothing.
The inbox source built its Slack client in ``__init__`` from the token it was handed then, so after
a Save it went on polling, replying and reacting with the token it started with (none, on an
install configured after enable) until the gateway restarted. #124 moved the channel transport onto
the live store. This moves the inbox half onto the same ``LiveConfig`` and ``load_tokens``, so the
two providers cannot disagree about which token is in effect.

Saves go through core's own route handler. Nothing opens a socket: ``RealSlackClient`` is replaced
where the inbox source builds it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import slack_runtime.inbox_source as inbox_mod
from personalclaw.dashboard.handlers.apps import api_app_config_put
from personalclaw.providers.settings import ProviderSettings
from slack_runtime.inbox_source import create_provider

_APP = "slack-channel"
_BUNDLE = Path(__file__).resolve().parents[1]


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A scratch home with this app installed and nothing configured."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    home_app = tmp_path / "apps" / _APP
    home_app.mkdir(parents=True)
    shutil.copy(_BUNDLE / "app.json", home_app / "app.json")
    return tmp_path


class _FakeClient:
    """``RealSlackClient`` stand-in: records the token each client was built with and used."""

    built_with: list[str] = []
    used: list[tuple[str, str]] = []

    def __init__(self, token: str) -> None:
        self.token = token
        _FakeClient.built_with.append(token)

    async def fetch_history(self, channel, oldest, limit=200):
        _FakeClient.used.append(("fetch_history", self.token))
        return [{"ts": "1700000001.000100", "user": "U_ALICE", "text": "hello"}]

    async def get_user_info(self, user_id):
        _FakeClient.used.append(("get_user_info", self.token))
        return {"real_name": "Alice Example"}

    async def post_message(self, channel, text, thread_ts=None, **_kw):
        _FakeClient.used.append(("post_message", self.token))
        return "1700000099.000000"

    async def add_reaction(self, channel, ts, emoji):
        _FakeClient.used.append(("add_reaction", self.token))


@pytest.fixture
def fake_client(monkeypatch):
    _FakeClient.built_with, _FakeClient.used = [], []
    monkeypatch.setattr(inbox_mod, "RealSlackClient", _FakeClient)
    return _FakeClient


async def _configure_save(values: dict) -> None:
    """The Apps page's Configure → Save: core's PUT /api/apps/{name}/config handler."""

    class _Request(dict):
        match_info = {"name": _APP}

        async def json(self):
            return values

    resp = await api_app_config_put(_Request())
    assert resp.status == 200, resp.text


def _registry_built():
    """What core's ``InboxTypeHandler.create`` does when the app is enabled."""
    return create_provider(ProviderSettings.load(_APP))


async def _poll(source):
    return await source.poll(["C1"], {}, "U_ME")


@pytest.mark.asyncio
async def test_a_token_saved_after_enable_is_the_one_the_next_poll_uses(installed, fake_client):
    source = _registry_built()

    await _configure_save({"bot_token": "bot-token-saved"})
    messages, _ = await _poll(source)

    assert [m.text for m in messages] == ["hello"]
    assert {token for _, token in fake_client.used} == {"bot-token-saved"}, fake_client.used


@pytest.mark.asyncio
async def test_a_rotated_token_is_the_one_replies_and_reactions_use(installed, fake_client):
    await _configure_save({"bot_token": "bot-token-old"})
    source = _registry_built()
    await _poll(source)

    await _configure_save({"bot_token": "bot-token-new"})
    assert await source.send_reply("C1", "ack", "1700000001.000100") is True
    assert await source.add_reaction("C1", "1700000001.000100", "eyes") is True

    assert fake_client.used[-2:] == [("post_message", "bot-token-new"), ("add_reaction", "bot-token-new")]


@pytest.mark.asyncio
async def test_an_unchanged_token_keeps_one_client(installed, fake_client):
    await _configure_save({"bot_token": "bot-token-saved"})
    source = _registry_built()

    await _poll(source)
    await _poll(source)
    await source.send_reply("C1", "ack")

    assert fake_client.built_with == ["bot-token-saved"]


@pytest.mark.asyncio
async def test_the_shared_credential_is_the_fallback_for_an_unset_token(
    installed, fake_client, monkeypatch
):
    """The same order as the transport: the app's store, then the name the gateway exports."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "bot-token-shared")
    await _poll(_registry_built())

    assert {token for _, token in fake_client.used} == {"bot-token-shared"}


@pytest.mark.asyncio
async def test_an_explicit_config_is_not_replaced_by_an_unchanged_store(installed, fake_client):
    """A source built with its own config keeps it while the machine's store stays as it was."""
    data = installed / "apps" / _APP / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(json.dumps({"bot_token": "bot-token-machine"}), encoding="utf-8")

    await _poll(create_provider({"bot_token": "bot-token-explicit"}))

    assert {token for _, token in fake_client.used} == {"bot-token-explicit"}
