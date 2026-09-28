"""DiscordTransport against core's channel conformance kit.

The kit is the ONE executable statement of the channel contract, shipped by core and
imported through ``personalclaw.sdk.channel``. It asserts the floor every channel shares —
connect/send echo shapes, capability-dict completeness, health/test shapes, the
unknown-sender flow (canned reply + one actionable owner request, deduped), non-owner
group content entering a session FENCED, and (Discord declares ``edits=True``) a throttled
edit stream that force-flushes on stop and takes every status a call can have. With the owner's
press wired, how an approval prompt ends however it ends, and what a press after that is told,
driven at ``resolve_interaction``, where a button press arrives.

Discord-specific behaviour — the intents bitfield, the 2000-char split, gateway RESUME —
stays in this bundle's other test modules.
"""

from __future__ import annotations

import pytest

from personalclaw.sdk.channel import ChannelContractError, assert_channel_contract

from discord_runtime.delivery import (
    _APPROVE,
    _DENY,
    _EDIT_MIN_INTERVAL,
    INTERACTION_TYPE_COMPONENT,
    DiscordDelivery,
)
from discord_runtime.transport import DiscordTransport

from test_delivery import FakeAPI

OWNER = "42"


def _press(delivery: DiscordDelivery, api: FakeAPI):
    """The owner's Approve or Deny on a prompt, through the handler a button press reaches;
    returns what the owner was told, in the message only they see ("" for a silent ack)."""

    async def press(pending, approve: bool) -> str:
        acked = len(api.acks)
        await delivery.resolve_interaction(
            {
                "type": INTERACTION_TYPE_COMPONENT,
                "id": f"i-{acked}",
                "token": "tok",
                "data": {"custom_id": f"{_APPROVE if approve else _DENY}:{pending.request_id}"},
                "user": {"id": OWNER},
            }
        )
        told = [a.get("data") or {} for a in api.acks[acked:]]
        return str(told[-1].get("content") or "") if told else ""

    return press


def _wired() -> tuple[DiscordTransport, DiscordDelivery, FakeAPI, dict]:
    """A transport + a delivery over the recording fake, with an injectable clock.

    The clock seam is the delivery's own ``_now`` (as in
    ``test_delivery``'s throttle test), so the throttle clause is asserted against the
    app's real floor without sleeping.
    """
    api = FakeAPI()
    delivery = DiscordDelivery(api, lambda: OWNER)
    clock = {"t": 0.0}
    delivery._now = lambda: clock["t"]  # type: ignore[method-assign]
    return DiscordTransport({"bot_token": "conformance.token"}), delivery, api, clock


def test_discord_transport_meets_the_channel_contract():
    transport, delivery, api, clock = _wired()
    assert_channel_contract(
        transport,
        delivery=delivery,
        fake_backend=api,
        min_edit_interval=_EDIT_MIN_INTERVAL,
        clock=lambda t: clock.__setitem__("t", t),
        # Discord runs its own gateway WS loop from start_inbound and normalizes each
        # MESSAGE_CREATE in _on_message_create; no generic receive() iterator.
        inbound_via="_on_message_create",
        press=_press(delivery, api),
    )


def test_unconfigured_transport_also_conforms():
    """No token ⇒ offline, send() returns False rather than raising."""
    assert_channel_contract(DiscordTransport({}), inbound_via="_on_message_create")


def test_the_kit_is_actually_asserting_something_here():
    """Guard against a vacuous green: break the throttle and the kit must catch it."""

    class Unthrottled(DiscordDelivery):
        async def _maybe_edit(self, st, *, force: bool) -> None:  # type: ignore[no-untyped-def]
            return await super()._maybe_edit(st, force=True)

    api = FakeAPI()
    delivery = Unthrottled(api, "42")
    clock = {"t": 0.0}
    delivery._now = lambda: clock["t"]  # type: ignore[method-assign]
    with pytest.raises(ChannelContractError, match=r"\[streaming\]"):
        assert_channel_contract(
            DiscordTransport({"bot_token": "conformance.token"}),
            delivery=delivery,
            fake_backend=api,
            min_edit_interval=_EDIT_MIN_INTERVAL,
            clock=lambda t: clock.__setitem__("t", t),
            inbound_via="_on_message_create",
        )


def test_the_kit_catches_a_late_press_told_nothing_of_how_it_ended():
    """The approvals clause reaches this app: a delivery that forgot how its approvals ended
    answers every late press alike, and the kit names it."""

    class Forgetful(DiscordDelivery):
        async def resolve_interaction(self, interaction):  # type: ignore[no-untyped-def]
            self._ended.clear()
            await super().resolve_interaction(interaction)

    api = FakeAPI()
    delivery = Forgetful(api, lambda: OWNER)
    with pytest.raises(ChannelContractError, match=r"\[approvals\].*answered alike"):
        assert_channel_contract(
            DiscordTransport({"bot_token": "conformance.token"}),
            delivery=delivery,
            inbound_via="_on_message_create",
            press=_press(delivery, api),
        )
