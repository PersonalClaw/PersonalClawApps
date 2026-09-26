"""Slack keeps its owner under its own key, and reads that one back.

Slack, Telegram and Discord all wrote the owner's user id under the one shared
``PERSONALCLAW_OWNER_ID``. Setting up a second channel replaced the first one's owner with an id
from another platform, and every channel DMed and trusted whichever platform's id was saved last.
Core keys an owner per channel now (``owner_id_credential``, read with ``owner_id_for``), and
Slack writes and reads its own: from setup, from the first-contact claim, in the doctor probe and
in the runtime that decides who may talk. Driven against the real credential store in this test's
home.
"""

from __future__ import annotations

import secrets
from types import SimpleNamespace

import pytest

from personalclaw.config import credentials
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.channel import (
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    AppConfig,
    owner_id_credential,
)
from personalclaw.sdk.cli import SetupContext

import cli_doctor
import cli_setup
import slack_runtime.handler as H
import slack_runtime.transport as transport_mod
from slack_runtime.runtime import SlackRuntime

_APP = "slack-channel"
#: Slack's own owner key, by the name core files this channel's delivery under and
#: reads its owner by.
_OWN = owner_id_credential("slack")
#: The key every channel used to write. Named literally: nothing in the app writes it now.
_SHARED = "PERSONALCLAW_OWNER_ID"
#: Another channel's owner under the shared key: a Telegram user id, meaningless on Slack.
_TELEGRAM_OWNER = "424242"
#: Fake tokens, made at run time as this app's other tests make them.
BOT = f"xoxb-{secrets.token_hex(12)}"
APP_TOKEN = f"xapp-1-{secrets.token_hex(12)}"


@pytest.fixture(autouse=True)
def _keys_restored(monkeypatch):
    """``save_credential`` mirrors into the process environment, so register every key it may
    set here: teardown then restores each one for the next test."""
    for key in (_OWN, _SHARED, CRED_SLACK_BOT_TOKEN, CRED_SLACK_APP_TOKEN):
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


def _runtime() -> SlackRuntime:
    """The runtime built the way the transport builds it, from what the gateway hands a
    transport: its config, and ``owner_id``, the SHARED key's value."""
    services = SimpleNamespace(
        config=AppConfig.load(), owner_id=credentials.get_credential(_SHARED)
    )
    return SlackRuntime(services, config={})


def test_setup_keeps_the_owner_under_slacks_own_key():
    assert _OWN == "PERSONALCLAW_OWNER_ID_SLACK"
    credentials.save_credential(_SHARED, _TELEGRAM_OWNER)  # Telegram's, from an earlier release

    _run_setup(["y", APP_TOKEN, BOT, "U0SLACKOWNER", ""])

    assert credentials.get_credential(_OWN) == "U0SLACKOWNER"
    assert credentials.get_credential(_SHARED) == _TELEGRAM_OWNER, "setup replaced Telegram's owner"


def test_the_first_contact_claim_is_kept_under_slacks_own_key():
    credentials.save_credential(_SHARED, _TELEGRAM_OWNER)

    assert H.claim_owner("U0SLACKOWNER") is True

    assert credentials.get_credential(_OWN) == "U0SLACKOWNER"
    assert (
        credentials.get_credential(_SHARED) == _TELEGRAM_OWNER
    ), "the claim replaced Telegram's owner"


def test_doctor_reports_slacks_own_owner_and_names_its_key_when_unset(monkeypatch):
    monkeypatch.setenv(CRED_SLACK_BOT_TOKEN, BOT)
    monkeypatch.setenv(CRED_SLACK_APP_TOKEN, APP_TOKEN)

    assert {line.label: line.detail for line in cli_doctor.probe()}["owner"] == f"{_OWN} not set"

    credentials.save_credential(_OWN, "U0SLACKOWNER")
    lines = {line.label: (line.status, line.detail) for line in cli_doctor.probe()}
    assert lines["owner"] == ("ok", "U0SLACKOWNER")


def test_the_runtime_trusts_slacks_own_owner():
    """Who may talk: Slack's own owner, not the Telegram id under the shared key."""
    credentials.save_credential(_SHARED, _TELEGRAM_OWNER)
    credentials.save_credential(_OWN, "U0SLACKOWNER")

    runtime = _runtime()

    assert runtime._owner_id == "U0SLACKOWNER"
    assert runtime._allowed_users == {"U0SLACKOWNER"}


class _FakeSocket:
    """The Socket Mode client stand-in: connects at once, no network."""

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_the_transport_files_its_delivery_under_slacks_own_owner(monkeypatch):
    """Core files the delivery under "slack" and reads Slack's own owner by it, and the handle
    DMs that owner, not the Telegram id under the shared key."""
    monkeypatch.setenv(CRED_SLACK_BOT_TOKEN, BOT)
    monkeypatch.setenv(CRED_SLACK_APP_TOKEN, APP_TOKEN)
    monkeypatch.setattr(transport_mod, "RealSlackClient", lambda token: SimpleNamespace())
    monkeypatch.setattr(transport_mod, "init_interactions", lambda runtime: None)
    monkeypatch.setattr(
        transport_mod,
        "init_socket_mode",
        lambda runtime, seen: setattr(runtime, "_socket_client", _FakeSocket()),
    )
    credentials.save_credential(_SHARED, _TELEGRAM_OWNER)
    credentials.save_credential(_OWN, "U0SLACKOWNER")
    registered = []
    services = SimpleNamespace(
        config=AppConfig.load(),
        owner_id=_TELEGRAM_OWNER,
        register_channel_delivery=lambda delivery, provider="": registered.append(
            (provider, delivery._owner_id)
        ),
        dashboard_state=None,
        channel_history=None,
    )

    transport = transport_mod.create_provider({})
    await transport.start_inbound(services)
    try:
        assert registered == [("slack", "U0SLACKOWNER")]
    finally:
        await transport.stop_inbound()


def test_an_install_from_an_earlier_release_keeps_its_owner(monkeypatch):
    """Only the shared key, holding this workspace's member id, as an earlier setup left it.
    Core falls back to it, so the doctor and the runtime still see the owner, and running setup
    again with Enter keeps it and stores it under Slack's own key."""
    monkeypatch.setenv(CRED_SLACK_BOT_TOKEN, BOT)
    monkeypatch.setenv(CRED_SLACK_APP_TOKEN, APP_TOKEN)
    credentials.save_credential(_SHARED, "U0SLACKOWNER")

    assert {line.label: line.detail for line in cli_doctor.probe()}["owner"] == "U0SLACKOWNER"
    assert _runtime()._owner_id == "U0SLACKOWNER"

    _run_setup(["y", "", "", "", ""])

    assert credentials.get_credential(_OWN) == "U0SLACKOWNER"
