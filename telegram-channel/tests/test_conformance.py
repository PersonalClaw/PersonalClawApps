"""TelegramTransport against core's channel conformance kit.

The kit is the ONE executable statement of the channel contract, shipped by core and
imported through ``personalclaw.sdk.channel`` like every other core symbol this app uses.
It asserts what this bundle's own suite deliberately does not re-litigate per channel:
the connect/send echo shapes, the completeness of the capability dict, the health/test
shapes, the unknown-sender flow (canned reply + one actionable owner request, deduped),
that non-owner group content enters a session FENCED, and — because Telegram declares
``edits=True`` — that the edit stream is throttled and force-flushes on stop, and takes every
status a call can have. With the owner's press wired, it asserts how an approval prompt ends
however it ends, and what a press after that is told, driven at ``resolve_callback``, where a
button press arrives.

Everything Telegram-specific (MarkdownV2 rendering, the 4096 cap, offset persistence)
stays in this bundle's other test modules. This file is the shared floor.
"""

from __future__ import annotations

import pytest

from personalclaw.sdk.channel import ChannelContractError, assert_channel_contract

from telegram_runtime.delivery import _ANSWER, _EDIT_MIN_INTERVAL, TelegramDelivery
from telegram_runtime.transport import TelegramTransport

from test_delivery import FakeAPI

OWNER = "42"


def _press(delivery: TelegramDelivery, api: FakeAPI):
    """The owner's press on one of the prompt's answers, by its key, through the handler a
    button press reaches, with the callback data the prompt's own button carries, from the
    message it is on; returns what the owner's press was answered with."""

    async def press(pending, answer: str) -> str:
        answered = len(api.answers)
        prompt = [m for m in api.sent if m["reply_markup"]][-1]
        buttons = [row[0] for row in prompt["reply_markup"]["inline_keyboard"]]
        data = next(
            b["callback_data"]
            for b, offered in zip(buttons, pending.answers)
            if offered["key"] == answer
        )
        assert data.startswith(_ANSWER) and data.endswith(f":{pending.request_id}")
        await delivery.resolve_callback(
            {"id": f"cq-{answered}", "data": data, "from": {"id": OWNER}, **api.press_on_prompt()}
        )
        told = api.answers[answered:]
        return str(told[-1]["text"] or "") if told else ""

    return press


def _transport(api: FakeAPI) -> TelegramTransport:
    """A configured transport that sends through the recording fake: the kit's send clause must
    reach no real Telegram. A transport sends on the receiver's client while the token it
    started with is current, so the fake stands in as that client."""
    transport = TelegramTransport({"bot_token": "123:conformance"})
    transport._api = api
    return transport


def _wired() -> tuple[TelegramTransport, TelegramDelivery, FakeAPI, object]:
    """A transport + a delivery over the recording fake, with an injectable clock.

    The clock seam is the delivery's own ``_now`` (the same one
    ``test_delivery.TestStreamThrottle`` drives), so the throttle clause is asserted
    without sleeping and against the app's real floor rather than a number the kit
    invented.
    """
    api = FakeAPI()
    delivery = TelegramDelivery(api, lambda: OWNER)
    clock = {"t": 0.0}
    delivery._now = lambda: clock["t"]  # type: ignore[method-assign]
    return _transport(api), delivery, api, clock


def test_telegram_transport_meets_the_channel_contract():
    transport, delivery, api, clock = _wired()
    assert_channel_contract(
        transport,
        delivery=delivery,
        fake_backend=api,
        min_edit_interval=_EDIT_MIN_INTERVAL,
        clock=lambda t: clock.__setitem__("t", t),
        # Telegram drives its own long-poll loop from start_inbound and normalizes each
        # update in _on_message; it does not implement the generic receive() iterator.
        inbound_via="_on_message",
        press=_press(delivery, api),
    )


def test_unconfigured_transport_also_conforms():
    """No token ⇒ offline, send() returns False rather than raising. A transport whose
    contract only holds once credentials exist fails the owner at exactly the moment
    they are debugging why it is offline."""
    assert_channel_contract(TelegramTransport({}), inbound_via="_on_message")


def test_the_kit_is_actually_asserting_something_here():
    """Guard against a vacuous green: break one clause and the kit must catch it.

    A conformance call that would pass no matter what the transport did is worse than
    no call at all, so this pins that the kit's failure path reaches this app.
    """

    class Unthrottled(TelegramDelivery):
        async def _maybe_edit(self, st, *, force: bool) -> None:  # type: ignore[no-untyped-def]
            # Force every append through, ignoring the throttle window.
            return await super()._maybe_edit(st, force=True)

    api = FakeAPI()
    delivery = Unthrottled(api, "42")
    clock = {"t": 0.0}
    delivery._now = lambda: clock["t"]  # type: ignore[method-assign]
    with pytest.raises(ChannelContractError, match=r"\[streaming\]"):
        assert_channel_contract(
            _transport(api),
            delivery=delivery,
            fake_backend=api,
            min_edit_interval=_EDIT_MIN_INTERVAL,
            clock=lambda t: clock.__setitem__("t", t),
            inbound_via="_on_message",
        )


def test_the_kit_catches_a_late_press_told_nothing_of_how_it_ended():
    """The approvals clause reaches this app: a delivery that forgot how its approvals ended
    answers every late press alike, and the kit names it."""

    class Forgetful(TelegramDelivery):
        async def resolve_callback(self, cq):  # type: ignore[no-untyped-def]
            self._ended.clear()
            await super().resolve_callback(cq)

    api = FakeAPI()
    delivery = Forgetful(api, lambda: OWNER)
    with pytest.raises(ChannelContractError, match=r"\[approvals\].*answered alike"):
        assert_channel_contract(
            _transport(api),
            delivery=delivery,
            inbound_via="_on_message",
            press=_press(delivery, api),
        )
