"""A Slack the gateway cannot reach as it starts is tried again, and the card says it could not reach it.

The gateway started before its network was up. The workspace check (``auth.test``) got "Connection
refused", and that turned Slack's inbound off for the life of the process: nothing asked again, and
the channel card said "auth.test rejected the Bot Token. Re-check it in Settings", about a token
that had worked all week. Only a restart brought inbound back.

A check that got no answer says nothing about the token. It is asked again with backoff, the card
says Slack could not be reached and when it tries next, and no message is accepted until a check
succeeds. Only Slack answering that it refuses the token turns inbound off, and only then does the
card name the token.

Driven through the real transport, the real workspace check and the real Socket Mode wiring. The
network is cut out at the two calls that would leave the machine: ``auth.test`` and the socket.
"""

from __future__ import annotations

import asyncio
import secrets
import urllib.error
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web import WebClient

from personalclaw.sdk.channel import CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN, AppConfig

import slack_runtime.enterprise as enterprise
import slack_runtime.events as events_mod
import slack_runtime.handler as H
import slack_runtime.interactions as interactions_mod
import slack_runtime.transport as transport_mod

BOT = f"fake-bot-token-{secrets.token_hex(8)}"
APP_TOKEN = f"fake-app-token-{secrets.token_hex(8)}"
TEAM = {"ok": True, "team": "Example", "team_id": "T0EXAMPLE"}


def _no_answer() -> Exception:
    """What urllib raises when nothing listens yet: the log line of the start that failed."""
    return urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


def _slack_says(error: str, status: int = 200) -> SlackApiError:
    class _Response(dict):
        status_code = status

    return SlackApiError("The request to the Slack API failed.", _Response(ok=False, error=error))


class _AuthTest:
    """``auth.test``: each call takes the next answer; the last one repeats."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls = 0

    def answer(self):
        self.calls += 1
        answer = self.answers[min(self.calls, len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def install(self, monkeypatch) -> "_AuthTest":
        monkeypatch.setattr(WebClient, "auth_test", lambda _client, **_kw: self.answer())
        return self


class _Sockets:
    """Socket Mode clients as the runtime builds them; ``failures`` are what connects raise."""

    def __init__(self, *failures: str) -> None:
        self.failures = list(failures)
        self.connects = 0

    def __call__(self, **_kw):
        sockets = self

        class _Socket:
            socket_mode_request_listeners: list = []
            connect_error = ""
            ping_interval = 10
            _connected = False

            async def connect(self) -> None:
                sockets.connects += 1
                if sockets.failures:
                    self.connect_error = sockets.failures.pop(0)
                    raise OSError(self.connect_error)
                self._connected = True

            async def is_connected(self) -> bool:
                return self._connected

            async def close(self) -> None:
                self._connected = False

        return _Socket()


@pytest.fixture
def slack(monkeypatch):
    """Tokens in the environment, the clients cut off the network, retries a moment apart, and
    the module state the wiring sets restored afterwards."""
    monkeypatch.setenv(CRED_SLACK_BOT_TOKEN, BOT)
    monkeypatch.setenv(CRED_SLACK_APP_TOKEN, APP_TOKEN)
    monkeypatch.setattr(transport_mod, "RealSlackClient", lambda token: SimpleNamespace())
    monkeypatch.setattr(events_mod, "AsyncWebClient", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(transport_mod, "_RETRY_DELAYS", (0.01,), raising=False)
    monkeypatch.setattr(enterprise, "_validated_team_id", "")
    for name in ("_gateway_services", "_orch_cfg"):
        monkeypatch.setattr(H, name, getattr(H, name))
    for name in ("global_enabled", "auto_speak", "auto_reply_to_voice"):
        monkeypatch.setattr(H._vc, name, getattr(H._vc, name))
    monkeypatch.setattr(interactions_mod, "_orch", interactions_mod._orch)

    def _wire(auth_test: _AuthTest, sockets: _Sockets) -> None:
        auth_test.install(monkeypatch)
        monkeypatch.setattr(events_mod, "SocketModeReceiver", sockets)

    return _wire


def _services(registered: list) -> SimpleNamespace:
    return SimpleNamespace(
        config=AppConfig.load(),
        owner_id="",
        register_channel_delivery=lambda delivery, provider="": registered.append(provider),
        dashboard_state=None,
        channel_history=None,
    )


async def _until(predicate, what: str) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


# ── the workspace check tells the two failures apart ──────────────────────────────────────


def test_a_refused_connection_is_not_a_rejected_token(monkeypatch):
    _AuthTest(_no_answer()).install(monkeypatch)

    check = enterprise.validate_enterprise(BOT)

    assert check.outcome == enterprise.UNREACHABLE and not check.ok
    assert check.reason == "Slack could not be reached to check the Bot Token (Connection refused)."
    assert BOT not in check.reason


@pytest.mark.parametrize(
    "error, status",
    [
        ("ratelimited", 429),
        ("", 503),
        ("fatal_error", 200),
        # What slack_sdk says of an answer that is not Slack's API: something else on the address.
        ("Received a response in a non-JSON format: 404: Not Found", 404),
    ],
)
def test_slack_saying_it_cannot_answer_now_is_tried_again(monkeypatch, error, status):
    _AuthTest(_slack_says(error, status)).install(monkeypatch)

    check = enterprise.validate_enterprise(BOT)

    assert check.outcome == enterprise.UNREACHABLE, check
    assert "refused" not in check.reason, check.reason


def test_slack_refusing_the_token_is_a_rejection(monkeypatch):
    _AuthTest(_slack_says("invalid_auth")).install(monkeypatch)

    check = enterprise.validate_enterprise(BOT)

    assert check.outcome == enterprise.REJECTED
    assert check.reason.startswith("Slack refused the Bot Token (invalid_auth).")
    assert enterprise.check_message_origin("T0EXAMPLE") is False, "fail closed"


def test_a_workspace_that_answers_is_bound(monkeypatch):
    _AuthTest(TEAM).install(monkeypatch)

    assert enterprise.validate_enterprise(BOT).ok
    assert enterprise.check_message_origin("T0EXAMPLE") is True
    assert enterprise.check_message_origin("T0SOMEWHERE") is False


# ── the transport keeps trying, and the card says what is happening ───────────────────────


@pytest.mark.asyncio
async def test_a_slack_not_reachable_at_start_is_tried_again_until_inbound_runs(slack):
    auth_test = _AuthTest(_no_answer(), _no_answer(), TEAM)
    slack(auth_test, _Sockets())
    registered: list = []
    transport = transport_mod.create_provider({})
    try:
        await transport.start_inbound(_services(registered))

        health = await transport.health()
        assert health["state"] == "error", health
        assert "Slack could not be reached to check the Bot Token (Connection refused)." in (
            health["detail"]
        ), health
        assert "Trying again" in health["detail"], health
        assert "rejected" not in health["detail"] and "refused the Bot Token" not in (
            health["detail"]
        ), health
        assert enterprise.check_message_origin("T0EXAMPLE") is False, "nothing accepted meanwhile"
        assert registered == []

        await _until(lambda: transport._inbound_started, "inbound connected after the retries")
        assert await transport.health() == {
            "state": "ready",
            "detail": "Tokens configured, Socket-Mode connected",
        }
        assert auth_test.calls == 3
        assert registered == ["slack"], "the delivery is handed to core once, when it validates"
        assert enterprise.check_message_origin("T0EXAMPLE") is True
    finally:
        await transport.stop_inbound()


@pytest.mark.asyncio
async def test_a_refused_token_turns_inbound_off_names_the_token_and_is_not_retried(slack):
    auth_test = _AuthTest(_slack_says("invalid_auth"))
    slack(auth_test, _Sockets())
    transport = transport_mod.create_provider({})
    try:
        await transport.start_inbound(_services([]))
        await asyncio.sleep(0.05)

        health = await transport.health()
        assert health["state"] == "error", health
        assert "Slack refused the Bot Token (invalid_auth)" in health["detail"], health
        assert "Trying again" not in health["detail"], health
        assert transport._retry is None and auth_test.calls == 1
    finally:
        await transport.stop_inbound()


@pytest.mark.asyncio
async def test_a_socket_that_cannot_connect_at_start_is_tried_again(slack):
    sockets = _Sockets("ConnectionRefusedError")
    slack(_AuthTest(TEAM), sockets)
    transport = transport_mod.create_provider({})
    try:
        await transport.start_inbound(_services([]))
        health = await transport.health()
        assert "Socket Mode could not connect (ConnectionRefusedError)." in health["detail"], health

        await _until(lambda: transport._inbound_started, "Socket Mode connected on a retry")
        assert sockets.connects == 2
    finally:
        await transport.stop_inbound()


@pytest.mark.asyncio
async def test_an_app_token_slack_refuses_is_not_retried(slack):
    sockets = _Sockets("invalid_auth")
    slack(_AuthTest(TEAM), sockets)
    transport = transport_mod.create_provider({})
    try:
        await transport.start_inbound(_services([]))
        await asyncio.sleep(0.05)

        health = await transport.health()
        assert "Slack refuses the App Token (invalid_auth)" in health["detail"], health
        assert transport._retry is None and sockets.connects == 1
    finally:
        await transport.stop_inbound()


@pytest.mark.asyncio
async def test_stopping_the_channel_stops_the_retries(slack):
    auth_test = _AuthTest(_no_answer())
    slack(auth_test, _Sockets())
    transport = transport_mod.create_provider({})
    await transport.start_inbound(_services([]))
    retry = transport._retry
    assert retry is not None and not retry.done()

    await transport.stop_inbound()
    calls = auth_test.calls
    await asyncio.sleep(0.05)

    assert retry.cancelled() and transport._retry is None
    assert auth_test.calls == calls, "a stopped channel went on asking Slack"
