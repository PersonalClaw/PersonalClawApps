"""Slack message formatting — markdown to mrkdwn conversion.

Generic, channel-agnostic text helpers (``extract_options``,
``strip_thinking_tags``) live in :mod:`personalclaw.textfmt` and are re-exported
here for the Slack modules that use them alongside the mrkdwn/Block-Kit builders.
"""

import re
from typing import Any

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
            parts.append(f"*{escape_mrkdwn(choice[:72])}*")
        else:
            parts.append(f"~{escape_mrkdwn(choice[:73])}~")
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


def escape_mrkdwn(text: str) -> str:
    """*text* as Slack shows it, character for character.

    Slack reads ``&``, ``<`` and ``>`` as control characters: ``<…>`` is a mention (``<@U…>``,
    ``<!here>``, ``<!channel>``, ``<!everyone>``), a channel (``<#C…>``) or a link, and ``&``
    starts an entity. Each is sent as the entity Slack's escaping rules give it, so text a
    model or a sender wrote is shown as written and can never notify anyone. Nothing else is
    encoded: Slack decodes only these three."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


#: The entities Slack spells ``&``, ``<`` and ``>`` as in a message's text, and the characters.
_SLACK_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">"}
_SLACK_ENTITY_RE = re.compile("|".join(_SLACK_ENTITIES))


def slack_text(text: str) -> str:
    """A message's text from Slack as its sender typed it: :func:`escape_mrkdwn` read back.

    Slack sends ``&``, ``<`` and ``>`` in a message's text as ``&amp;``, ``&lt;`` and ``&gt;``,
    because it reads the characters themselves as its own markup. Each of the three is turned back
    into its character in one pass, so ``&amp;lt;`` (someone who typed ``&lt;``) stays ``&lt;``.
    Nothing else is decoded, since Slack encodes nothing else: any other ``&…;`` is as typed. A
    mention (``<@U…>``, ``<!here>``), a channel (``<#C…|name>``) or a link
    (``<https://…|words>``) stays in Slack's spelling, the characters inside it read back too."""
    return _SLACK_ENTITY_RE.sub(lambda m: _SLACK_ENTITIES[m.group(0)], text)


#: The rich-text elements that notify someone: a person, a user group, or everyone in a channel
#: or the workspace.
_NOTIFYING_ELEMENTS = {"user": "user_id", "usergroup": "usergroup_id", "broadcast": "range"}


def verbatim_blocks(blocks: Any) -> Any:
    """*blocks* with every mrkdwn text in them sent ``verbatim``.

    Slack reads a mrkdwn text in a block that is not verbatim before it posts it, and turns a
    plain ``@here``, ``@channel`` or ``@everyone`` into the mention, and a ``#name`` into the
    channel. Nothing this app sends means one that way: a mention it means is written as Slack's
    ``<…>`` sequence, which a verbatim text still reads. So every mrkdwn text is verbatim,
    whoever wrote the blocks."""
    if isinstance(blocks, list):
        return [verbatim_blocks(b) for b in blocks]
    if isinstance(blocks, dict):
        out = {k: verbatim_blocks(v) for k, v in blocks.items()}
        if out.get("type") == "mrkdwn":
            out["verbatim"] = True
        return out
    return blocks


#: The blocks a model's rich message keeps: the ones that show something and have nothing to
#: press.
_SHOWN_BLOCKS = frozenset(
    {"section", "header", "divider", "context", "image", "rich_text", "markdown"}
)
#: What a context block of a model's keeps, and a section's accessory: text and images.
_SHOWN_ELEMENTS = frozenset({"mrkdwn", "plain_text", "image"})


def model_blocks(blocks: Any) -> Any:
    """Blocks a model wrote, as Slack shows them with nobody notified and nothing to press.

    Only the blocks that show something are kept (:data:`_SHOWN_BLOCKS`): a section's accessory
    only when it is an image, a context's elements only its text and images. A button, a menu, a
    date or time picker, an input, and a block this app does not know, are left out. Slack sends a
    press, a pick or an input on one to this app as an action, and this app answers its own controls
    (an approval's among them) by the action's id, which whoever writes the blocks chooses. So the
    only controls on a message are this app's own.

    A mrkdwn text in them is the model's markdown, converted as a reply is
    (:func:`to_slack_mrkdwn`): its links to web addresses are links and every other character is
    text. A markdown block's text is shown as written. A rich-text mention of a person, a user
    group or a whole channel is text, ``@`` and what it names (``@here``, a person's id), and
    notifies no one."""
    if not isinstance(blocks, list):
        return _model_text(blocks)
    shown: list[Any] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in _SHOWN_BLOCKS:
            continue
        accessory = block.get("accessory")
        if accessory is not None and not _shown_element(accessory):
            block = {k: v for k, v in block.items() if k != "accessory"}
        if block.get("type") == "context" and isinstance(block.get("elements"), list):
            block = {**block, "elements": [e for e in block["elements"] if _shown_element(e)]}
        shown.append(_model_text(block))
    return shown


def _shown_element(element: Any) -> bool:
    """Whether a context element or a section's accessory shows something and has nothing to
    press: text or an image."""
    return isinstance(element, dict) and element.get("type") in _SHOWN_ELEMENTS


def _model_text(node: Any) -> Any:
    """*node* of a model's blocks with its text shown as written and its mentions as words."""
    if isinstance(node, list):
        return [_model_text(n) for n in node]
    if not isinstance(node, dict):
        return node
    kind = node.get("type")
    if kind in _NOTIFYING_ELEMENTS:
        return {"type": "text", "text": f"@{node.get(_NOTIFYING_ELEMENTS[kind]) or kind}"}
    out = {k: _model_text(v) for k, v in node.items()}
    if isinstance(out.get("text"), str):
        if kind == "mrkdwn":
            out["text"] = to_slack_mrkdwn(out["text"])
        elif kind == "markdown":
            out["text"] = escape_mrkdwn(out["text"])
    return out


def to_slack_mrkdwn(text: str, *, keep_tables: bool = False) -> str:
    """Convert LLM markdown to Slack mrkdwn format.

    Every character of *text* reaches Slack as a character (:func:`escape_mrkdwn`), in code as
    well. The only markup that comes out is the formatting markdown asks for, and links: a
    markdown link, a link written in Slack's own syntax and a bare address become Slack links
    when they point at a web or mail address, and are text when they point anywhere else."""
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
            out.append(escape_mrkdwn(line))
        elif in_code:
            out.append(escape_mrkdwn(line))
        else:
            line = _convert_inline(line)
            out.append(line)
    return "\n".join(out)


# ── Inline conversions (outside code blocks) ──

#: What a line is read as, left to right: a code span (CommonMark: a run of backticks closed by
#: a run of exactly as many), which is sent as written; a markdown link [text](url); a link in
#: Slack's own syntax to a web address, <url|text> or <url>; or a bare web address. Whichever
#: starts first wins, so a link written inside code stays code, and a code span in a link's
#: text stays in the link.
_INLINE_RE = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`).+?(?<!`)(?P=ticks)(?!`))"
    r"|\[(?P<label>[^\]]+)\]\((?P<url>[^)]+)\)"
    r"|<(?P<slack_url>(?:https?://|mailto:)[^\s<>|]+)(?:\|(?P<slack_label>[^<>]*))?>"
    r"|(?P<bare>https?://[^\s<>|]+)"
)
#: The addresses a link may point at: the web and mail. Slack reads any other <…> as a mention
#: or a channel, so a link anywhere else is shown as the text that was written.
_LINKABLE_RE = re.compile(r"(?i)(?:https?://|mailto:)[^\s]")
#: What ends a sentence or closes emphasis right after an address, and is not part of it.
_URL_TRAILERS = ".,:;!?'\"*_~"
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
        return f"*{_inline(m.group(2).strip())}*"

    # Horizontal rule → unicode line
    if _HR_RE.match(line):
        return "─" * 30

    return _inline(line)


def _inline(text: str) -> str:
    """*text* as mrkdwn: its code spans and links as such, and every other character as text."""
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        out.append(_text(text[pos:m.start()]))
        if m.group("code"):
            out.append(escape_mrkdwn(m.group("code")))
        elif m.group("url") is not None:
            # [text](url) → <url|text>
            out.append(_link(m.group("url"), _inline(m.group("label")), m.group(0)))
        elif m.group("slack_url") is not None:
            label = m.group("slack_label")
            out.append(_link(m.group("slack_url"), _inline(label) if label else "", m.group(0)))
        else:
            url, trail = _trim_address(m.group("bare"))
            out.append(_link(url, "", url) + _text(trail))
        pos = m.end()
    out.append(_text(text[pos:]))
    return "".join(out)


def _link(url: str, label: str, written: str) -> str:
    """A Slack link to *url* showing *label* (mrkdwn), or *written* as text when *url* is not a
    web or mail address. The address is escaped too, and a ``|`` in it, which would end it, is
    percent-encoded."""
    url = url.strip()
    if not _LINKABLE_RE.match(url):
        return _text(written)
    target = escape_mrkdwn(url).replace("|", "%7C")
    return f"<{target}|{label}>" if label else f"<{target}>"


def _trim_address(url: str) -> tuple[str, str]:
    """``(address, rest)``: a bare address without the punctuation after it, and a closing
    bracket only when the address opened one."""
    end = len(url)
    while end > 0:
        ch = url[end - 1]
        if ch in _URL_TRAILERS or (ch == ")" and url[:end].count(")") > url[:end].count("(")):
            end -= 1
            continue
        break
    return url[:end], url[end:]


def _text(text: str) -> str:
    """Text outside code: escaped, and its markdown emphasis as mrkdwn's."""
    return _convert_emphasis(escape_mrkdwn(text))


def _convert_emphasis(text: str) -> str:
    """**bold** → *bold*, ~~strike~~ → ~strike~, in text outside code."""
    return _STRIKE_RE.sub(r"~\1~", text.replace("**", "*"))


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
    them outside code (the cut drops that space, as a line break is dropped at a cut). A cut
    never falls inside an escaped character (:func:`escape_mrkdwn`): half of ``&lt;`` shows as
    the letters it is made of."""
    prefix = line[:size]
    space = prefix.rfind(" ")
    if not in_code and space > 0 and prefix[:space].strip():
        return prefix[:space], line[space + 1:]
    amp = prefix.rfind("&", max(0, size - 4))
    if amp > 0 and ";" not in prefix[amp:]:
        return prefix[:amp], line[amp:]
    return prefix, line[size:]
