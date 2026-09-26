"""The Home tab says when a newer PersonalClaw is available, in the field core actually sends.

``_publish_home_tab`` read ``remote_version`` from ``get_update_info()``; core's update check
writes ``latest`` (``dashboard/handlers/updates.py``: "it was previously emitted as
"remote_version", which nothing read"), so the "vX available" line could never render.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from slack_helpers import MockSlackClient

import personalclaw.dashboard.handlers.updates as updates
from slack_runtime.events import _publish_home_tab


def _version_line(slack: MockSlackClient) -> str:
    views = [detail["view"] for kind, detail in slack.actions if kind == "views_publish"]
    assert views, "the Home tab was never published"
    context = [b for b in views[-1]["blocks"] if b["type"] == "context"]
    return context[-1]["elements"][0]["text"]


def _orch() -> SimpleNamespace:
    return SimpleNamespace(
        sessions=None, vector_memory=None, ctx_builder=None,
        slack_command="personalclaw", slack=MockSlackClient(),
    )


@pytest.mark.asyncio
async def test_an_available_update_is_named_on_the_home_tab(monkeypatch):
    monkeypatch.setitem(updates._update_info, "available", True)
    monkeypatch.setitem(updates._update_info, "latest", "9.9.9")
    orch = _orch()

    await _publish_home_tab(orch, "U1")

    assert "🆕 v9.9.9 available — open Dashboard to update" in _version_line(orch.slack)


@pytest.mark.asyncio
async def test_no_update_says_only_the_running_version(monkeypatch):
    monkeypatch.setitem(updates._update_info, "available", False)
    monkeypatch.setitem(updates._update_info, "latest", "")
    orch = _orch()

    await _publish_home_tab(orch, "U1")

    line = _version_line(orch.slack)
    assert line.startswith("📦 PersonalClaw v")
    assert "available" not in line
