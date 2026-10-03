"""A rich message the agent writes in Block Kit shows its text and images, and has nothing to press.

Slack sends a press on a button, a pick from a menu or a date, or a typed input to this app as an
action, and this app answers its own controls (an approval's buttons, the options, a scheduled
result's Acknowledge) by the action's id, which whoever writes the blocks chooses. So a block the
agent wrote keeps what it shows and leaves out everything a person could press, pick or type into:
the only controls on a message are this app's own.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from slack_runtime.client import RealSlackClient
from slack_runtime.delivery import SlackDelivery


def _sent(blocks: list[dict]) -> list[dict]:
    """The blocks a rich message the agent wrote reaches Slack with."""
    web = MagicMock()
    web.chat_postMessage = AsyncMock(return_value={"ts": "1.1"})
    client = RealSlackClient.__new__(RealSlackClient)
    client._web = web
    asyncio.run(SlackDelivery(client, owner=lambda: "U1").deliver_rich("D1", blocks, "Q3 report"))
    return web.chat_postMessage.await_args.kwargs["blocks"]


def _button(action_id: str, label: str) -> dict:
    return {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": label}}


def test_what_it_shows_is_kept():
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Q3 report"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Revenue is up"},
         "fields": [{"type": "mrkdwn", "text": "*Web* green"}],
         "accessory": {"type": "image", "image_url": "https://charts.example.com/q3.png",
                       "alt_text": "Q3 chart"}},
        {"type": "divider"},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": "From the weekly run"},
            {"type": "image", "image_url": "https://charts.example.com/run.png",
             "alt_text": "run"},
        ]},
        {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
            {"type": "text", "text": "Details in "},
            {"type": "link", "url": "https://reports.example.com/q3", "text": "the report"},
        ]}]},
    ]
    sent = _sent(blocks)
    assert [b["type"] for b in sent] == ["header", "section", "divider", "context", "rich_text"]
    assert sent[1]["accessory"]["type"] == "image"
    assert [e["type"] for e in sent[3]["elements"]] == ["mrkdwn", "image"]


def test_nothing_a_person_presses_picks_or_types_into_is_sent():
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Your report is ready"},
         "accessory": _button("open_report", "Open")},
        {"type": "actions", "elements": [
            _button("show_details", "Show details"),
            {"type": "static_select", "action_id": "pick_week",
             "placeholder": {"type": "plain_text", "text": "Week"},
             "options": [{"text": {"type": "plain_text", "text": "This week"}, "value": "w1"}]},
            {"type": "datepicker", "action_id": "pick_day"},
        ]},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": "Reply to change it"},
            _button("change_it", "Change"),
        ]},
        {"type": "input", "label": {"type": "plain_text", "text": "Note"},
         "element": {"type": "plain_text_input", "action_id": "note"}},
    ]
    sent = _sent(blocks)
    assert [b["type"] for b in sent] == ["section", "context"]
    assert "accessory" not in sent[0]
    assert [e["type"] for e in sent[1]["elements"]] == ["mrkdwn"]
    assert "action_id" not in json.dumps(sent)


def test_a_block_slack_does_not_document_is_left_out():
    """Blocks are kept by name: one this app does not know shows nothing it can vouch for."""
    sent = _sent([
        {"type": "section", "text": {"type": "mrkdwn", "text": "Kept"}},
        {"type": "carousel", "elements": [_button("next", "Next")]},
    ])
    assert [b["type"] for b in sent] == ["section"]
