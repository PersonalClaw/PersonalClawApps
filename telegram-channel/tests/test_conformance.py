"""TelegramTransport against core's channel conformance kit (CE-6 / T7.1).

The kit is the ONE executable statement of the channel contract, shipped by core and
imported through ``personalclaw.sdk.channel`` like every other core symbol this app uses.
It asserts what this bundle's own suite deliberately does not re-litigate per channel:
the connect/send echo shapes, the completeness of the capability dict, the health/test
shapes, the unknown-sender flow (canned reply + one actionable owner request, deduped),
that non-owner group content enters a session FENCED, and — because Telegram declares
``edits=True`` — that the edit stream is throttled and force-flushes on stop.

Everything Telegram-specific (MarkdownV2 rendering, the 4096 cap, offset persistence)
stays in this bundle's other test modules. This file is the shared floor.
"""

from __future__ import annotations

import pytest

from personalclaw.sdk.channel import ChannelContractError, assert_channel_contract

from telegram_runtime.delivery import _EDIT_MIN_INTERVAL, TelegramDelivery
from telegram_runtime.transport import TelegramTransport

from test_delivery import FakeAPI


def _wired() -> tuple[TelegramTransport, TelegramDelivery, FakeAPI, object]:
    """A transport + a delivery over the recording fake, with an injectable clock.

    The clock seam is the delivery's own ``_now`` (the same one
    ``test_delivery.TestStreamThrottle`` drives), so the throttle clause is asserted
    without sleeping and against the app's real floor rather than a number the kit
    invented.
    """
    api = FakeAPI()
    delivery = TelegramDelivery(api, "42")
    clock = {"t": 0.0}
    delivery._now = lambda: clock["t"]  # type: ignore[method-assign]
    return TelegramTransport({"bot_token": "123:conformance"}), delivery, api, clock


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
            TelegramTransport({"bot_token": "123:conformance"}),
            delivery=delivery,
            fake_backend=api,
            min_edit_interval=_EDIT_MIN_INTERVAL,
            clock=lambda t: clock.__setitem__("t", t),
            inbound_via="_on_message",
        )
