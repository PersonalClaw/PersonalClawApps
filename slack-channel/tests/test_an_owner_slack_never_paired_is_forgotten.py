"""An owner Slack never paired is forgotten when the channel starts, and setup names none.

An earlier release made the first person to message the bot its owner and stored them under
Slack's own key, ``PERSONALCLAW_OWNER_ID_SLACK``, or, before that, under the one key every channel
shared, ``PERSONALCLAW_OWNER_ID``, which the next release copied to Slack's own. ``personalclaw
setup`` stored a member id typed at the prompt the same way, and a container could set one in the
environment. None of those was confirmed from the account, and nothing tells one from another, so
Slack keeps only the owner core's owner pairing named. When the channel starts, any other owner core
holds for Slack is forgotten, with or without tokens, and the owner pairs once more from the
Configure page. Setup asks for no member id: it says how to pair. Another channel's owner left
under the shared key stays that channel's. Driven against the real credential and trust stores in
this test's home.
"""

from __future__ import annotations

import logging
import secrets
from types import SimpleNamespace

import pytest

from personalclaw import channel_trust
from personalclaw.config import credentials
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.channel import (
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    AppConfig,
    owner_id_credential,
    owner_id_for,
)
from personalclaw.sdk.cli import SetupContext

import cli_doctor
import cli_setup
import slack_runtime.allowlist as allowlist_mod
import slack_runtime.events as events_mod
import slack_runtime.handler as H
import slack_runtime.interactions as interactions_mod
import slack_runtime.transport as transport_mod
from slack_runtime.enterprise import VALIDATED, WorkspaceCheck

_APP = "slack-channel"
_OWN = owner_id_credential("slack")
#: The key every channel used to write. Named literally: nothing in the app writes it now.
_SHARED = "PERSONALCLAW_OWNER_ID"
#: Another channel's owner under the shared key: a Telegram user id, nobody on Slack.
_TELEGRAM_OWNER = "424242"
OWNER = "U0NOOROWNER"
CLAIMED = "U0FIRSTSENDER"
BOT = f"xoxb-{secrets.token_hex(12)}"
APP_TOKEN = f"fake-app-token-1{secrets.token_hex(12)}"
_HOW_TO_PAIR = "Settings → Providers → Slack Channel → Configure → Pair as owner"


@pytest.fixture(autouse=True)
def _keys_restored(monkeypatch):
    """``save_credential`` mirrors into the process environment, so register every key it may
    set here: teardown then restores each one for the next test."""
    for key in (_OWN, _SHARED, owner_id_credential("telegram"), CRED_SLACK_BOT_TOKEN, CRED_SLACK_APP_TOKEN):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _pair(owner: str = OWNER) -> None:
    """The owner pairs Slack through core: the Configure page's code, sent in a DM."""
    code = channel_trust.create_owner_pairing_code("slack")
    assert channel_trust.redeem_owner_pairing_code("slack", owner, code, "Noor")


def _claimed_by_an_earlier_release(key: str = _OWN, who: str = CLAIMED) -> None:
    """What the first-contact claim left: the id under an owner key, and trusted as the owner."""
    credentials.save_credential(key, who)
    channel_trust.allow_sender("slack", who, "", via="owner")


def _run_setup(answers: list[str]) -> tuple[list[str], list[str]]:
    """``cli_setup:run`` with the context core's setup runner builds for it. Returns what it
    asked and what it printed."""
    replies = iter(answers)
    asked: list[str] = []
    printed: list[str] = []

    def _ask(prompt: str) -> str:
        asked.append(prompt)
        return next(replies, "")

    cli_setup.run(
        SetupContext(
            app_name=_APP,
            get_credential=credentials.get_credential,
            save_credential=credentials.save_credential,
            settings=ProviderSettings,
            print=lambda *parts: printed.append(" ".join(str(p) for p in parts)),
            input=_ask,
        )
    )
    return asked, printed


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
    monkeypatch.setattr(events_mod, "validate_enterprise", lambda *a, **k: WorkspaceCheck(VALIDATED))
    monkeypatch.setattr(events_mod, "AsyncWebClient", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(events_mod, "SocketModeReceiver", _FakeSocketClient)
    for name in ("_gateway_services", "_orch_cfg", "_allowed_users", "_tracking_channels"):
        monkeypatch.setattr(H, name, getattr(H, name))
    for name in ("global_enabled", "auto_speak", "auto_reply_to_voice"):
        monkeypatch.setattr(H._vc, name, getattr(H._vc, name))
    monkeypatch.setattr(interactions_mod, "_orch", interactions_mod._orch)


async def _start(registered: list) -> None:
    """One gateway start of the channel: build the transport, start inbound, stop it again.
    ``registered`` collects what the delivery handle is filed under with core, and the owner it
    DMs."""
    services = SimpleNamespace(
        config=AppConfig.load(),
        register_channel_delivery=lambda delivery, provider="": registered.append(
            (provider, delivery._owner())
        ),
        dashboard_state=None,
        channel_history=None,
    )
    transport = transport_mod.create_provider({})
    await transport.start_inbound(services)
    await transport.stop_inbound()


def _forget_log(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == allowlist_mod.__name__]


# ── setup and doctor ──────────────────────────────────────────────────────────────────────


def test_setup_asks_for_no_member_id_and_says_how_to_pair():
    credentials.save_credential(_SHARED, _TELEGRAM_OWNER)

    asked, printed = _run_setup(["y", APP_TOKEN, BOT, ""])

    assert not [p for p in asked if "Member ID" in p], f"setup asked for a member id: {asked}"
    assert any(_HOW_TO_PAIR in line for line in printed), printed
    assert credentials.get_credential(_OWN) == ""
    assert credentials.get_credential(_SHARED) == _TELEGRAM_OWNER, "setup changed Telegram's owner"
    assert ProviderSettings.load(_APP)["bot_token"] == BOT, "vacuity floor: setup saved the tokens"


def test_doctor_says_how_to_pair_when_slack_has_no_owner(monkeypatch):
    monkeypatch.setenv(CRED_SLACK_BOT_TOKEN, BOT)
    monkeypatch.setenv(CRED_SLACK_APP_TOKEN, APP_TOKEN)

    status, detail = {l.label: (l.status, l.detail) for l in cli_doctor.probe()}["owner"]

    assert status == "warn"
    assert detail == f"{_OWN} not set — pair one in the dashboard ({_HOW_TO_PAIR})"


def test_doctor_says_an_owner_nobody_paired_will_be_forgotten(monkeypatch):
    monkeypatch.setenv(CRED_SLACK_BOT_TOKEN, BOT)
    monkeypatch.setenv(CRED_SLACK_APP_TOKEN, APP_TOKEN)
    _claimed_by_an_earlier_release()

    status, detail = {l.label: (l.status, l.detail) for l in cli_doctor.probe()}["owner"]

    assert status == "warn"
    assert CLAIMED in detail and "never paired" in detail and _HOW_TO_PAIR in detail


def test_doctor_reports_the_paired_owner(monkeypatch):
    monkeypatch.setenv(CRED_SLACK_BOT_TOKEN, BOT)
    monkeypatch.setenv(CRED_SLACK_APP_TOKEN, APP_TOKEN)
    _pair()

    assert {l.label: (l.status, l.detail) for l in cli_doctor.probe()}["owner"] == ("ok", OWNER)


# ── the start ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_first_sender_an_earlier_release_made_the_owner_is_forgotten(slack_offline, caplog):
    caplog.set_level(logging.INFO)
    _claimed_by_an_earlier_release()
    assert owner_id_for("slack") == CLAIMED, "vacuity floor: core held the claimed owner"
    registered: list = []

    await _start(registered)

    assert owner_id_for("slack") == ""
    assert credentials.get_credential(_OWN) == ""
    assert not channel_trust.is_allowed_sender("slack", CLAIMED)
    assert registered == [("slack", "")]
    assert H.is_owner(CLAIMED) is False and H.is_allowed_user(CLAIMED) is False
    (logged,) = _forget_log(caplog)
    assert _HOW_TO_PAIR in logged and CLAIMED not in logged


@pytest.mark.asyncio
async def test_an_owner_kept_under_the_shared_key_is_forgotten_too(slack_offline):
    """The oldest releases kept the first sender under the shared key alone."""
    _claimed_by_an_earlier_release(_SHARED)

    await _start([])

    assert owner_id_for("slack") == ""
    assert credentials.get_credential(_OWN) == "", "the claim was copied to Slack's own key"


@pytest.mark.asyncio
async def test_another_channels_owner_under_the_shared_key_stays_that_channels(slack_offline):
    credentials.save_credential(_SHARED, _TELEGRAM_OWNER)

    await _start([])

    assert owner_id_for("slack") == ""
    assert credentials.get_credential(_SHARED) == _TELEGRAM_OWNER
    assert owner_id_for("telegram") == _TELEGRAM_OWNER


@pytest.mark.asyncio
async def test_an_owner_set_in_the_environment_is_not_slacks_owner(slack_offline, monkeypatch):
    for _gateway_start in range(2):
        monkeypatch.setenv(_OWN, "U0FROMENV")
        await _start([])
        assert owner_id_for("slack") == ""


@pytest.mark.asyncio
async def test_the_paired_owner_is_kept_at_every_start(slack_offline, caplog):
    caplog.set_level(logging.INFO)
    _pair()

    for _gateway_start in range(2):
        registered: list = []
        await _start(registered)
        assert registered == [("slack", OWNER)]
        assert H.get_owner_id() == OWNER

    assert _forget_log(caplog) == []
    assert channel_trust.is_allowed_sender("slack", OWNER)


@pytest.mark.asyncio
async def test_a_slack_without_tokens_forgets_an_unpaired_owner_too():
    """Inbound stays offline without tokens and the owner is still forgotten, so tokens saved
    later start a Slack whose owner is the one it pairs."""
    _claimed_by_an_earlier_release()
    transport = transport_mod.create_provider({})

    await transport.start_inbound(SimpleNamespace(config=AppConfig.load()))

    assert transport._runtime is None, "inbound started without tokens"
    assert owner_id_for("slack") == ""


@pytest.mark.asyncio
async def test_with_no_owner_the_start_says_how_to_pair(slack_offline, caplog):
    caplog.set_level(logging.INFO)

    await _start([])

    said = [r.getMessage() for r in caplog.records if r.name == events_mod.__name__]
    assert any("no owner" in m and _HOW_TO_PAIR in m for m in said), said
    assert not any("first user" in m or "claim" in m for m in said), said
