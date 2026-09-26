"""A Slack thread is titled with the title, never the label or the scaffolding around it.

The auto-title path stored the reply's first line with quotes and dots trimmed, so the labels
core's #3590 measured on real models reached Slack verbatim: ``Title: Example Site Docs``,
``TAGS: Planned, Review``, ``Chat title:``, ```` ```python ````. ``slack_runtime.titles`` applies
core's rules; the parity test below runs it and core's own parser over the same replies on the
INSTALLED core, so a change to core's rules fails here rather than drifting.
"""

from __future__ import annotations

import pytest
from slack_helpers import MockSlackClient

from personalclaw.dashboard.chat_title import _parse_title as core_parse_title
from personalclaw.llm.base import LLMEvent
from slack_runtime.titles import parse_title

#: #3590's corpus: every reply its validators recorded, plus the real titles that must survive.
REPLIES = [
    "Title: Example Site Docs",
    "Title: France Capital",
    "Title: Example Site Docs\nTAGS: Planned, Review",
    "Title: TYPED-DURING-FAILURE",
    "TAGS: Planned, Review",
    "Chat title:",
    "```python",
    "```python\ndef reverse(s):\n    return s[::-1]\n```",
    "Chat title:\nBuild Log Failure Summary",
    "TAGS: Planned, Review\nExample Docs",
    "**Title:** Offsite Planning",
    "**Title**: Offsite Planning",
    "**Title: Offsite Planning**",
    "## Title: Offsite Planning",
    "# Offsite Planning",
    "Conversation title: Offsite Planning",
    "Suggested title - Offsite Planning",
    "Here is a short title for this conversation: Offsite Planning",
    "Sure! Here's a short title:\n\nOffsite Planning",
    'Title: "Offsite Planning"',
    "“Offsite Planning”",
    "Offsite Planning.",
    "- Offsite Planning",
    "```\ncode\n```\nString Reversal",
    "Learning C#",
    ".NET Dependency Injection",
    "__init__ vs __new__",
    "Movie Titles: Best of 2025",
    "Title IX Compliance Questions",
    "A* Search in Python",
    "Rock 'n' Roll History",
    "Node.js Streams",
    "",
    "   \n  ",
    "SKIP",
    "**SKIP**",
    "user: and another thing",
    "assistant: sure, here you go",
    "---",
    "x" * 61,
    "Title: see https://example.com/abc for the docs",
]


@pytest.mark.parametrize("reply", REPLIES)
def test_the_app_parses_a_title_exactly_as_core_does(reply):
    assert parse_title(reply) == core_parse_title(reply)


@pytest.mark.parametrize(
    ("reply", "title"),
    [
        ("Title: Example Site Docs\nTAGS: Planned, Review", "Example Site Docs"),
        ("**Title:** Offsite Planning", "Offsite Planning"),
        ("Movie Titles: Best of 2025", "Movie Titles: Best of 2025"),
    ],
)
def test_labels_are_stripped_and_real_titles_kept(reply, title):
    assert parse_title(reply) == title


@pytest.mark.parametrize("reply", ["TAGS: Planned, Review", "Chat title:", "```python", "SKIP"])
def test_scaffolding_alone_is_no_title(reply):
    assert parse_title(reply) == ""


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
