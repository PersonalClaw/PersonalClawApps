"""A Slack thread is titled with the title, never the label or the scaffolding around it.

The auto-title path stored the reply's first line with quotes and dots trimmed, so the labels
core's #3590 measured on real models reached Slack verbatim: ``Title: Example Site Docs``,
``TAGS: Planned, Review``, ``Chat title:``, ```` ```python ````. The handler now runs core's own
parser, ``personalclaw.sdk.channel.parse_title`` — the one the dashboard titles chats with — so
there is no copy of the rules here to drift from core's.
"""

from __future__ import annotations

import pytest
from slack_helpers import MockSlackClient

from personalclaw.sdk import channel as sdk_channel
from personalclaw.sdk.channel import LLMEvent


def test_the_handler_titles_threads_with_cores_parser():
    import slack_runtime.handler as h

    assert h.parse_title is sdk_channel.parse_title


# ── the auto-title path ───────────────────────────────────────────────────────


class _TitleModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def stream(self, prompt, timeout=120.0):
        yield LLMEvent(kind="text_chunk", text=self.reply)
        yield LLMEvent(kind="complete")

    async def reject_tool(self, rid):
        pass


class _Sessions:
    def __init__(self, reply: str) -> None:
        self.model = _TitleModel(reply)

    async def get_or_create(self, key, agent=None, channel_id=None, approval_policy=None):
        return self.model, False, False

    def release(self, key):
        pass


@pytest.fixture(autouse=True)
def _fresh_title_state():
    import slack_runtime.handler as h

    h._titled_threads.clear()
    h._auto_title_lock = None
    yield
    h._titled_threads.clear()
    h._auto_title_lock = None


async def _auto_title(reply: str, key: str) -> MockSlackClient:
    from slack_runtime.handler import _mark_titled, _maybe_auto_title_slack

    slack = MockSlackClient()
    _mark_titled(key)
    await _maybe_auto_title_slack(slack, _Sessions(reply), "C1", key, None, "the ask", "the answer")
    return slack


def _titles(slack: MockSlackClient) -> list[str]:
    return [detail["title"] for kind, detail in slack.actions if kind == "set_thread_title"]


@pytest.mark.asyncio
async def test_an_echoed_label_is_not_the_threads_title():
    slack = await _auto_title("Title: Example Site Docs\nTAGS: Planned, Review", "t1")
    assert _titles(slack) == ["Example Site Docs"]


@pytest.mark.asyncio
async def test_a_reply_with_no_title_leaves_the_thread_untitled_and_retries():
    from slack_runtime.handler import _titled_threads

    slack = await _auto_title("```python\nprint('hi')\n```", "t2")
    assert _titles(slack) == []
    assert "t2" not in _titled_threads  # released, so the next exchange tries again
