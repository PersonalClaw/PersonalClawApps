"""A Slack dashboard link says how long it can be opened and how long its sign-in lasts, and a
lifetime the gateway refuses is refused in Slack too, in the gateway's words.

Measured on apps ``main``: the DM and the slash command's reply read
``⏱ Click within 1440m · session lasts 60m`` for the default one-hour link. Core's link window is
24 hours, but a link can be opened only until the sooner of that window and the sign-in's own
lifetime, so the one-hour link stopped opening after 60 minutes, not 1,440. The sign-in ends its
lifetime after the link is made, not after it is opened. And the app shortened any lifetime to
``MAX_SESSION_TTL_SECS`` itself, without a word, where the gateway now refuses one that is too
long with a sentence saying why.

Written to pass against a core from before the 90-day limit as well as after it: the refusal is
driven with a stubbed ``generate_token``, and the no-clamp test reads the installed limit.
"""

from __future__ import annotations

import os
import socket
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_runtime.blocks import dashboard_link_block, duration_words, link_lifetime_text

REFUSAL = (
    "A sign-in can last at most 90 days, and 365 days is longer. 90 days is the limit for a "
    "long-lived credential, because the longer a link or token keeps working, the longer anyone "
    "who copies it can use your dashboard. Ask for 90 days or less, such as 90d or 2160h."
)


def _slack() -> MagicMock:
    slack = MagicMock()
    slack.post_message = AsyncMock(return_value=None)
    slack.open_dm = AsyncMock(return_value="D_DM")
    slack.post_blocks = AsyncMock(return_value=None)
    return slack


def _local_dashboard(stack: ExitStack) -> MagicMock:
    """The patches the dashboard-command tests beside this one use: a local-only dashboard at
    the default port, and a stand-in for the security log."""
    cfg = MagicMock()
    cfg.dashboard.url = ""
    stack.enter_context(patch("slack_runtime.allowlist.AppConfig.load", return_value=cfg))
    stack.enter_context(
        patch("personalclaw.dashboard.origin.socket.gethostname", return_value="myhostname")
    )
    stack.enter_context(
        patch("personalclaw.dashboard.origin.socket.gethostbyname", return_value="10.0.0.1")
    )
    stack.enter_context(
        patch("personalclaw.dashboard.origin.socket.getaddrinfo", side_effect=socket.gaierror)
    )
    stack.enter_context(patch.dict(os.environ, {}, PERSONALCLAW_PORT=""))
    sel = stack.enter_context(patch("slack_runtime.allowlist.sel"))
    sel.return_value.log_api_access = MagicMock()
    return sel.return_value.log_api_access


# ── the words ────────────────────────────────────────────────────────────────────────────────


def test_a_one_hour_link_opens_for_an_hour_not_a_day():
    assert link_lifetime_text(3600, 86400) == (
        "⏱ Open it within 1 hour · the sign-in ends 1 hour from now"
    )


def test_a_week_long_link_opens_for_the_window_and_signs_in_for_the_week():
    assert link_lifetime_text(7 * 86400, 86400) == (
        "⏱ Open it within 24 hours · the sign-in ends 7 days from now"
    )


@pytest.mark.parametrize(
    "secs, words",
    [
        (60, "1 minute"),
        (1800, "30 minutes"),
        (5400, "90 minutes"),
        (7200, "2 hours"),
        (86400, "24 hours"),
        (36 * 3600, "36 hours"),
        (2 * 86400, "2 days"),
        (90 * 86400, "90 days"),
    ],
)
def test_durations_read_as_words(secs, words):
    assert duration_words(secs) == words


@pytest.mark.parametrize("ttl", [60, 1800, 3600, 86400, 3 * 86400, 90 * 86400])
@pytest.mark.parametrize("window", [300, 86400])
def test_a_link_never_claims_to_open_for_longer_than_its_sign_in_lasts(ttl, window):
    """Whatever the gateway's window: open-for is the sooner of the two, and the sign-in's end
    is its whole lifetime."""
    text = link_lifetime_text(ttl, window)
    assert f"Open it within {duration_words(min(ttl, window))} " in text
    assert text.endswith(f"the sign-in ends {duration_words(ttl)} from now")
    assert "Click within" not in text


def test_the_slash_command_block_carries_the_same_words():
    block = dashboard_link_block("http://localhost:10000/?token=t", 3600, 86400)
    text = block[0]["text"]["text"]
    assert text.startswith("🔗 <http://localhost:10000/?token=t|*Open Dashboard*>\n")
    assert text.endswith("⏱ Open it within 1 hour · the sign-in ends 1 hour from now")


# ── the DM a real command sends ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_dm_says_how_long_the_default_link_lasts():
    from slack_runtime.handler import _handle_slash_command

    slack = _slack()
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    with ExitStack() as stack:
        _local_dashboard(stack)
        await _handle_slash_command(
            "!dashboard", slack, sessions, "C123", "ts1", "ts2", "sess1", "U001"
        )
    dm_channel, dm_text = slack.post_message.call_args_list[0][0][:2]
    assert dm_channel == "D_DM"
    assert dm_text.endswith("⏱ Open it within 1 hour · the sign-in ends 1 hour from now")
    assert "1440m" not in dm_text


# ── a lifetime the gateway refuses ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_refused_lifetime_is_answered_with_the_gateways_sentence_and_nothing_is_sent():
    from slack_runtime.handler import _handle_slash_command

    slack = _slack()
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    with ExitStack() as stack:
        log = _local_dashboard(stack)
        stack.enter_context(
            patch("slack_runtime.allowlist.generate_token", side_effect=ValueError(REFUSAL))
        )
        await _handle_slash_command(
            "!dashboard 2h", slack, sessions, "C123", "ts1", "ts2", "sess1", "U001"
        )
    slack.open_dm.assert_not_called()
    replies = [c[0][1] for c in slack.post_message.call_args_list]
    assert replies == [f"❌ {REFUSAL}"]
    log.assert_called_once_with(
        caller="U001", operation="slack.dashboard_token", outcome="denied", resources="ttl=7200"
    )


@pytest.mark.asyncio
async def test_the_slash_command_relays_the_refusal_too():
    from slack_runtime.events import _handle_dashboard

    slack = _slack()
    orch = MagicMock()
    orch.slack = slack
    orch.slack_command = "personalclaw"
    respond = AsyncMock()
    with ExitStack() as stack:
        _local_dashboard(stack)
        stack.enter_context(
            patch("slack_runtime.allowlist.generate_token", side_effect=ValueError(REFUSAL))
        )
        await _handle_dashboard(orch, "U001", "2h", respond)
    slack.open_dm.assert_not_called()
    respond.assert_awaited_once_with(f"❌ {REFUSAL}")


@pytest.mark.asyncio
async def test_the_slash_command_reply_says_how_long_the_link_lasts():
    from slack_runtime.events import _handle_dashboard

    slack = _slack()
    orch = MagicMock()
    orch.slack = slack
    orch.slack_command = "personalclaw"
    respond = AsyncMock()
    with ExitStack() as stack:
        _local_dashboard(stack)
        await _handle_dashboard(orch, "U001", "", respond)
    respond.assert_awaited_once()
    blocks = respond.await_args.kwargs["blocks"]
    assert blocks[0]["text"]["text"].endswith(
        "⏱ Open it within 1 hour · the sign-in ends 1 hour from now"
    )


@pytest.mark.asyncio
async def test_the_app_does_not_shorten_a_lifetime_itself():
    """The lifetime asked for reaches ``generate_token`` as asked, even past the installed
    limit, so the gateway refuses it rather than the app quietly granting less."""
    from personalclaw.sdk.channel import MAX_SESSION_TTL_SECS
    from slack_runtime.allowlist import send_dashboard_link

    asked = MAX_SESSION_TTL_SECS + 3600
    seen: list[int] = []

    def _mint(user_id: str, ttl_seconds: int = 3600, *, app: str = "") -> str:
        seen.append(ttl_seconds)
        raise ValueError(REFUSAL)

    with ExitStack() as stack:
        _local_dashboard(stack)
        stack.enter_context(patch("slack_runtime.allowlist.generate_token", side_effect=_mint))
        with pytest.raises(ValueError, match="at most 90 days"):
            await send_dashboard_link(_slack(), "U001", asked)
    assert seen == [asked]
