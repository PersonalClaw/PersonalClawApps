"""slack_runtime.format re-export identity — the app must re-export core
textfmt objects, not re-implement them (moved from core tests/test_textfmt.py)."""

from personalclaw.textfmt import extract_options, strip_thinking_tags


def test_slack_format_reexports_are_identical():
    """slack.format must re-export the SAME objects, not re-implement them."""
    from slack_runtime import format as slack_format

    assert slack_format.extract_options is extract_options
    assert slack_format.strip_thinking_tags is strip_thinking_tags
