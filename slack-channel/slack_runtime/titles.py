"""A thread title out of a title-generation reply: the title, never the label or scaffolding.

Core's rules for the dashboard (#3590), applied to Slack's auto-titles. The reply's first line used
to be taken as-is, so a model that echoed the prompt's shape named the thread ``Title: Example
Site Docs``, ``TAGS: Planned, Review``, ``Chat title:`` or ```` ```python ````. Core does not
publish its parser (it is private to ``personalclaw.dashboard.chat_title``, outside
``personalclaw.sdk``), so the rules are mirrored here and ``tests/test_title_parse.py`` runs this
copy and core's over the same replies on the installed core — a change to core's rules fails there.
"""

from __future__ import annotations

import logging
import re

from personalclaw.sdk.channel import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: A code-fence line (three or more backticks or tildes, with or without an info string).
_FENCE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})")

#: A tag line (``TAGS: a, b``), bold label and all — never a title.
_TAGS_LINE_RE = re.compile(r"^[\s*`]*tags[\s*`]*:[\s*`]*", re.IGNORECASE)

#: Markdown structure a model puts in front of a line: a heading, a quote, a bullet, a list number.
_LEAD_MARK_RE = re.compile(r"^(?:#{1,6}\s+|>\s*|[-*+•]\s+|\d{1,2}[.)]\s+)+")

#: A label echoed before the title. A closed vocabulary rather than "any words before a colon", so
#: a real title with a colon in it (``Movie Titles: Best of 2025``) is left alone.
_LABEL_RE = re.compile(
    r"^\**\s*"
    r"(?:(?:sure|ok|okay|certainly)[!,.]?\s+)?"
    r"(?:(?:here's|here’s|here\s+is|a|an|the|my|suggested|proposed|short|brief|final|new|chat|"
    r"conversation|session)\s+){0,4}"
    r"(?:title(?:\s+(?:for|of)\s+(?:this|the)\s+(?:conversation|chat|session))?|topic|subject)"
    r"\s*\**\s*(?::|\s[-–—](?=\s))\s*\**\s*",
    re.IGNORECASE,
)

#: A whole line wrapped in one emphasis/code marker (``**X**``, ``*X*``, `` `X` ``).
_WRAPPED_RE = re.compile(r"(\*\*|\*|`)(.+?)\1")

#: Quote characters a title is wrapped in; single quotes only when they wrap BOTH ends, so an
#: apostrophe in the title itself survives.
_DOUBLE_QUOTES = '"“”«»「」'
_SINGLE_QUOTES = "'‘’"

#: The longest title kept — core's ceiling for the same field.
MAX_TITLE_CHARS = 60


def _unwrap(line: str) -> str:
    while True:
        m = _WRAPPED_RE.fullmatch(line)
        if m is None:
            return line
        line = m.group(2).strip()


def _clean_title_line(line: str) -> str:
    """One reply line reduced to the words a title could be; ``""`` for a bare label."""
    s = _unwrap(_LEAD_MARK_RE.sub("", line.strip()))
    label = _LABEL_RE.match(s)
    if label is not None:
        s = s[label.end() :]
    s = _unwrap(s.strip().rstrip("*").strip())
    s = s.strip(_DOUBLE_QUOTES).strip()
    if len(s) >= 2 and s[0] in _SINGLE_QUOTES and s[-1] in _SINGLE_QUOTES:
        s = s[1:-1].strip()
    return s.rstrip(".").strip()


def parse_title(text: str) -> str:
    """The title in a raw title-generation reply, or ``""`` when it holds no plausible one.

    ``""`` is the caller's cue to leave the thread untitled and retry on the next exchange. The
    first line with words is the candidate: fenced code, the tag line, bare labels, lead-ins ending
    in ``:`` and punctuation-only lines are skipped to reach it; the candidate is then accepted or
    refused as a whole, never traded for a later line.
    """
    in_fence = False
    for raw in text.splitlines():
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence or not raw.strip() or _TAGS_LINE_RE.match(raw):
            continue
        title = _clean_title_line(raw)
        if title.endswith(":") or not any(ch.isalnum() for ch in title):
            continue
        return _accept_title(title)
    return ""


def _accept_title(title: str) -> str:
    """*title* redacted, or ``""`` for a SKIP, a conversation continuation, or an over-long line."""
    if title.upper() == "SKIP":
        return ""
    if title.lower().startswith(("user:", "assistant:")) or len(title) > MAX_TITLE_CHARS:
        logger.info("Slack title rejected (looks like a continuation): %r", title[:80])
        return ""
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    return title[:MAX_TITLE_CHARS]
