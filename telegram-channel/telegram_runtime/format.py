r"""Telegram MarkdownV2 formatting — the classic escaping footgun, contained here.

Telegram's ``MarkdownV2`` parse mode reserves eighteen characters
(``_ * [ ] ( ) ~ ` > # + - = | { } . !``) that MUST be backslash-escaped
*everywhere they are not part of a markup entity*, or the Bot API rejects the
whole message with ``400 Bad Request: can't parse entities``. The escaping rules
differ by context:

* **plain text** — every reserved char is escaped.
* **inside inline code / pre blocks** — only `` ` `` and ``\`` are escaped.
* **inside a link/emoji URL** — only ``)`` and ``\`` are escaped.

LLM output is CommonMark-ish, so this module converts the common constructs
(``**bold**`` → ``*bold*``, ``__``/``_italic_``, fenced/inline code, ``[t](u)``
links) into MarkdownV2 while escaping the reserved set correctly per context. It
is deliberately conservative: anything it does not recognize as markup is treated
as plain text and fully escaped, so a message never fails to parse. That is the
contract the table-driven tests pin.

A reply longer than one message is split by :func:`render_parts` BEFORE it is
rendered, never after: a cut through rendered MarkdownV2 lands inside a code block
or between an escape's backslash and its character, and Telegram refuses the part.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# The full MarkdownV2 reserved set (Bot API docs, "MarkdownV2 style").
_RESERVED = r"_*[]()~`>#+-=|{}.!"
_RESERVED_SET = frozenset(_RESERVED)

TELEGRAM_MAX_TEXT = 4096  # Bot API hard limit for a single sendMessage text.

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def escape_markdown_v2(text: str) -> str:
    """Escape every reserved MarkdownV2 char in *plain* text (no markup honored).

    Use this for a string that must render verbatim (a user name, a raw value).
    :func:`to_markdown_v2` is the richer path that preserves markup."""
    out = []
    for ch in text:
        if ch in _RESERVED_SET:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def escape_code(text: str) -> str:
    """Escape for inside an inline-code / pre block: only `` ` `` and ``\\``."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def escape_link_url(url: str) -> str:
    """Escape for inside a ``(url)`` link target: only ``)`` and ``\\``."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


# ── Markdown → MarkdownV2 with correct per-context escaping ──

# A fenced code block: ```lang\n...\n``` (lang optional). Non-greedy body.
_FENCE_RE = re.compile(r"```(?:[^\n`]*)\n?(.*?)```", re.DOTALL)
# Inline code: `code` (single backticks, no embedded backtick).
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# A markdown link [text](url).
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# Bold: **text** or __text__ (kept as *text* in V2).
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
# Italic: *text* or _text_ (kept as _text_ in V2).
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)|(?<!_)_(?!_)([^_\n]+?)_(?!_)")


def to_markdown_v2(text: str) -> str:
    """Convert LLM markdown to a valid Telegram MarkdownV2 string.

    Preserves fenced/inline code, links, bold and italic; fully escapes all other
    reserved characters so the Bot API always accepts the message. Length is not
    this function's concern — :func:`render_parts` splits the source first and
    renders each part with this."""
    text = _ANSI_RE.sub("", text)

    # Tokenize by extracting entities (code fences, inline code, links) into
    # placeholders so their bodies escape by their OWN rules, then escape the
    # remaining plain text, then splice the entities back in.
    entities: list[str] = []

    def _stash(rendered: str) -> str:
        entities.append(rendered)
        return f"\x00{len(entities) - 1}\x00"

    def _fence(m: re.Match) -> str:
        return _stash(f"```\n{escape_code(m.group(1).rstrip(chr(10)))}\n```")

    def _inline(m: re.Match) -> str:
        return _stash(f"`{escape_code(m.group(1))}`")

    def _link(m: re.Match) -> str:
        label = escape_markdown_v2(m.group(1))
        return _stash(f"[{label}]({escape_link_url(m.group(2))})")

    text = _FENCE_RE.sub(_fence, text)
    text = _INLINE_CODE_RE.sub(_inline, text)
    text = _LINK_RE.sub(_link, text)

    # Bold / italic → V2 markers around ESCAPED inner text. Do bold first so the
    # italic pass does not see the ** markers.
    def _bold(m: re.Match) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return _stash(f"*{escape_markdown_v2(inner)}*")

    def _italic(m: re.Match) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return _stash(f"_{escape_markdown_v2(inner)}_")

    text = _BOLD_RE.sub(_bold, text)
    text = _ITALIC_RE.sub(_italic, text)

    # Everything left is plain text: escape the full reserved set. The placeholder
    # sentinel (\x00) is not reserved, so it survives.
    text = escape_markdown_v2(text)

    # Splice entities back (placeholders were escaped to \\x00N\\x00 — the digits
    # are unescaped, \x00 unescaped; match the escaped form).
    def _restore(m: re.Match) -> str:
        return entities[int(m.group(1))]

    text = re.sub("\x00([0-9]+)\x00", _restore, text)
    return text


# ── Splitting a reply into messages ──

#: A line that opens or closes a fenced code block (CommonMark allows three spaces of indent).
_FENCE_LINE_RE = re.compile(r"^ {0,3}```")
#: The longest info string (a code block's language) carried onto a block reopened in the next
#: part. Anything longer, or with a space or backtick in it, is not a language tag, and the
#: reopened block goes without it.
_MAX_FENCE_INFO = 32
_CLOSE_FENCE = "```"


def utf16_len(text: str) -> int:
    """A text's length as Telegram counts it: UTF-16 code units, so most emoji are two."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class MessagePart:
    """One Telegram message of a reply.

    ``markdown_v2`` is what is sent, with ``parse_mode="MarkdownV2"``. ``plain`` is the part's
    own source text, sent with no parse mode when Telegram refuses the rendering, so a
    formatting the Bot API will not take costs the part its formatting, never the part."""

    markdown_v2: str
    plain: str


def render_parts(text: str, limit: int = TELEGRAM_MAX_TEXT) -> list[MessagePart]:
    """*text* as the Telegram messages it takes, each rendered to MarkdownV2 on its own.

    The SOURCE is split, never the rendering: at line breaks, into the longest runs of whole
    lines whose rendering and whose own text both fit *limit* in UTF-16 code units (the Bot
    API's count, after parsing — the rendering is never shorter, so it is the safe measure).
    Each run is then rendered by :func:`to_markdown_v2` alone, which is what makes every part
    valid MarkdownV2 by itself: nothing can be left open across a cut the renderer never saw.

    A code block cut in two is closed at the end of one part and opened again, with its
    language, at the start of the next, so both halves arrive as code. A line too long for a
    message of its own is cut at its last space that fits, or where it has to be in code or in
    a run with no space."""
    source = _ANSI_RE.sub("", text)
    if not source.strip():
        return []

    def fits(chunk: str) -> bool:
        return max(utf16_len(to_markdown_v2(chunk)), utf16_len(chunk)) <= limit

    return [MessagePart(to_markdown_v2(chunk), chunk) for chunk in _split_source(source, fits)]


def _split_source(source: str, fits: Callable[[str], bool]) -> list[str]:
    """*source* cut into chunks that each ``fits``, at line breaks wherever one does."""
    lines = source.split("\n")
    chunks: list[str] = []
    i = 0
    #: The opening line of the code block line ``i`` is in; "" outside one.
    fence = ""
    while i < len(lines):
        if not fence:
            # Outside code, the break between two messages already separates them: a part
            # does not start on blank lines.
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i == len(lines):
                break
        head = [_reopening(fence)] if fence else []
        body: list[str] = []
        state = fence
        while i < len(lines):
            after = _fence_after(state, lines[i])
            if not fits(_chunk(head + body + [lines[i]], after)):
                break
            body.append(lines[i])
            state = after
            i += 1
        if i < len(lines) and len(body) > 1 and _opens(body[-1], state):
            # A part does not end on the line that opens a code block: the block would
            # arrive empty, and its code as the next part's.
            body.pop()
            i -= 1
            state = ""
        if i < len(lines) and (not body or _opens(body[-1], state)):
            # The next line does not fit even at the start of a part: its longest piece
            # that does ends this part, and the rest of it starts the next.
            piece, rest = _cut(lines[i], head + body, state, fits)
            body.append(piece)
            state = _fence_after(state, piece)
            lines[i] = rest
        if not state:
            while body and not body[-1].strip():
                body.pop()
        chunks.append(_chunk(head + body, state))
        fence = state
    return chunks


def _chunk(lines: list[str], fence: str) -> str:
    """*lines* as one part's source, its code block closed when the part ends inside one."""
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
    """The line that reopens the code block *fence* opened, in the next part."""
    info = fence[3:].strip()
    if info and len(info) <= _MAX_FENCE_INFO and " " not in info and "`" not in info:
        return f"{_CLOSE_FENCE}{info}"
    return _CLOSE_FENCE


def _cut(line: str, before: list[str], fence: str, fits: Callable[[str], bool]) -> tuple[str, str]:
    """``(piece, rest)``: the longest start of *line* that still fits after *before*.

    Outside code it ends at the piece's last space, which the cut drops as a line break
    would be; code is cut where it must be, with nothing dropped. At least one character is
    always taken, so a line of any length is consumed."""
    lo, hi = 1, len(line)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(_chunk(before + [line[:mid]], _fence_after(fence, line[:mid]))):
            lo = mid
        else:
            hi = mid - 1
    prefix = line[:lo]
    space = prefix.rfind(" ")
    if not fence and space > 0 and prefix[:space].strip():
        return prefix[:space], line[space + 1:]
    return prefix, line[lo:]
