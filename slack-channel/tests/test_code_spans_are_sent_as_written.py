"""A code span in a reply reaches Slack exactly as it was written.

The markdown-to-mrkdwn pass rewrote ``**`` as ``*``, ``~~x~~`` as ``~x~`` and ``[t](u)`` as
``<u|t>`` everywhere on a line outside a fenced block, the inside of a code span included, so
``a**b`` in code arrived as ``a*b`` and a link written in code arrived as Slack link syntax.
Outside code the pass is unchanged, and it never touches an underscore: Slack does not read one
inside a word as italics.
"""

from __future__ import annotations

import pytest

from slack_runtime.format import to_slack_mrkdwn


@pytest.mark.parametrize(("source", "sent"), [
    ("`a**b` and **bold**", "`a**b` and *bold*"),
    ("`[x](https://example.com)` and [y](https://example.com/y_z)",
     "`[x](https://example.com)` and <https://example.com/y_z|y>"),
    ("`~~z~~` and ~~gone~~", "`~~z~~` and ~gone~"),
    ("``a ` **b**`` **c**", "``a ` **b**`` *c*"),
    ("- **Parcel `httpx.ReadTimeout`:** issue `A**B`", "- *Parcel `httpx.ReadTimeout`:* issue `A**B`"),
    ("[see `cfg_x`](https://example.com/x)", "<https://example.com/x|see `cfg_x`>"),
    ("`unclosed **bold**", "`unclosed *bold*"),
])
def test_a_code_span_is_sent_as_written(source, sent):
    assert to_slack_mrkdwn(source) == sent


@pytest.mark.parametrize("name", [
    "mcp/notes-vault/list_allowed_directories",
    "read_multiple_files",
    "snake_case and _italic_",
])
def test_underscores_are_left_to_slack(name):
    assert to_slack_mrkdwn(name) == name
