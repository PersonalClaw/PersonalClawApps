"""A Slack thread imported into the dashboard is continued there, and its answers go back to Slack.

"Link to dashboard" (``!link-to-dashboard`` or the button under an answer) imports the thread
into a new chat and links the thread to it, so the next reply in the thread continues that chat
through the platform's inbound door. The chat was made with no channel, so the door had nothing
to answer on: the turn ran in the dashboard and Slack heard nothing back. And after a restart the
door will not continue a chat that carries no channel, so the reply went to a conversation of
its own. The chat is made as this channel's now, as every chat the door opens is.

Driven against core's real dashboard state and session store, the two the door reads.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

THREAD = "1712793600.000200"
CHANNEL = "C0123ABC456"


def _dashboard(tmp_path):
    from personalclaw.config.loader import AppConfig
    from personalclaw.dashboard.state import DashboardState
    from personalclaw.history import ConversationLog
    from personalclaw.session import SessionManager

    return DashboardState(
        sessions=SessionManager(AppConfig()),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path / "sessions"),
    )


@pytest.mark.asyncio
async def test_an_imported_thread_continues_its_chat_and_is_answered_on_slack(tmp_path):
    from slack_runtime import interactions

    slack = MagicMock()
    slack.fetch_thread_replies = AsyncMock(
        return_value=[
            {"user": "U0OWNER1", "text": "Which train do we take?"},
            {"bot_id": "B0BOT1", "text": "The 9:10 to the coast."},
        ]
    )
    state = _dashboard(tmp_path)

    chat = await interactions._import_thread_to_session(slack, state, CHANNEL, THREAD)

    assert chat is not None
    assert state.channel_provider_for(chat.key) == "slack", "its answers have no channel to go to"
    assert state.get_linked_session(THREAD) is chat, "a reply in the thread opens another chat"
    assert state.sessions.get_channel_link(f"dashboard:{chat.key}") == (THREAD, CHANNEL)
    # Asked again, the import finds the chat it made rather than importing the thread twice.
    assert await interactions._import_thread_to_session(slack, state, CHANNEL, THREAD) is chat
    slack.fetch_thread_replies.assert_awaited_once()
