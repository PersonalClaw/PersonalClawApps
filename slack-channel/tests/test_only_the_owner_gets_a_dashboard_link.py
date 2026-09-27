"""Only the owner is sent a dashboard link, by `!dashboard` or `/personalclaw dashboard`.

A dashboard link signs in as the owner: the token opens the whole dashboard, whatever user id it
names. Measured on `main`: an allowed user who is not the owner typed `!dashboard` and was sent a
DM with a link that opened the owner's dashboard, and `/personalclaw dashboard` did the same. The
link is minted by core's `owner_sign_in_token` now, for this channel's owner alone. Anyone else is
told why in core's words, and nothing is minted or sent.

Each refusal has its floor: the owner, asking the same way, is sent a link that signs in.
"""

from __future__ import annotations

import os
import socket
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personalclaw.sdk.channel import NOT_THE_OWNER_SENTENCE, owner_id_credential

OWNER = "U0OWNER01"
ALLOWED = "U0ALLOWED"


@pytest.fixture(autouse=True)
def owner_and_an_allowed_user(monkeypatch):
    """OWNER owns this channel, and ALLOWED may talk to the bot, but is not the owner."""
    import slack_runtime.handler as h

    monkeypatch.setattr(h, "_owner_id", OWNER)
    monkeypatch.setattr(h, "_allowed_users", {OWNER, ALLOWED})
    monkeypatch.setenv(owner_id_credential("slack"), OWNER)


def _slack() -> MagicMock:
    slack = MagicMock()
    slack.post_message = AsyncMock(return_value=None)
    slack.open_dm = AsyncMock(return_value="D_DM")
    slack.post_blocks = AsyncMock(return_value=None)
    return slack


def _local_dashboard(stack: ExitStack) -> MagicMock:
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


def _signs_in_as(dm_text: str) -> str:
    from personalclaw.dashboard.token_auth import validate_token

    token = dm_text.split("?token=", 1)[1].split("|", 1)[0].split(">", 1)[0]
    valid, user, reason = validate_token(token)
    assert valid, reason
    return user


async def _bang_dashboard(user: str) -> MagicMock:
    from slack_runtime.handler import _handle_slash_command

    slack = _slack()
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    with ExitStack() as stack:
        _local_dashboard(stack)
        await _handle_slash_command(
            "!dashboard", slack, sessions, "C123", "ts1", "ts2", "sess1", user
        )
    return slack


@pytest.mark.asyncio
async def test_an_allowed_user_who_is_not_the_owner_is_refused_and_sent_nothing():
    slack = await _bang_dashboard(ALLOWED)
    slack.open_dm.assert_not_called()
    replies = [c[0] for c in slack.post_message.call_args_list]
    assert [r[1] for r in replies] == [f"❌ {NOT_THE_OWNER_SENTENCE}"]
    assert replies[0][0] == "C123"
    assert not any("?token=" in str(c) for c in slack.mock_calls)


@pytest.mark.asyncio
async def test_the_owner_is_sent_a_link_that_signs_in():
    slack = await _bang_dashboard(OWNER)
    slack.open_dm.assert_called_once_with(OWNER)
    dm_channel, dm_text = slack.post_message.call_args_list[0][0][:2]
    assert dm_channel == "D_DM"
    assert _signs_in_as(dm_text) == OWNER


@pytest.mark.asyncio
async def test_the_owners_enterprise_id_is_the_owner_too():
    """An Enterprise Grid W… id is the same person as the owner's U… id, as is_owner reads it."""
    grid = OWNER.replace("U", "W", 1)
    slack = await _bang_dashboard(grid)
    dm_text = slack.post_message.call_args_list[0][0][1]
    assert _signs_in_as(dm_text) == OWNER


@pytest.mark.asyncio
async def test_with_no_owner_nobody_is_sent_one(monkeypatch):
    import slack_runtime.handler as h

    monkeypatch.setattr(h, "_owner_id", "")
    monkeypatch.delenv(owner_id_credential("slack"), raising=False)
    slack = await _bang_dashboard(OWNER)
    slack.open_dm.assert_not_called()
    assert [c[0][1] for c in slack.post_message.call_args_list] == [
        f"❌ {NOT_THE_OWNER_SENTENCE}"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("caller, sent", [(ALLOWED, False), (OWNER, True)])
async def test_the_slash_command_follows_the_same_rule(caller, sent):
    from slack_runtime.events import _handle_dashboard

    slack = _slack()
    orch = MagicMock()
    orch.slack = slack
    orch.slack_command = "personalclaw"
    respond = AsyncMock()
    with ExitStack() as stack:
        log = _local_dashboard(stack)
        await _handle_dashboard(orch, caller, "", respond)
    if sent:
        slack.open_dm.assert_called_once_with(OWNER)
        assert _signs_in_as(slack.post_message.call_args_list[0][0][1]) == OWNER
    else:
        slack.open_dm.assert_not_called()
        respond.assert_awaited_once_with(f"❌ {NOT_THE_OWNER_SENTENCE}")
        log.assert_called_once_with(
            caller=ALLOWED,
            operation="slack.dashboard_token",
            outcome="denied",
            resources="ttl=3600",
            error=NOT_THE_OWNER_SENTENCE,
        )
