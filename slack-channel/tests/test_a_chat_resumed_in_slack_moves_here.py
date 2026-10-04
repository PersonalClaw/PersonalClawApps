"""A chat resumed in Slack moves here, whichever channel it was on, and then answers in Slack.

`!sessions` lists recent chats with a Resume button, and Resume offers a thread here or the
owner's DM. A chat already linked to any thread was "already active": a chat that came in on
Telegram was never offered the choice, and was pointed to a Slack conversation built from its
Telegram chat's ids, which opens nothing. A chat's link now names the channel it is on, so only a
chat already in a thread here is pointed to it. One on another channel is moved to the thread it
is resumed in, through core's link (``link_channel(…, provider="slack")``), which takes it off the
thread it was on, and its answers come here.

Driven against core's real dashboard state and session store, the two the inbound door reads.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

OWNER = "U0OWNER1"
HERE = "C0123ABC456"  # the Slack channel `!sessions` was asked in
TELEGRAM_DM = "5550123"
RESUMED = "1712793900.000300"  # the thread the resume opens here


def _dashboard(tmp_path):
    from personalclaw.config.loader import AppConfig
    from personalclaw.dashboard.state import DashboardState
    from personalclaw.history import ConversationLog
    from personalclaw.session import SessionManager

    state = DashboardState(
        sessions=SessionManager(AppConfig()),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path / "sessions"),
    )
    state.push_sessions_update = MagicMock()
    return state


@pytest.fixture
def slack(monkeypatch, tmp_path):
    """This app wired to a real dashboard as the gateway wires it, with a Slack that posts."""
    from slack_runtime import interactions

    state = _dashboard(tmp_path)
    client = MagicMock()
    client.post_blocks = AsyncMock(return_value="1712793800.000100")
    client.post_message = AsyncMock(return_value=RESUMED)
    client.open_dm = AsyncMock(return_value="D0OWNERDM")
    monkeypatch.setattr(
        interactions,
        "_orch",
        SimpleNamespace(sessions=state.sessions, slack=client, dashboard_state=state),
    )
    monkeypatch.setattr(interactions, "is_owner", lambda user: user == OWNER)
    return interactions, state, client


def _a_chat_from_telegram(state):
    """What Telegram's inbound door makes of the owner's message: a chat linked to their DM there."""
    chat = state.get_or_create_session(app="telegram")
    chat.append("user", "Which train do we take?", "msg msg-u")
    state.link_channel(chat.key, TELEGRAM_DM, TELEGRAM_DM, provider="telegram")
    return chat


def _resume(chat) -> dict:
    import json

    return {"value": json.dumps({"key": f"dashboard:{chat.key}", "title": "Trip planning"})}


def _choice(chat) -> dict:
    import json

    return {
        "value": json.dumps(
            {"key": f"dashboard:{chat.key}", "title": "Trip planning", "src_channel": HERE}
        )
    }


@pytest.mark.asyncio
async def test_a_chat_on_telegram_is_offered_a_thread_here(slack):
    """🔴 Red before: it was "already active", so no choice was offered."""
    interactions, state, client = slack
    chat = _a_chat_from_telegram(state)

    await interactions._handle_session_resume({}, _resume(chat), HERE, "1712793700.000050", OWNER)

    client.post_blocks.assert_awaited_once()
    actions = [e["action_id"] for b in client.post_blocks.await_args.args[1] for e in b.get("elements", [])]
    assert any(a.startswith("pc_resume_thread_") for a in actions), actions


@pytest.mark.asyncio
async def test_a_chat_on_telegram_resumed_in_a_thread_here_answers_here(slack):
    """🔴 Red before: the choice said "Already active", linked nothing, and the chat kept answering
    on Telegram."""
    interactions, state, client = slack
    chat = _a_chat_from_telegram(state)

    await interactions._handle_resume_choice(
        {}, _choice(chat), HERE, "1712793800.000100", OWNER, mode="thread"
    )

    assert client.post_message.await_args_list[0].args[0] == HERE
    assert state.sessions.get_channel_link(f"dashboard:{chat.key}") == (RESUMED, HERE)
    assert state.channel_provider_for(chat.key) == "slack"
    assert state.get_linked_session(RESUMED) is chat, "a reply in the thread opens another chat"
    assert state.get_linked_session(TELEGRAM_DM) is None, "the Telegram DM still holds the chat"


@pytest.mark.asyncio
async def test_a_chat_already_in_a_thread_here_is_pointed_to_it(slack):
    """The control: a chat in a Slack thread is not offered a second one, and stays where it is."""
    interactions, state, client = slack
    chat = state.get_or_create_session(app="slack")
    state.link_channel(chat.key, "1712793600.000200", HERE, provider="slack")

    await interactions._handle_session_resume({}, _resume(chat), HERE, "1712793700.000050", OWNER)
    await interactions._handle_resume_choice(
        {}, _choice(chat), HERE, "1712793800.000100", OWNER, mode="thread"
    )

    client.post_blocks.assert_not_awaited()
    client.post_message.assert_not_awaited()
    assert state.sessions.get_channel_link(f"dashboard:{chat.key}") == ("1712793600.000200", HERE)
    assert state.channel_provider_for(chat.key) == "slack"
