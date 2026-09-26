"""Telegram keeps its owner under its own key, and reads that one back.

Slack, Telegram and Discord all wrote the owner's user id under the one shared
``PERSONALCLAW_OWNER_ID``. Setting up a second channel replaced the first one's owner with an id
from another platform, and every channel DMed and trusted whichever platform's id was saved last.
Core keys an owner per channel now (``owner_id_credential``, read with ``owner_id_for``), and
Telegram writes and reads its own. An install from an earlier release, with the owner only under
the shared key, moves it to Telegram's own key the first time the channel starts. Driven through
the real setup step, doctor probe and transport, against the real credential store in this
test's home.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from types import SimpleNamespace

import pytest

from personalclaw.config import credentials
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.channel import AppConfig, owner_id_credential
from personalclaw.sdk.cli import SetupContext

import cli_doctor
import cli_setup
import telegram_runtime.settings as settings_mod
import telegram_runtime.transport as transport_mod
from telegram_runtime.settings import CRED_TELEGRAM_BOT_TOKEN
from telegram_runtime.transport import create_provider

_APP = "telegram-channel"
#: Telegram's own owner key, by the name core files this channel's delivery under and
#: reads its owner by.
_OWN = owner_id_credential("telegram")
#: The key every channel used to write. Named literally: nothing in the app writes it now.
_SHARED = "PERSONALCLAW_OWNER_ID"
#: Another channel's owner under the shared key: a Slack member id, meaningless on Telegram.
_SLACK_OWNER = "U0SLACKOWNER"
#: A fake token, made at run time as this app's other tests make it.
TOKEN = f"123456:{secrets.token_hex(12)}"


@pytest.fixture(autouse=True)
def _keys_restored(monkeypatch):
    """``save_credential`` mirrors into the process environment, so register every key it may
    set here: teardown then restores each one for the next test."""
    for key in (_OWN, _SHARED, CRED_TELEGRAM_BOT_TOKEN):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _run_setup(answers: list[str]) -> None:
    """``cli_setup:run`` with the context core's setup runner builds for it."""
    replies = iter(answers)
    cli_setup.run(
        SetupContext(
            app_name=_APP,
            get_credential=credentials.get_credential,
            save_credential=credentials.save_credential,
            settings=ProviderSettings,
            print=lambda *_: None,
            input=lambda _prompt: next(replies, ""),
        )
    )


def test_setup_keeps_the_owner_under_telegrams_own_key():
    assert _OWN == "PERSONALCLAW_OWNER_ID_TELEGRAM"
    credentials.save_credential(_SHARED, _SLACK_OWNER)  # Slack's owner, from an earlier release

    _run_setup(["y", TOKEN, "424242", ""])

    assert credentials.get_credential(_OWN) == "424242"
    assert credentials.get_credential(_SHARED) == _SLACK_OWNER, "setup replaced Slack's owner"


def test_doctor_reports_telegrams_own_owner_and_names_its_key_when_unset(monkeypatch):
    monkeypatch.setenv(CRED_TELEGRAM_BOT_TOKEN, TOKEN)

    assert {line.label: line.detail for line in cli_doctor.probe()}["owner"] == f"{_OWN} not set"

    credentials.save_credential(_OWN, "424242")
    lines = {line.label: (line.status, line.detail) for line in cli_doctor.probe()}
    assert lines["owner"] == ("ok", "424242")


class _FakeAPI:
    """``HTTPTelegramAPI`` stand-in: no network, and a long poll that waits to be stopped."""

    def __init__(self, token: str, **_kw) -> None:
        self.token = token

    async def get_updates(self, **_kw):
        await asyncio.sleep(3600)

    async def close(self) -> None:
        pass


async def _what_the_transport_registers(monkeypatch) -> tuple[str, str]:
    """Start inbound offline. Returns the name the delivery handle is filed under with core,
    which is the name core reads the owner by, and the owner the handle DMs."""
    monkeypatch.setattr(transport_mod, "HTTPTelegramAPI", _FakeAPI)
    registered = []
    # What the gateway hands a transport: its config, and ``owner_id``, the SHARED key's value.
    services = SimpleNamespace(
        config=AppConfig.load(),
        owner_id=credentials.get_credential(_SHARED),
        register_channel_delivery=lambda delivery, provider="": registered.append(
            (provider, delivery._owner_id)
        ),
        dashboard_state=None,
    )
    transport = create_provider({})
    await transport.start_inbound(services)
    try:
        assert len(registered) == 1, "inbound started without registering one delivery handle"
        return registered[0]
    finally:
        await transport.stop_inbound()


def _adoption_log(caplog) -> list[str]:
    """What the adoption logged: the lines from the module that stores the owner."""
    return [r.getMessage() for r in caplog.records if r.name == settings_mod.__name__]


@pytest.mark.asyncio
async def test_the_transport_files_its_delivery_under_telegrams_own_owner(monkeypatch, caplog):
    """Core files the delivery under "telegram" and reads Telegram's own owner by it, and the
    handle DMs that owner, not the Slack id under the shared key. Its own key is left alone."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv(CRED_TELEGRAM_BOT_TOKEN, TOKEN)
    credentials.save_credential(_SHARED, _SLACK_OWNER)
    credentials.save_credential(_OWN, "424242")

    assert await _what_the_transport_registers(monkeypatch) == ("telegram", "424242")
    assert credentials.get_credential(_OWN) == "424242"
    assert _adoption_log(caplog) == [], "an install with its own key was migrated again"


def test_setup_keeps_an_earlier_releases_owner_under_the_own_key(monkeypatch):
    """Only the shared key, holding this bot's owner, as an earlier setup left it, and no start
    since. Core falls back to it, so the doctor still sees the owner, and running setup with Enter
    stores it under Telegram's own key."""
    monkeypatch.setenv(CRED_TELEGRAM_BOT_TOKEN, TOKEN)
    credentials.save_credential(_SHARED, "424242")

    assert {line.label: line.detail for line in cli_doctor.probe()}["owner"] == "424242"

    _run_setup(["y", "", "", ""])

    assert credentials.get_credential(_OWN) == "424242"


@pytest.mark.asyncio
async def test_an_install_from_an_earlier_release_adopts_its_owner_once(monkeypatch, caplog):
    """The first start stores the owner the shared key holds under Telegram's own key, so it
    outlives core's fallback. The next start finds it there and writes nothing, the owner the
    channel DMs never changes, and the log names the key, never the id."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv(CRED_TELEGRAM_BOT_TOKEN, TOKEN)
    credentials.save_credential(_SHARED, "424242")

    for _gateway_start in range(2):
        assert await _what_the_transport_registers(monkeypatch) == ("telegram", "424242")
        assert credentials.get_credential(_OWN) == "424242"

    logged = _adoption_log(caplog)
    assert len(logged) == 1, f"stored {len(logged)} times: {logged}"
    assert _OWN in logged[0] and "424242" not in logged[0]
    assert credentials.get_credential(_SHARED) == "424242", "the shared key was changed"


@pytest.mark.asyncio
async def test_a_channel_without_a_token_adopts_its_owner_too():
    """Inbound stays offline without a token and the owner is still stored, so a token saved
    later starts a channel that already knows its owner."""
    credentials.save_credential(_SHARED, "424242")

    await create_provider({}).start_inbound(object())  # no token: returns before using services

    assert credentials.get_credential(_OWN) == "424242"


@pytest.mark.asyncio
async def test_an_install_with_no_owner_starts_as_before(monkeypatch, caplog):
    """No owner anywhere: the channel starts with no owner to DM, as it always has, and stores
    and logs nothing about one."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv(CRED_TELEGRAM_BOT_TOKEN, TOKEN)

    assert await _what_the_transport_registers(monkeypatch) == ("telegram", "")

    assert credentials.get_credential(_OWN) == ""
    assert credentials.get_credential(_SHARED) == ""
    assert _adoption_log(caplog) == []
