"""Text a model or a sender wrote reaches Slack as the characters it is, and notifies no one.

Slack reads ``&``, ``<`` and ``>`` as control characters: ``<!channel>``, ``<!here>`` and
``<!everyone>`` notify a whole channel or workspace, ``<@U…>`` notifies a person, ``<#C…>`` names a
channel and ``<url|label>`` is a link. A reply, a scheduled result, a rich message and an approval
prompt all carry text a model wrote, and a model may only be repeating something it read. So each
of those characters goes as the entity Slack's escaping rules give it, in code as well; every
mrkdwn text in a block is verbatim, so Slack does not read a plain ``@here`` as the mention; and
the only links that come out point at web and mail addresses.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from personalclaw.llm.base import LLMEvent
from slack_runtime.client import RealSlackClient
from slack_runtime.delivery import SlackDelivery
from slack_runtime.format import build_options_selected_blocks, to_slack_mrkdwn

#: Slack's own syntax for a channel-wide mention, a person and the people online, as text.
_MENTIONS = [
    ("<!channel>", "&lt;!channel&gt;"),
    ("<@U123>", "&lt;@U123&gt;"),
    ("<!here>", "&lt;!here&gt;"),
]


def _wire() -> tuple[RealSlackClient, MagicMock]:
    """A real client over a recorded Web API: what it hands Slack is what these tests read."""
    web = MagicMock()
    web.chat_postMessage = AsyncMock(return_value={"ts": "1.1"})
    web.chat_update = AsyncMock(return_value={"ok": True})
    web.api_call = AsyncMock(return_value={"ts": "1.1"})
    client = RealSlackClient.__new__(RealSlackClient)
    client._web = web
    return client, web


def _mrkdwn_texts(blocks: list) -> list[dict]:
    """Every mrkdwn text object in *blocks*."""
    found: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if node.get("type") == "mrkdwn":
                found.append(node)
            for value in node.values():
                walk(value)

    walk(blocks)
    return found


@pytest.mark.parametrize(("written", "sent"), _MENTIONS)
def test_a_mention_in_a_reply_is_sent_as_text(written, sent):
    assert to_slack_mrkdwn(f"The page says {written} about the release.") == (
        f"The page says {sent} about the release."
    )


def test_ampersands_and_angle_brackets_show_as_written():
    assert to_slack_mrkdwn("R&D: 3 < 5 > 2") == "R&amp;D: 3 &lt; 5 &gt; 2"


def test_a_markdown_link_is_still_a_link():
    assert to_slack_mrkdwn("Read [the guide](https://example.com/guide?a=1&b=2) first.") == (
        "Read <https://example.com/guide?a=1&amp;b=2|the guide> first."
    )
    assert to_slack_mrkdwn("[**Q3** <draft>](https://example.com/q3)") == (
        "<https://example.com/q3|*Q3* &lt;draft&gt;>"
    )


def test_a_link_slack_cannot_open_is_shown_as_written():
    """A link to a file name or a relative path goes nowhere in Slack, and Slack would read a
    target that is not a web or mail address as a mention or a channel."""
    assert to_slack_mrkdwn("See [notes](notes.md).") == "See [notes](notes.md)."


def test_a_bare_address_is_a_link():
    """A block's text is verbatim, so Slack links no address in it by itself."""
    assert to_slack_mrkdwn("Read https://example.com/guide.") == "Read <https://example.com/guide>."


def test_code_keeps_its_content():
    """Slack shows an entity in code as its character, so code reads exactly as it was written."""
    assert to_slack_mrkdwn("Run `a < b && c > d` now") == "Run `a &lt; b &amp;&amp; c &gt; d` now"
    assert to_slack_mrkdwn("```html\n<a href='x'>R&D</a>\n```") == (
        "```html\n&lt;a href='x'&gt;R&amp;D&lt;/a&gt;\n```"
    )


def test_a_scheduled_result_notifies_no_one():
    client, web = _wire()
    asyncio.run(SlackDelivery(client, owner=lambda: "U1").deliver_cron_result(
        "C1", "Daily <digest>", "job-1", "Status for <!channel>: green, @here too."
    ))
    sent = web.chat_postMessage.await_args.kwargs
    (section, *_rest) = _mrkdwn_texts(sent["blocks"])
    assert section["text"] == (
        "⏰ *Cron: Daily &lt;digest&gt;*\n\nStatus for &lt;!channel&gt;: green, @here too."
    )
    assert section["verbatim"] is True
    assert "<" not in sent["text"]


def test_a_notification_title_is_sent_as_text():
    client, web = _wire()
    asyncio.run(SlackDelivery(client, owner=lambda: "U1").deliver_notification(
        "D1", "Mail from <ana@example.com>", "Lunch & notes"
    ))
    assert web.chat_postMessage.await_args.kwargs["text"] == (
        "💓 *Mail from &lt;ana@example.com&gt;*\n\nLunch &amp; notes"
    )


def test_a_rich_message_a_model_wrote_notifies_no_one():
    client, web = _wire()
    blocks = [
        {"type": "section", "text": {
            "type": "mrkdwn", "verbatim": False,
            "text": "Heads up <!channel>: see [the plan](https://example.com/plan)",
        }},
        {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
            {"type": "broadcast", "range": "here"},
            {"type": "user", "user_id": "U123"},
            {"type": "text", "text": " review this"},
        ]}]},
    ]
    asyncio.run(SlackDelivery(client, owner=lambda: "U1").deliver_rich(
        "C1", blocks, "Heads up <!here>"
    ))
    sent = web.chat_postMessage.await_args.kwargs
    (section,) = _mrkdwn_texts(sent["blocks"])
    assert section == {
        "type": "mrkdwn",
        "verbatim": True,
        "text": "Heads up &lt;!channel&gt;: see <https://example.com/plan|the plan>",
    }
    assert sent["blocks"][1]["elements"][0]["elements"] == [
        {"type": "text", "text": "@here"},
        {"type": "text", "text": "@U123"},
        {"type": "text", "text": " review this"},
    ]
    assert sent["text"] == "Heads up &lt;!here&gt;"


def test_every_block_this_app_sends_is_verbatim():
    """A plain @here in a block's mrkdwn is the mention unless the text is verbatim."""
    client, web = _wire()
    asyncio.run(client.post_blocks("C1", [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "seen by @here"}]},
    ], "fallback"))
    asyncio.run(client.update_message("C1", "1.1", text="x", blocks=[
        {"type": "section", "fields": [{"type": "mrkdwn", "text": "@channel"}]},
    ]))
    posted = web.chat_postMessage.await_args.kwargs["blocks"]
    updated = web.chat_update.await_args.kwargs["blocks"]
    assert [t["verbatim"] for t in _mrkdwn_texts(posted) + _mrkdwn_texts(updated)] == [True, True]


def test_a_streamed_reply_is_sent_as_text():
    client, web = _wire()
    asyncio.run(client.append_stream("C1", "1.1", "Reminder for <!here> & the team"))
    (chunk,) = web.api_call.await_args.kwargs["json"]["chunks"]
    assert chunk == {"type": "markdown_text", "text": "Reminder for &lt;!here&gt; &amp; the team"}


def test_the_reply_shown_while_it_is_written_is_converted():
    """Without a stream the reply so far is edited into one message, as mrkdwn like the final."""
    import slack_runtime.handler as H

    slack = MagicMock()
    slack.update_message = AsyncMock()
    asyncio.run(H._safe_update(slack, "C1", "1.1", "So far <@U123> **said**"))
    assert slack.update_message.await_args.args == ("C1", "1.1", "So far &lt;@U123&gt; *said*")


def test_an_approval_prompt_shows_the_call_as_written():
    import slack_runtime.handler as H

    brief = {
        "tool": "bash",
        "input": "echo '<!channel> & done'",
        "purpose": "Tell <@U123> it is done",
        "summary": "Can: writes files under <repo>/build",
        # What a call with no chat of its own offers: core's answers, as the brief carries them.
        "answers": [
            {"key": "approved", "label": "Allow once", "ends": "approved", "word": "APPROVE",
             "promise": ""},
            {"key": "rejected", "label": "Deny", "ends": "rejected", "word": "DENY", "promise": ""},
        ],
    }
    event = LLMEvent(
        kind="permission_request", request_id="req-text", title="bash", options=[],
        tool_input=brief["input"], tool_meta={"approval_brief": brief},
    )
    (blocks,) = H._approval_messages(event)
    assert [t["text"] for t in _mrkdwn_texts(blocks)][1:] == [
        "```\necho '&lt;!channel&gt; &amp; done'\n```",
        "Tell &lt;@U123&gt; it is done",
        "Can: writes files under &lt;repo&gt;/build",
    ]
    assert H._approval_fallback(event) == (
        "🔐 Approval needed: bash — Can: writes files under &lt;repo&gt;/build"
    )


def test_chosen_options_are_shown_as_written():
    (block,) = build_options_selected_blocks(["Tea & cake", "<!here>"], 0)
    assert block["elements"][0]["text"] == "*Tea &amp; cake*  |  ~&lt;!here&gt;~"


def test_option_buttons_carry_the_choices_own_words():
    """The choices are read off the reply before it is converted, so a button says ``&``."""
    client, web = _wire()
    asyncio.run(SlackDelivery(client, owner=lambda: "U1").deliver_chat_mirror(
        "C1", "Which one? [OPTIONS: Tea & cake | Coffee]"
    ))
    first, second = web.chat_postMessage.await_args_list
    assert first.kwargs["text"] == "Which one?"
    labels = [o["text"]["text"] for o in second.kwargs["blocks"][0]["elements"][0]["options"]]
    assert labels == ["Tea & cake", "Coffee"]
