"""Slack keeps its owner under its own key, and reads that one back.

Slack, Telegram and Discord all wrote the owner's user id under the one shared
``PERSONALCLAW_OWNER_ID``. Setting up a second channel replaced the first one's owner with an id
from another platform, and every channel DMed and trusted whichever platform's id was saved last.
Core keys an owner per channel now (``owner_id_credential``, read with ``owner_id_for``), and
Slack writes and reads its own: from setup, from the first-contact claim, in the doctor probe and
in the runtime that decides who may talk. An install from an earlier release, with the owner only
under the shared key, moves it to Slack's own key the first time the channel starts. Driven
against the real credential store in this test's home.
"""

from __future__ import annotations

import logging
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
import slack_runtime.events as events_mod
import slack_runtime.handler as H
import slack_runtime.interactions as interactions_mod
import slack_runtime.settings as settings_mod
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


class _FakeSocketClient:
    """``SocketModeClient`` stand-in: holds the listener the real wiring attaches, no network."""

    def __init__(self, **_kw) -> None:
        self.socket_mode_request_listeners: list = []

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.fixture
def slack_offline(monkeypatch):
    """The real transport and the real Socket Mode wiring, with the network cut out: the
    workspace check passes and the Slack clients are stand-ins. The module state that wiring sets
    is restored afterwards, so no later test sees it."""
    monkeypatch.setenv(CRED_SLACK_BOT_TOKEN, BOT)
    monkeypatch.setenv(CRED_SLACK_APP_TOKEN, APP_TOKEN)
    monkeypatch.setattr(transport_mod, "RealSlackClient", lambda token: SimpleNamespace())
    monkeypatch.setattr(events_mod, "validate_enterprise", lambda *a, **k: True)
    monkeypatch.setattr(events_mod, "AsyncWebClient", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(events_mod, "WSSocketModeClient", _FakeSocketClient)
    for name in ("_gateway_services", "_orch_cfg"):
        monkeypatch.setattr(H, name, getattr(H, name))
    for name in ("global_enabled", "auto_speak", "auto_reply_to_voice"):
        monkeypatch.setattr(H._vc, name, getattr(H._vc, name))
    monkeypatch.setattr(interactions_mod, "_orch", interactions_mod._orch)


async def _start(registered: list) -> None:
    """One gateway start of the channel: build the transport, start inbound, stop it again.
    ``registered`` collects what the delivery handle is filed under with core, and the owner it
    DMs. The Slack handler keeps the owner the start wired in, for the caller to check."""
    services = SimpleNamespace(
        config=AppConfig.load(),
        # What the gateway hands a transport: ``owner_id`` is the SHARED key's value.
        owner_id=credentials.get_credential(_SHARED),
        register_channel_delivery=lambda delivery, provider="": registered.append(
            (provider, delivery._owner_id)
        ),
        dashboard_state=None,
        channel_history=None,
    )
    transport = transport_mod.create_provider({})
    await transport.start_inbound(services)
    await transport.stop_inbound()


def _adoption_log(caplog) -> list[str]:
    """What the adoption logged: the lines from the module that stores the owner."""
    return [r.getMessage() for r in caplog.records if r.name == settings_mod.__name__]


def _claim_mode_warned(caplog) -> bool:
    return any("owner-claim mode" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_transport_files_its_delivery_under_slacks_own_owner(slack_offline, caplog):
    """Core files the delivery under "slack" and reads Slack's own owner by it, and the handle
    DMs that owner, not the Telegram id under the shared key. Its own key is left alone."""
    caplog.set_level(logging.INFO)
    credentials.save_credential(_SHARED, _TELEGRAM_OWNER)
    credentials.save_credential(_OWN, "U0SLACKOWNER")
    registered: list = []

    await _start(registered)

    assert registered == [("slack", "U0SLACKOWNER")]
    assert H.get_owner_id() == "U0SLACKOWNER"
    assert credentials.get_credential(_OWN) == "U0SLACKOWNER"
    assert _adoption_log(caplog) == [], "an install with its own key was migrated again"


@pytest.mark.asyncio
async def test_an_earlier_release_install_adopts_its_owner_once_and_stays_closed(
    slack_offline, caplog
):
    """Only the shared key, holding this workspace's member id, as an earlier setup left it.
    The first start stores it under Slack's own key and later starts write nothing. Every start
    keeps that owner, so Slack never starts in first-contact claim mode and a stranger's first DM
    claims nothing. The log names the key, never the id."""
    caplog.set_level(logging.INFO)
    credentials.save_credential(_SHARED, "U0SLACKOWNER")

    for _gateway_start in range(2):
        registered: list = []
        await _start(registered)
        assert registered == [("slack", "U0SLACKOWNER")]
        assert H.get_owner_id() == "U0SLACKOWNER"
        assert credentials.get_credential(_OWN) == "U0SLACKOWNER"

    assert not _claim_mode_warned(caplog), "Slack started waiting for a first sender to claim it"
    assert H.claim_owner("U0STRANGER") is False
    assert H.is_allowed_user("U0STRANGER") is False
    logged = _adoption_log(caplog)
    assert len(logged) == 1, f"stored {len(logged)} times: {logged}"
    assert _OWN in logged[0] and "U0SLACKOWNER" not in logged[0]
    assert credentials.get_credential(_SHARED) == "U0SLACKOWNER", "the shared key was changed"


@pytest.mark.asyncio
async def test_an_install_with_no_owner_starts_as_before(slack_offline, caplog):
    """No owner anywhere: Slack starts in first-contact claim mode as it always has, stores no
    owner and logs nothing about one, and the first sender's claim still works."""
    caplog.set_level(logging.INFO)
    registered: list = []

    await _start(registered)

    assert registered == [("slack", "")]
    assert H.get_owner_id() == ""
    assert _claim_mode_warned(caplog)
    assert credentials.get_credential(_OWN) == ""
    assert credentials.get_credential(_SHARED) == ""
    assert _adoption_log(caplog) == []
    assert H.claim_owner("U0FIRST") is True
    assert credentials.get_credential(_OWN) == "U0FIRST"


@pytest.mark.asyncio
async def test_a_slack_without_tokens_adopts_its_owner_too():
    """Inbound stays offline without tokens and the owner is still stored, so tokens saved later
    start a Slack that already knows its owner, not one waiting for a first sender."""
    credentials.save_credential(_SHARED, "U0SLACKOWNER")
    transport = transport_mod.create_provider({})

    await transport.start_inbound(SimpleNamespace(config=AppConfig.load()))

    assert transport._runtime is None, "inbound started without tokens"
    assert credentials.get_credential(_OWN) == "U0SLACKOWNER"


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
