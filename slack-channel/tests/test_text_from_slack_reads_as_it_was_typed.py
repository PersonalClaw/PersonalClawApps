"""A message's text from Slack reaches the agent, the Inbox and the history as it was typed.

Slack sends ``&``, ``<`` and ``>`` in a message's text as ``&amp;``, ``&lt;`` and ``&gt;``: it
reads the characters themselves as its own markup, a mention (``<@U…>``, ``<!here>``), a channel
(``<#C…|name>``) or a link (``<https://…|words>``). Each place this app reads a message's text
from Slack turns those three back into the characters, and nothing else: a mention, a channel or
a link stays in Slack's own spelling. The payloads below are shaped as Slack sends them.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_runtime.client import RealSlackClient
from slack_runtime.format import slack_text
from slack_runtime.inbox_source import SlackInboxSource

#: What a sender typed, and how Slack sends it.
TYPED = "Is 3 < 5 && 5 > 2? Ask R&D"
SENT = "Is 3 &lt; 5 &amp;&amp; 5 &gt; 2? Ask R&amp;D"


class TestSlackText:
    def test_the_three_characters_are_read_back(self):
        assert slack_text(SENT) == TYPED

    def test_an_entity_that_was_typed_stays_as_typed(self):
        """``&amp;lt;`` is someone who typed ``&lt;``: one pass, so it does not become ``<``."""
        assert slack_text("Write &amp;lt; for &lt;") == "Write &lt; for <"

    def test_nothing_slack_does_not_encode_is_decoded(self):
        assert slack_text("&quot;quoted&quot; &copy; &#60; &nbsp;") == (
            "&quot;quoted&quot; &copy; &#60; &nbsp;"
        )

    def test_mentions_channels_and_links_stay_in_slacks_spelling(self):
        assert slack_text(
            "<@U123> see <https://example.com/?a=1&amp;b=2|Q&amp;A> in <#C1|general> <!here>"
        ) == "<@U123> see <https://example.com/?a=1&b=2|Q&A> in <#C1|general> <!here>"


# ── a message event ──


def _orch() -> MagicMock:
    from slack_runtime.settings import ACTIVATION_ALWAYS, SlackSettings
    import slack_runtime.settings as _st

    settings = SlackSettings(channels={}, dm_activation=ACTIVATION_ALWAYS)
    _st._current = settings
    orch = MagicMock()
    orch.settings = settings
    orch.channel_history = MagicMock()
    orch.slack = AsyncMock()
    orch.sessions = MagicMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.has_session = MagicMock(return_value=False)
    orch.ctx_builder = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


def _route(orch, event, *, is_mention=False):
    """Hand *event* to the app as Slack's socket would, and return what each consumer got."""
    from slack_runtime.events import _route_message

    with patch("slack_runtime.events.is_allowed_user", return_value=True), \
         patch("slack_runtime.events.check_message_origin", return_value=True), \
         patch("slack_runtime.events.publish_inbound_for_trigger_source") as published, \
         patch("slack_runtime.events.handle_message", new_callable=AsyncMock) as handled:

        async def go():
            await _route_message(orch, event, is_mention=is_mention)
            await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)

        asyncio.run(go())
    return handled, published


def _dm(text: str, ts: str = "1700000001.000100") -> dict:
    return {"type": "message", "user": "U1", "text": text, "ts": ts, "channel": "D1",
            "channel_type": "im", "team": "T1", "event_ts": ts}


def test_the_agent_the_history_and_automations_get_what_was_typed():
    orch = _orch()
    handled, published = _route(orch, _dm(SENT))

    assert handled.await_args.args[3] == TYPED
    orch.channel_history.push.assert_called_once_with("D1", "U1", TYPED, thread_ts=None)
    assert published.call_args.kwargs["text"] == TYPED


def test_a_message_waiting_for_a_busy_session_is_kept_as_typed():
    orch = _orch()
    orch._session_tasks["1700000001.000100"] = MagicMock()
    orch.sessions.enqueue.return_value = True
    _route(orch, _dm(SENT))

    assert orch.sessions.enqueue.call_args.args[2] == TYPED


def test_a_mention_of_the_bot_is_taken_off_and_a_typed_one_is_kept():
    orch = _orch()
    mention = {**_dm("<@UBOT> what does &lt;br&gt; do?"), "channel": "C1",
               "channel_type": "channel", "type": "app_mention"}
    handled, _ = _route(orch, mention, is_mention=True)
    assert handled.await_args.args[3] == "what does <br> do?"

    orch = _orch()
    typed = {**_dm("&lt;@U999&gt; is how a mention is written, <@UBOT>", ts="1700000002.000100"),
             "channel": "C1", "channel_type": "channel", "type": "app_mention"}
    handled, _ = _route(orch, typed, is_mention=True)
    assert handled.await_args.args[3] == "<@U999> is how a mention is written, <@UBOT>"


# ── a message read back from Slack's API ──


def _client(messages: list[dict]) -> RealSlackClient:
    client = RealSlackClient.__new__(RealSlackClient)
    client._web = MagicMock()
    client._web.conversations_history = AsyncMock(return_value={"messages": messages})
    return client


def test_a_threads_first_message_is_read_as_typed():
    client = _client([{"text": SENT}])
    assert asyncio.run(client.fetch_message("C1", "1.1")) == TYPED


def test_a_section_is_read_as_typed_and_rich_text_is_kept_as_slack_sends_it():
    """Slack spells a mrkdwn text with the entities. A rich text element carries the characters
    themselves, so an ``&amp;`` in one is what its sender typed."""
    client = _client([{
        "text": "fallback",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Q3 &lt;draft&gt; &amp; notes"}},
            {"type": "section", "text": {"type": "plain_text", "text": "a &amp; b, as written"}},
            {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
                {"type": "text", "text": "Tom &amp; Jerry < 3"},
            ]}]},
        ],
    }])
    assert asyncio.run(client.fetch_message("C1", "1.1")) == (
        "Q3 <draft> & notes\na &amp; b, as written\nTom &amp; Jerry < 3"
    )


def test_a_thread_linked_to_the_dashboard_is_imported_as_typed(monkeypatch):
    from slack_runtime import interactions

    # A member this app lets in: a message from anyone else is imported fenced, as data.
    monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: uid == "U1")
    session = MagicMock()
    ds = MagicMock()
    ds.get_linked_session = MagicMock(return_value=None)
    ds.get_or_create_session = MagicMock(return_value=session)
    ds._self_bot_id = "B1"
    slack = MagicMock()
    slack.fetch_thread_replies = AsyncMock(return_value=[
        {"user": "U1", "text": SENT},
        {"bot_id": "B1", "text": "Yes: 3 &lt; 5"},
    ])
    with patch("personalclaw.sdk.channel.save_session_to_history"):
        asyncio.run(interactions._import_thread_to_session(slack, ds, "C1", "100.0"))

    assert [c.args[:2] for c in session.append.call_args_list] == [
        ("user", TYPED), ("assistant", "Yes: 3 < 5"),
    ]


# ── the Inbox ──


class _History:
    """A channel's history as ``conversations.history`` answers it: one page, newest first."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages

    async def fetch_history(self, channel, oldest, limit=200, *, latest=""):
        top = float(latest) if latest else float("inf")
        return [m for m in self._messages if float(oldest) < float(m["ts"]) < top], False

    async def get_user_info(self, user_id):
        return {"real_name": "Alice Example"}


def test_an_inbox_message_and_the_digest_history_read_as_typed():
    history = _History([{"ts": "1700000001.000100", "user": "U_ALICE", "text": SENT}])
    src = SlackInboxSource({"bot_token": "fake-bot-token-test"}, client=history)
    messages, _ = asyncio.run(src.poll(["C1"], {"C1": "0"}, "U_ME"))
    assert [m.text for m in messages] == [TYPED]

    rows = asyncio.run(src.get_channel_history("C1", "0", 10))
    assert [r["text"] for r in rows] == [TYPED]


@pytest.mark.parametrize("text", ["", "plain words", "<@U123> <#C1|general> <!here>"])
def test_text_with_nothing_to_decode_is_unchanged(text):
    assert slack_text(text) == text
