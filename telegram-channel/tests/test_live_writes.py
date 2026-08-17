"""``TelegramTransport.send()`` under the DISABLE_LIVE_WRITES kill switch (§1.4).

Every test here drives a FAKE Bot API. Nothing in this module can reach
api.telegram.org, so no test can post a message to a real chat.

The shape being pinned is a TYPED refusal, and each of its three properties is
asserted separately because dropping any one of them re-introduces a distinct bug:

* not typed → a caller cannot tell a suppressed write from a failed one;
* not falsy → every ``if await transport.send(...)`` call site above this one flips
  meaning and starts reporting a refusal as a success;
* not returned (i.e. raised) → core's channel conformance kit fails, because
  ``send()`` may not raise for a well-formed message.

And the whole file rests on a VACUITY FLOOR (``test_guard_off_actually_transmits``):
with the guard off, the same call must really reach the fake API. Without it,
"refused" would pass just as happily against a transport that never sends anything.
"""

from __future__ import annotations

import pytest

from personalclaw.sdk.channel import OutboundMessage

from telegram_runtime.transport import TelegramTransport
from telegram_runtime.writes import (
    ENV_DISABLE_LIVE_WRITES,
    SendRefused,
    guard_flag,
    live_writes_disabled,
)

from test_delivery import FakeAPI

_MSG = OutboundMessage(channel_id="4242", text="ping")


def _wired() -> tuple[TelegramTransport, FakeAPI]:
    """A CONFIGURED transport over the recording fake API.

    Configured matters: the token gate short-circuits before the guard, so an
    unconfigured transport would return a plain ``False`` and the refusal assertions
    would pass for the wrong reason.
    """
    api = FakeAPI()
    transport = TelegramTransport({"bot_token": "123:test"})
    transport._api = api
    return transport, api


# ── the vacuity floor ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_off_actually_transmits():
    """Guard OFF ⇒ the call really reaches the transport. This is what stops every
    other test in this file from passing vacuously against a dead send path."""
    transport, api = _wired()
    result = await transport.send(_MSG)
    assert result is True
    assert len(api.sent) == 1
    assert api.sent[0]["chat_id"] == "4242"
    assert "ping" in api.sent[0]["text"]


# ── the refusal ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_on_refuses_and_transmits_nothing(monkeypatch):
    monkeypatch.setenv(ENV_DISABLE_LIVE_WRITES, "1")
    transport, api = _wired()

    result = await transport.send(_MSG)

    assert isinstance(result, SendRefused)
    assert api.sent == [], "the guard must suppress the write, not just label it"


@pytest.mark.asyncio
async def test_refusal_is_falsy_so_existing_call_sites_are_unchanged(monkeypatch):
    """Core's ``ChannelManager.send`` returns this straight up to callers that test it
    for truth. A truthy refusal would report an unsent message as delivered."""
    monkeypatch.setenv(ENV_DISABLE_LIVE_WRITES, "1")
    transport, _ = _wired()

    result = await transport.send(_MSG)

    assert not result
    assert bool(result) is False


@pytest.mark.asyncio
async def test_a_caller_can_tell_a_refusal_from_a_delivery_failure(monkeypatch):
    """THE point of the type. Both outcomes are falsy; only one is the operator's own
    choice, and they demand opposite responses (retry/alert vs. respect it)."""
    transport, api = _wired()

    # A delivery FAILURE: the API raises, send() swallows it into False.
    async def _boom(*a, **k):
        raise RuntimeError("telegram 502")

    monkeypatch.setattr(api, "send_message", _boom)
    failure = await transport.send(_MSG)

    monkeypatch.setenv(ENV_DISABLE_LIVE_WRITES, "1")
    refusal = await transport.send(_MSG)

    assert failure is False
    assert isinstance(refusal, SendRefused)
    # Both falsy — so truthiness ALONE cannot separate them, which is exactly why the
    # refusal has to carry a type.
    assert not failure and not refusal
    assert not isinstance(failure, SendRefused)


@pytest.mark.asyncio
async def test_refusal_never_raises(monkeypatch):
    """``send()`` may not raise for a well-formed message (core's conformance kit).
    The refusal rides the return value; asserted here rather than left to the kit,
    which runs with the guard off."""
    monkeypatch.setenv(ENV_DISABLE_LIVE_WRITES, "1")
    transport, _ = _wired()
    await transport.send(_MSG)  # no pytest.raises — reaching the next line is the test


@pytest.mark.asyncio
async def test_refusal_names_the_channel_target_and_switch(monkeypatch):
    """An operator reading a log line must learn which channel, which target and which
    switch — otherwise "refused" is an unactionable mystery on a multi-channel host."""
    monkeypatch.setenv(ENV_DISABLE_LIVE_WRITES, "1")
    transport, _ = _wired()

    result = await transport.send(_MSG)

    assert result.channel == "telegram"
    assert result.target == "4242"
    assert ENV_DISABLE_LIVE_WRITES in result.reason
    assert "4242" in str(result) and "telegram" in str(result)


@pytest.mark.asyncio
async def test_unconfigured_transport_still_returns_a_plain_bool(monkeypatch):
    """No token ⇒ nothing could have been written, so this is a plain ``False``, not a
    refusal. Claiming the guard suppressed an impossible write would be a lie, and it
    would also break the conformance kit's unconfigured-transport clause."""
    monkeypatch.setenv(ENV_DISABLE_LIVE_WRITES, "1")
    monkeypatch.delenv("PERSONALCLAW_TELEGRAM_BOT_TOKEN", raising=False)
    result = await TelegramTransport({}).send(_MSG)
    assert result is False


# ── the flag parse must not drift from core's ────────────────────────────────


def test_absent_var_allows_writes(monkeypatch):
    """The switch is opt-IN. An absent var means writes are ALLOWED — this is not a
    guard-class default-on flag, and getting it backwards would silence every channel
    on a normal gateway."""
    monkeypatch.delenv(ENV_DISABLE_LIVE_WRITES, raising=False)
    assert live_writes_disabled() is False


@pytest.mark.parametrize(
    "raw",
    ["0", "false", "FALSE", "no", "off", "disable", "disabled", "n", "f", " Off "],
)
def test_explicit_falsy_values_turn_the_guard_off(monkeypatch, raw):
    monkeypatch.setenv(ENV_DISABLE_LIVE_WRITES, raw)
    assert live_writes_disabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "", "ture", "maybe", "2"])
def test_any_other_present_value_turns_the_guard_on(monkeypatch, raw):
    """The fail-safe half: a typo (``"ture"``), an unknown token, or an empty string
    keeps the guard ON. A mistyped guard flag must never silently disable the guard."""
    monkeypatch.setenv(ENV_DISABLE_LIVE_WRITES, raw)
    assert live_writes_disabled() is True


@pytest.mark.parametrize(
    ("value", "enabled"),
    [(None, True), (True, True), (False, False), (0, False), (1, True), (object(), True)],
)
def test_guard_flag_matches_cores_parse_for_non_string_shapes(value, enabled):
    assert guard_flag(value) is enabled


def test_this_apps_parse_agrees_with_cores_symbol_for_symbol(monkeypatch):
    """The mirror is only safe while it MATCHES. Core's ``guardrails.writes`` is not an
    SDK export, so the runtime code cannot import it — but this test can (test files are
    exempt from the import-boundary lint), which is the whole point: if core ever
    changes the parse, this reds instead of the two halves quietly disagreeing about
    whether writes are on."""
    from personalclaw.guardrails.flags import guard_flag as core_guard_flag
    from personalclaw.guardrails.writes import live_writes_disabled as core_disabled

    for raw in ["0", "false", "off", "n", "1", "true", "", "ture", " OFF ", "disabled"]:
        assert guard_flag(raw) == core_guard_flag(raw), raw
        monkeypatch.setenv(ENV_DISABLE_LIVE_WRITES, raw)
        assert live_writes_disabled() == core_disabled(), raw

    monkeypatch.delenv(ENV_DISABLE_LIVE_WRITES, raising=False)
    assert live_writes_disabled() == core_disabled() is False
