"""SlackTransport against core's channel conformance kit (CE-6 / T7.1).

The kit is the ONE executable statement of the channel contract, shipped by core and
imported through ``personalclaw.sdk.channel``: connect/send echo shapes, capability-dict
completeness, health/test shapes, the unknown-sender flow (canned reply + one actionable
owner request, deduped), and non-owner content entering a session FENCED.

**Slack does not fully pass it yet, and that is a finding this file records rather than
hides.** Slack predates the CE-1 core trust seam: it still runs its own
``slack_runtime/allowlist.py`` allow/deny + owner-prompt UX, and
``grep -rn guard_inbound slack-channel/`` is empty — CHANNEL-EXPANSION T1.4 ("Slack app
onto the seam: `persist_allowed_user/tracking_channel` delegate to core trust") has not
landed. Telegram, Discord and email all consume ``verdict.fenced_text``; Slack has no
verdict to consume.

So instead of an annotation nobody reads, the gap is ASSERTED: the full-kit call is a
strict xfail (it flips to a failure the day it starts passing for the wrong reason), and
``test_only_the_trust_seam_clause_is_outstanding`` pins that fencing is the *only* clause
Slack fails — so a regression in any other clause turns that assertion red instead of
disappearing into an accepted failure. The kit was NOT weakened to make Slack green.
"""

from __future__ import annotations

import pytest

from personalclaw.sdk.channel import ChannelContractError, assert_channel_contract

from slack_runtime.transport import SlackTransport

#: Slack's inbound is Socket-Mode, connected inside ``start_inbound`` (the one hook the
#: gateway calls at boot); the message router lives in ``slack_runtime.handler`` rather
#: than on the transport, so ``start_inbound`` IS this transport's inbound proof.
_INBOUND_VIA = "start_inbound"


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    """Isolate the trust store and guarantee the run stays offline.

    Two reasons this fixture is not optional. (1) This suite's conftest isolates the
    session map and the migration marker but not ``PERSONALCLAW_HOME``, and the kit
    drives the REAL core trust store, which resolves through ``config_dir()`` — without
    this it would write the developer's own
    ``~/.personalclaw/entity_settings/channel_trust.json``. (2) With a bot token present
    ``SlackTransport.send`` builds a ``RealSlackClient`` and posts to
    ``slack.com/api/chat.postMessage`` for real; clearing the token keeps the kit's
    send clause on the local early-return path, so no test here opens a socket.
    """
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path_factory.mktemp("pclaw-slack-conf")))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    # The guarded door caches verdicts per message id and the kit reuses ids across
    # clauses, so a stale entry would answer the next test's admission. The reset
    # lives HERE and not in conftest because the apps boundary lint exempts only
    # ``test_*.py`` files — the same placement the discord/telegram/email suites use.
    from personalclaw.channel_inbound import reset_admissions

    reset_admissions()
    yield
    reset_admissions()


@pytest.mark.xfail(
    raises=ChannelContractError,
    strict=True,
    reason=(
        "CHANNEL-EXPANSION T1.4 (Slack onto the CE-1 trust seam) has not landed: "
        "slack_runtime owns its own allowlist.py allow/deny UX and never calls "
        "guard_inbound, so nothing here reads verdict.fenced_text. Strict, so this "
        "flips to a failure the moment T1.4 ships and the xfail becomes a lie."
    ),
)
def test_slack_transport_meets_the_channel_contract():
    assert_channel_contract(SlackTransport({}), inbound_via=_INBOUND_VIA)


def test_only_the_trust_seam_clause_is_outstanding():
    """Fencing is the ONLY clause Slack fails — asserted, not assumed.

    Without this, the xfail above would swallow a NEW violation in any other clause
    (health shape, capability dict, unknown-sender dedup) and still report a tidy
    ``xfailed``. Pinning the clause name means the accepted failure stays exactly as
    large as the known gap.
    """
    with pytest.raises(ChannelContractError) as exc:
        assert_channel_contract(SlackTransport({}), inbound_via=_INBOUND_VIA)
    assert "[fencing]" in str(exc.value), (
        "Slack's outstanding conformance gap changed. It was the trust-seam fencing "
        f"clause (T1.4); it is now: {exc.value}"
    )
    assert "verdict.fenced_text" in str(exc.value)


def test_transport_shaped_clauses_pass_today():
    """Everything the kit checks BEFORE the trust seam already holds for Slack.

    The kit fails fast on its first violation, so reaching the fencing clause is itself
    proof that identity/info, the capability dict, the connect/send echo shapes, the
    inbound declaration and the health/test shapes all passed. Stated as a test so that
    proof is a green assertion rather than an inference from a stack trace.
    """
    with pytest.raises(ChannelContractError) as exc:
        assert_channel_contract(SlackTransport({}), inbound_via=_INBOUND_VIA)
    for earlier in ("[identity]", "[capabilities]", "[connect/send]", "[inbound]", "[health/test]"):
        assert earlier not in str(exc.value)
