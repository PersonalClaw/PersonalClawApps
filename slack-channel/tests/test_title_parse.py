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


def test_the_handler_titles_threads_with_cores_parser():
    import slack_runtime.handler as h

    assert h.parse_title is sdk_channel.parse_title


# ── the auto-title path ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_title_state():
    import slack_runtime.handler as h

    h._titled_threads.clear()
    yield
    h._titled_threads.clear()


async def _auto_title(reply: str, key: str, monkeypatch) -> MockSlackClient:
    """Title the thread *key* with a Background model that answers *reply*."""
    import slack_runtime.handler as h

    async def _chore(prompt, *, usage, validate=None):
        return reply

    monkeypatch.setattr(h, "run_chore", _chore, raising=False)
    slack = MockSlackClient()
    h._mark_titled(key)
    await h._maybe_auto_title_slack(slack, "C1", key, None, "the ask", "the answer")
    return slack


def _titles(slack: MockSlackClient) -> list[str]:
    return [detail["title"] for kind, detail in slack.actions if kind == "set_thread_title"]


@pytest.mark.asyncio
async def test_an_echoed_label_is_not_the_threads_title(monkeypatch):
    slack = await _auto_title("Title: Example Site Docs\nTAGS: Planned, Review", "t1", monkeypatch)
    assert _titles(slack) == ["Example Site Docs"]


@pytest.mark.asyncio
async def test_a_reply_with_no_title_leaves_the_thread_untitled_and_retries(monkeypatch):
    from slack_runtime.handler import _titled_threads

    slack = await _auto_title("```python\nprint('hi')\n```", "t2", monkeypatch)
    assert _titles(slack) == []
    assert "t2" not in _titled_threads  # released, so the next exchange tries again
