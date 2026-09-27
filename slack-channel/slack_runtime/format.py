"""Slack message formatting — markdown to mrkdwn conversion.

Generic, channel-agnostic text helpers (``extract_options``,
``strip_thinking_tags``) live in :mod:`personalclaw.textfmt` and are re-exported
here for the Slack modules that use them alongside the mrkdwn/Block-Kit builders.
"""

import re

from personalclaw.sdk.channel import extract_options, strip_thinking_tags

__all__ = [
    "extract_options",
    "strip_thinking_tags",
]

SLACK_MAX_TEXT = 39_000

# Action ID prefix for OPTIONS buttons
OPTIONS_ACTION_PREFIX = "options_choice_"

# Action ID for OPTIONS checkboxes and submit
OPTIONS_CHECKBOXES_ACTION = "options_checkboxes"
OPTIONS_SUBMIT_ACTION = "options_submit"

# Action ID prefix for cron acknowledge buttons
CRON_ACK_ACTION_PREFIX = "cron_ack_"

# Action ID prefix for subagent acknowledge buttons
SUBAGENT_ACK_ACTION_PREFIX = "subagent_ack_"

# Action ID for link-to-dashboard button
LINK_DASHBOARD_ACTION = "pc_link_dashboard"


def build_options_blocks(choices: list[str]) -> list[dict]:
    """Build Slack Block Kit checkboxes + Send button for multi-select OPTIONS."""
    options = [
        {
            "text": {"type": "plain_text", "text": choice[:75]},
            "value": choice[:150],
        }
        for choice in choices[:10]  # checkboxes support up to 10
    ]
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "checkboxes",
                    "action_id": OPTIONS_CHECKBOXES_ACTION,
                    "options": options,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send"},
                    "action_id": OPTIONS_SUBMIT_ACTION,
                    "style": "primary",
                },
            ],
        },
    ]


def build_options_selected_blocks(choices: list[str], selected_indices: list[int] | int) -> list[dict]:
    """Render OPTIONS as static text with selected choices highlighted."""
    if isinstance(selected_indices, int):
        selected_indices = [selected_indices]
    selected_set = set(selected_indices)
    parts = []
    for i, choice in enumerate(choices[:10]):
        if i in selected_set:
            parts.append(f"*{choice[:72]}*")
        else:
            parts.append(f"~{choice[:73]}~")
    return [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  |  ".join(parts)}],
        }
    ]


def build_cron_ack_block(job_id: str) -> list[dict]:
    """Build a Slack Block Kit acknowledge button for cron notifications."""
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Acknowledge"},
                    "action_id": f"{CRON_ACK_ACTION_PREFIX}{job_id}",
                    "value": job_id,
                    "style": "primary",
                }
            ],
        }
    ]


def build_link_dashboard_button() -> dict:
    """Single button element for linking a Slack thread to the dashboard."""
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": "Link to Dashboard"},
        "action_id": LINK_DASHBOARD_ACTION,
    }


def to_slack_mrkdwn(text: str, *, keep_tables: bool = False) -> str:
    """Convert LLM markdown to Slack mrkdwn format."""
    text = _strip_ansi(text)

    if len(text) > SLACK_MAX_TEXT:
        cut = text[:SLACK_MAX_TEXT].rfind("\n") or SLACK_MAX_TEXT
        text = f"{text[:cut]}\n\n_…truncated ({len(text)} chars total)_"

    if not keep_tables:
        text = _convert_tables(text)
    text = _convert_mermaid(text)

    out: list[str] = []
    in_code = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            out.append(line)
        elif in_code:
            out.append(line)
        else:
            line = _convert_inline(line)
            out.append(line)
    return "\n".join(out)


# ── Inline conversions (outside code blocks) ──

# Markdown link [text](url) → Slack <url|text>
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
# Headings: # text → *text*
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
# Horizontal rule: --- or *** or ___ (3+ chars)
_HR_RE = re.compile(r"^[\s]*([-*_])\1{2,}\s*$")
# Strikethrough: ~~text~~ → ~text~
_STRIKE_RE = re.compile(r"~~(.+?)~~")


def _convert_inline(line: str) -> str:
    """Convert a single non-code line from markdown to Slack mrkdwn."""
    # Headings → bold
    m = _HEADING_RE.match(line)
    if m:
        return f"*{m.group(2).strip()}*"

    # Horizontal rule → unicode line
    if _HR_RE.match(line):
        return "─" * 30

    # **bold** → *bold*
    line = line.replace("**", "*")

    # ~~strike~~ → ~strike~
    line = _STRIKE_RE.sub(r"~\1~", line)

    # [text](url) → <url|text>
    line = _LINK_RE.sub(r"<\2|\1>", line)

    return line


# Markdown table: line starting with | and containing at least one more |
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+\|)\s*$")
# Separator row: only |, -, :, spaces
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")


def _convert_tables(text: str) -> str:
    """Convert markdown tables to vertical list format for mobile readability."""
    lines = text.split("\n")
    result: list[str] = []
    headers: list[str] = []
    data_rows: list[list[str]] = []

    def _flush_table() -> None:
        if not headers or not data_rows:
            return
        for row in data_rows:
            parts: list[str] = []
            for i, cell in enumerate(row):
                if not cell:
                    continue
                if i < len(headers):
                    parts.append(f"*{headers[i]}:* {cell}")
                else:
                    parts.append(cell)
            result.append("• " + " | ".join(parts))
        headers.clear()
        data_rows.clear()

    for line in lines:
        if _TABLE_ROW_RE.match(line):
            if _TABLE_SEP_RE.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not headers:
                headers.extend(cells)
            else:
                data_rows.append(cells)
        else:
            _flush_table()
            result.append(line)

    _flush_table()
    return "\n".join(result)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ── Mermaid → text ──

_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
# graph/flowchart edges: A[label] -->|text| B[label]  or  A --> B
_GRAPH_EDGE_RE = re.compile(
    r"(\w+)(?:\[([^\]]*)\]|\{([^}]*)\}|(?:\([^)]*\)))?"
    r"\s*(-->|---|-\.->|==>)(?:\|([^|]*)\|)?\s*"
    r"(\w+)(?:\[([^\]]*)\]|\{([^}]*)\}|(?:\([^)]*\)))?"
)
# sequence: Actor->>Actor: message
_SEQ_RE = re.compile(r"(\S+?)\s*(->>|-->>|->|-->)\s*(\S+?):\s*(.+)")


def _convert_mermaid(text: str) -> str:
    """Replace ```mermaid blocks with readable text diagrams."""

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        body = m.group(1).strip()
        first = body.split("\n", 1)[0].strip().lower()

        if first.startswith(("graph ", "flowchart ")):
            return _mermaid_graph(body)
        if first.startswith("sequencediagram"):
            return _mermaid_sequence(body)
        # Unknown diagram type — show as plain code block
        return f"```\n{body}\n```"

    return _MERMAID_BLOCK_RE.sub(_replace, text)


def _mermaid_graph(body: str) -> str:
    """Convert graph/flowchart to text arrows."""
    labels: dict[str, str] = {}
    edges: list[str] = []
    for line in body.split("\n")[1:]:  # skip "graph TD" line
        m = _GRAPH_EDGE_RE.search(line.strip())
        if not m:
            continue
        src, sl1, sl2, _, edge_label, dst, dl1, dl2 = m.groups()
        if sl1 or sl2:
            labels[src] = sl1 or sl2
        if dl1 or dl2:
            labels[dst] = dl1 or dl2
        src_name = labels.get(src, src)
        dst_name = labels.get(dst, dst)
        arrow = f" ({edge_label.strip()}) " if edge_label else " "
        edges.append(f"  {src_name} →{arrow}{dst_name}")
    return "\n".join(edges) if edges else body


def _mermaid_sequence(body: str) -> str:
    """Convert sequenceDiagram to text arrows."""
    lines: list[str] = []
    for line in body.split("\n")[1:]:  # skip "sequenceDiagram"
        m = _SEQ_RE.match(line.strip())
        if not m:
            continue
        src, arrow_type, dst, msg = m.groups()
        arrow = "→" if ">>" in arrow_type else "⇢"
        if "--" in arrow_type:
            arrow = "⇠" if ">>" in arrow_type else "⇠"
        lines.append(f"  {src} {arrow} {dst}: {msg.strip()}")
    return "\n".join(lines) if lines else body


# Slack message character limit (API rejects above ~4000)
SLACK_MSG_LIMIT = 3900

# Slack Block Kit `section.text` max_length. A plain chat.postMessage body may run to
# SLACK_MSG_LIMIT, but text placed in a section block is rejected (invalid_blocks) above
# this. Anything split for Block Kit delivery must use this bound, not SLACK_MSG_LIMIT.
SLACK_BLOCK_SECTION_LIMIT = 3000
TRUNCATION_NOTICE = "\n\n⚠️ _Response truncated (Slack message limit)_"
CONTINUATION = "\n\n_(continued…)_"

#: A line that opens or closes a fenced code block (CommonMark allows three spaces of indent).
_FENCE_LINE_RE = re.compile(r"^ {0,3}```")
#: The longest info string carried onto a code block reopened in the next message. Anything
#: longer, or with a space or backtick in it, is not a language tag.
_MAX_FENCE_INFO = 32
_CLOSE_FENCE = "```"


def split_message(text: str, limit: int = SLACK_MSG_LIMIT) -> list[str]:
    """Split text into chunks that fit within Slack's message limit.

    Splits at line breaks where it can, and every chunk but the last ends with
    :data:`CONTINUATION`. Slack renders each message's mrkdwn on its own, so a code block cut
    in two is closed at the end of one chunk and opened again at the start of the next, with
    the marker after the closing fence: cut anywhere, the first message kept its fence open
    (the marker inside the code) and the next showed the rest of the code as mrkdwn. A line
    longer than a whole chunk is cut at its last space that fits, or where it has to be in code
    (whose spaces are content) and in a run with no space.
    """
    if len(text) <= limit:
        return [text]

    lines = text.split("\n")
    parts: list[str] = []
    i = 0
    #: The opening line of the code block line ``i`` is in; "" outside one.
    fence = ""
    while i < len(lines):
        if not fence:
            # Outside code, the break between two messages already separates them: a
            # chunk does not start on blank lines.
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i == len(lines):
                break
        head = [_reopening(fence)] if fence else []
        # The last chunk carries no marker, so it may use the whole limit.
        final_state = fence
        for line in lines[i:]:
            final_state = _fence_after(final_state, line)
        rest = lines[i:]
        if not final_state:
            while rest and not rest[-1].strip():
                rest = rest[:-1]
        final = _chunk(head + rest, final_state)
        if len(final) <= limit:
            parts.append(final)
            break

        room = limit - len(CONTINUATION)
        body: list[str] = []
        state = fence
        while i < len(lines):
            after = _fence_after(state, lines[i])
            if len(_chunk(head + body + [lines[i]], after)) > room:
                break
            body.append(lines[i])
            state = after
            i += 1
        if i < len(lines) and len(body) > 1 and _opens(body[-1], state):
            # A chunk does not end on the line that opens a code block: the block would
            # arrive empty, and its code as the next chunk's.
            body.pop()
            i -= 1
            state = ""
        if i < len(lines) and (not body or _opens(body[-1], state)):
            # The next line does not fit even at the start of a chunk: its longest piece
            # that does ends this one, and the rest of it starts the next.
            before = len("\n".join(head + body + [""]))
            size = max(1, room - before - (len(_CLOSE_FENCE) + 1 if state else 0))
            piece, lines[i] = _cut(lines[i], size, in_code=bool(state))
            body.append(piece)
        if not state:
            while body and not body[-1].strip():
                body.pop()
        chunk = _chunk(head + body, state)
        if not state and not any(line.strip() for line in lines[i:]):
            # Only blank lines are left: this chunk is the last.
            parts.append(chunk)
            break
        parts.append(chunk + CONTINUATION)
        fence = state
    return parts or [""]


def _chunk(lines: list[str], fence: str) -> str:
    """*lines* as one message, its code block closed when the message ends inside one."""
    text = "\n".join(lines)
    return f"{text}\n{_CLOSE_FENCE}" if fence else text


def _fence_after(fence: str, line: str) -> str:
    """The code block the text is in after *line*: a fence line opens one, or closes it."""
    if not _FENCE_LINE_RE.match(line):
        return fence
    return "" if fence else line.strip()


def _opens(line: str, fence: str) -> bool:
    """Whether *line* opened the block *fence* (the state after it) is in."""
    return bool(fence) and bool(_FENCE_LINE_RE.match(line))


def _reopening(fence: str) -> str:
    """The line that reopens the code block *fence* opened, in the next message."""
    info = fence[3:].strip()
    if info and len(info) <= _MAX_FENCE_INFO and " " not in info and "`" not in info:
        return f"{_CLOSE_FENCE}{info}"
    return _CLOSE_FENCE


def _cut(line: str, size: int, *, in_code: bool) -> tuple[str, str]:
    """``(piece, rest)``: the first *size* characters of *line*, ended at the last space in
    them outside code (the cut drops that space, as a line break is dropped at a cut)."""
    prefix = line[:size]
    space = prefix.rfind(" ")
    if not in_code and space > 0 and prefix[:space].strip():
        return prefix[:space], line[space + 1:]
    return prefix, line[size:]
