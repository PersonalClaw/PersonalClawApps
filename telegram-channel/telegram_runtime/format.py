r"""Telegram MarkdownV2 formatting — the classic escaping footgun, contained here.

Telegram's ``MarkdownV2`` parse mode reserves eighteen characters
(``_ * [ ] ( ) ~ ` > # + - = | { } . !``) that MUST be backslash-escaped
*everywhere they are not part of a markup entity*, or the Bot API rejects the
whole message with ``400 Bad Request: can't parse entities``. The backslash is the
escape itself, so a literal one is escaped too. The rules differ by context:

* **plain text** — every reserved char, and ``\``, is escaped.
* **inside inline code / pre blocks** — only `` ` `` and ``\`` are escaped.
* **inside a link/emoji URL** — only ``)`` and ``\`` are escaped.

LLM output is CommonMark, so :func:`to_markdown_v2` reads it the way CommonMark does and
writes what it read as MarkdownV2, escaping each character once, by the rules of the place it
lands in. Each line is read in one pass into a small tree: fenced code blocks, code spans,
backslash escapes, links, and bold, italic and ``~~strikethrough~~`` paired by CommonMark's
delimiter rules. Those rules are why an underscore inside a word (``list_allowed_directories``,
``snake_case``) is a character and never italics, and why a star with a space after it is a
star. Anything that does not read as markup is text and shows as written, so a message never
fails to parse. That is the contract the table-driven tests pin.

Telegram does not let code take other formatting, so bold or italics around a code span are
closed before it and opened again after it, and a code span inside a link's label shows as the
label's text.

A reply longer than one message is split by :func:`render_parts` BEFORE it is
rendered, never after: a cut through rendered MarkdownV2 lands inside a code block
or between an escape's backslash and its character, and Telegram refuses the part.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

# The full MarkdownV2 reserved set (Bot API docs, "MarkdownV2 style").
_RESERVED = r"_*[]()~`>#+-=|{}.!"
#: What plain text escapes: the reserved set, and the backslash that does the escaping.
_ESCAPED = frozenset(_RESERVED + "\\")

TELEGRAM_MAX_TEXT = 4096  # Bot API hard limit for a single sendMessage text.

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def escape_markdown_v2(text: str) -> str:
    """Escape every reserved MarkdownV2 char, and ``\\``, in *plain* text (no markup honored).

    Use this for a string that must render verbatim (a user name, a raw value, a tool's
    title). :func:`to_markdown_v2` is the richer path that preserves markup."""
    out = []
    for ch in text:
        if ch in _ESCAPED:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def escape_code(text: str) -> str:
    """Escape for inside an inline-code / pre block: only `` ` `` and ``\\``."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def escape_link_url(url: str) -> str:
    """Escape for inside a ``(url)`` link target: only ``)`` and ``\\``."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


# ── Markdown → MarkdownV2 ──

#: A line that opens a fenced code block (CommonMark): up to three spaces of indent, three or
#: more backticks, and an info string, the block's language, with no backtick in it.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,})([^`]*)$")
#: A line that closes one: at least as many backticks as opened it, and nothing after but spaces.
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,})[ \t]*$")

#: ASCII punctuation: what a backslash escapes in CommonMark.
_ASCII_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
#: A character that can start markup; everything between two of them is text.
_MARKUP_START = re.compile(r"[\\`*_~\[\]!]")
#: The MarkdownV2 marker for each emphasis a line can carry.
_BOLD, _ITALIC, _STRIKE = "*", "_", "~"
#: The addresses a link may point at: the web and mail. Telegram reads a link to
#: ``tg://user?id=…`` as a mention that notifies that person, so a link anywhere else is the
#: text that was written.
_LINKABLE_RE = re.compile(r"(?i)(?:https?://|mailto:)\S")


def to_markdown_v2(text: str) -> str:
    """Convert LLM markdown to a valid Telegram MarkdownV2 string.

    Preserves fenced code blocks, code spans, links, bold, italic and strikethrough; every other
    character shows as written, escaped once, so the Bot API always accepts the message. Length
    is not this function's concern — :func:`render_parts` splits the source first and renders
    each part with this."""
    out: list[str] = []
    fence = ""  # the line that opened the code block being read; "" outside one
    code: list[str] = []
    for line in _ANSI_RE.sub("", text).split("\n"):
        if fence:
            if _fence_after(fence, line):
                code.append(line)
            else:
                out.append(_pre(code))
                fence, code = "", []
        else:
            fence = _fence_after("", line)
            if not fence:
                out.append(_render_line(line))
    if fence:
        out.append(_pre(code))
    return "\n".join(out)


def _pre(lines: list[str]) -> str:
    """A fenced code block's lines as a MarkdownV2 pre block, exactly as they were written."""
    return f"```\n{escape_code(chr(10).join(lines))}\n```"


class _Text:
    """Text as written: what a backslash escaped, and every character that is not markup."""

    __slots__ = ("s",)

    def __init__(self, s: str) -> None:
        self.s = s


class _Mark(_Text):
    """A run of ``*``, ``_`` or ``~``, or a ``[``: markup until it is known not to be, then text.
    Never merged into the text beside it, so pairing can find it and spend its characters."""


class _Code:
    __slots__ = ("s",)

    def __init__(self, s: str) -> None:
        self.s = s


class _Span:
    """Bold, italic or strikethrough (its MarkdownV2 marker) over the nodes it holds."""

    __slots__ = ("marker", "children")

    def __init__(self, marker: str, children: list) -> None:
        self.marker = marker
        self.children = children


class _Link:
    __slots__ = ("children", "url")

    def __init__(self, children: list, url: str) -> None:
        self.children = children
        self.url = url


class _Delim:
    """A run of emphasis characters that may open or close (CommonMark's delimiter run)."""

    __slots__ = ("char", "count", "length", "can_open", "can_close", "node")

    def __init__(self, node: _Mark, can_open: bool, can_close: bool) -> None:
        self.node = node
        self.char = node.s[0]
        self.count = self.length = len(node.s)
        self.can_open = can_open
        self.can_close = can_close


class _Bracket:
    """A ``[`` or ``![`` a later ``](url)`` may make a link of."""

    __slots__ = ("node", "image", "bottom", "active")

    def __init__(self, node: _Mark, image: bool, bottom: int) -> None:
        self.node = node
        self.image = image
        self.bottom = bottom  # the delimiters opened before it are outside its label
        self.active = True


@lru_cache(maxsize=2048)
def _render_line(line: str) -> str:
    """One line outside code as MarkdownV2. Lines are independent, so :func:`render_parts`,
    which renders a growing part line by line to measure it, reads each line once."""
    out: list[str] = []
    _write(_parse_inline(line), out, in_link=False)
    return "".join(out)


def _parse_inline(line: str) -> list:
    """*line* as CommonMark inline content: text, code spans, links and emphasis."""
    nodes: list = []
    delims: list[_Delim] = []
    brackets: list[_Bracket] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            escaped = i + 1 < n and line[i + 1] in _ASCII_PUNCT
            _add_text(nodes, line[i + 1] if escaped else "\\")
            i += 2 if escaped else 1
        elif ch == "`":
            j = _run_end(line, i)
            close = _closing_backticks(line, j, j - i)
            if close is None:
                _add_text(nodes, line[i:j])
                i = j
            else:
                nodes.append(_Code(_code_span_body(line[j:close])))
                i = close + (j - i)
        elif ch in "*_~":
            j = _run_end(line, i)
            run = _Mark(line[i:j])
            nodes.append(run)
            if ch != "~" or j - i == 2:
                delim = _delimiter(run, line[i - 1] if i else " ", line[j] if j < n else " ")
                if delim is not None:
                    delims.append(delim)
            i = j
        elif ch == "[" or (ch == "!" and line.startswith("[", i + 1)):
            mark = _Mark(line[i:i + 2] if ch == "!" else "[")
            nodes.append(mark)
            brackets.append(_Bracket(mark, ch == "!", len(delims)))
            i += len(mark.s)
        elif ch == "]":
            i = _close_bracket(line, i, nodes, delims, brackets)
        else:
            m = _MARKUP_START.search(line, i + 1)
            j = m.start() if m else n
            _add_text(nodes, line[i:j])
            i = j
    _pair_emphasis(nodes, delims)
    return nodes


def _add_text(nodes: list, s: str) -> None:
    if nodes and type(nodes[-1]) is _Text:
        nodes[-1].s += s
    else:
        nodes.append(_Text(s))


def _run_end(line: str, i: int) -> int:
    """Where the run of the character at *i* ends."""
    j = i
    while j < len(line) and line[j] == line[i]:
        j += 1
    return j


def _closing_backticks(line: str, start: int, length: int) -> int | None:
    """Where the run of exactly *length* backticks that closes a code span starts, if any."""
    k = line.find("`" * length, start)
    while k != -1:
        end = _run_end(line, k)
        if end - k == length:
            return k
        k = line.find("`" * length, end)
    return None


def _code_span_body(body: str) -> str:
    """A code span's content: one space is dropped from each end when both ends have one and
    it is not all spaces, so ``` `` `x` `` ``` shows `` `x` ``."""
    if len(body) >= 2 and body[0] == " " and body[-1] == " " and body.strip(" "):
        return body[1:-1]
    return body


def _is_punct(ch: str) -> bool:
    return ch in _ASCII_PUNCT or unicodedata.category(ch)[0] in "PS"


def _delimiter(run: _Mark, before: str, after: str) -> _Delim | None:
    """*run* as a delimiter, by whether it can open and close emphasis, given the characters
    around it (CommonMark's flanking rules), or None when it can do neither."""
    left = not after.isspace() and (
        not _is_punct(after) or before.isspace() or _is_punct(before)
    )
    right = not before.isspace() and (
        not _is_punct(before) or after.isspace() or _is_punct(after)
    )
    if run.s[0] == "_":
        # Inside a word an underscore is a character: it opens only where nothing but
        # punctuation is before it, and closes only where nothing but punctuation follows.
        can_open = left and (not right or _is_punct(before))
        can_close = right and (not left or _is_punct(after))
    else:
        can_open, can_close = left, right
    return _Delim(run, can_open, can_close) if can_open or can_close else None


def _close_bracket(
    line: str, i: int, nodes: list, delims: list[_Delim], brackets: list[_Bracket]
) -> int:
    """Read the ``]`` at *i*: the label since the last ``[`` becomes a link when ``(url)``
    follows and is a web or mail address; otherwise the ``]`` is text. Returns where reading
    goes on."""
    bracket = brackets.pop() if brackets else None
    target = _link_target(line, i + 1) if bracket is not None and bracket.active else None
    if bracket is None or target is None or not _LINKABLE_RE.match(target[0]):
        _add_text(nodes, "]")
        return i + 1
    url, end = target
    label_delims = delims[bracket.bottom:]
    del delims[bracket.bottom:]
    _pair_emphasis(nodes, label_delims)
    start = _index(nodes, bracket.node)
    nodes[start:] = [_Link(nodes[start + 1:], url)]
    if not bracket.image:
        for earlier in brackets:  # a link holds no link
            if not earlier.image:
                earlier.active = False
    return end


def _link_target(line: str, p: int) -> tuple[str, int] | None:
    """``(url, end)`` for the ``(destination "title")`` that starts at *p*, or None.

    The destination is ``<...>`` or a run with no space whose parentheses balance; a backslash
    escapes punctuation in it. An empty one is no link: Telegram has nowhere to take it."""
    n = len(line)
    if p >= n or line[p] != "(":
        return None
    q = _skip_spaces(line, p + 1)
    url: list[str] = []
    if q < n and line[q] == "<":
        q += 1
        while q < n and line[q] not in "<>":
            q = _take(line, q, url)
        if q >= n or line[q] != ">":
            return None
        q += 1
    else:
        depth = 0
        while q < n and not line[q].isspace() and ord(line[q]) >= 0x20:
            if line[q] == "(":
                depth += 1
            elif line[q] == ")":
                if not depth:
                    break
                depth -= 1
            q = _take(line, q, url)
        if depth:
            return None
    after = _skip_spaces(line, q)
    if after > q and after < n and line[after] in "\"'(":
        closer = ")" if line[after] == "(" else line[after]
        q = after + 1
        while q < n and line[q] != closer:
            q += 2 if line[q] == "\\" else 1
        if q >= n:
            return None
        after = _skip_spaces(line, q + 1)
    if after >= n or line[after] != ")" or not url:
        return None
    return "".join(url), after + 1


def _skip_spaces(line: str, q: int) -> int:
    while q < len(line) and line[q] in " \t":
        q += 1
    return q


def _take(line: str, q: int, into: list[str]) -> int:
    """Append the character at *q*, or the punctuation a backslash there escapes."""
    if line[q] == "\\" and q + 1 < len(line) and line[q + 1] in _ASCII_PUNCT:
        into.append(line[q + 1])
        return q + 2
    into.append(line[q])
    return q + 1


def _index(nodes: list, node: object) -> int:
    return next(k for k, x in enumerate(nodes) if x is node)


def _pair_emphasis(nodes: list, delims: list[_Delim]) -> None:
    """Pair each closing delimiter run with the nearest opening one it can close, and wrap what
    lies between them in emphasis (CommonMark's "process emphasis"). Two characters from each
    side make bold, one makes italics, ``~~`` makes strikethrough. Whatever is left unpaired
    stays in the line as text."""
    c = 0
    while c < len(delims):
        closer = delims[c]
        if not closer.can_close:
            c += 1
            continue
        o = c - 1
        while o >= 0 and not _closes(delims[o], closer):
            o -= 1
        if o < 0:
            if closer.can_open:
                c += 1
            else:
                del delims[c]
            continue
        opener = delims[o]
        if closer.char == "~":
            used, marker = 2, _STRIKE
        else:
            used = 2 if opener.count >= 2 and closer.count >= 2 else 1
            marker = _BOLD if used == 2 else _ITALIC
        start, end = _index(nodes, opener.node), _index(nodes, closer.node)
        nodes[start + 1:end] = [_Span(marker, nodes[start + 1:end])]
        del delims[o + 1:c]
        c = o + 1
        opener.count -= used
        closer.count -= used
        opener.node.s = opener.node.s[:opener.count]
        closer.node.s = closer.node.s[:closer.count]
        if not opener.count:
            del delims[o]
            c -= 1
        if not closer.count:
            del delims[c]


def _closes(opener: _Delim, closer: _Delim) -> bool:
    """Whether *closer* can close *opener* (CommonMark's "rule of three" included: a run that
    can both open and close does not pair with one whose lengths would make it ambiguous)."""
    if opener.char != closer.char or not opener.can_open:
        return False
    if closer.char == "~":
        return True
    both_ways = opener.can_close or closer.can_open
    return not (
        both_ways
        and (opener.length + closer.length) % 3 == 0
        and not (opener.length % 3 == 0 and closer.length % 3 == 0)
    )


def _write(nodes: list, out: list[str], *, in_link: bool) -> None:
    """Write *nodes* as MarkdownV2, opening each emphasis only around text that has it."""
    styles: list[str] = []
    for marks, node in _leaves(nodes, ()):
        if isinstance(node, _Link) and not in_link:
            label: list[str] = []
            _write(node.children, label, in_link=True)
            _restyle(styles, marks, out)
            out.append(f"[{''.join(label) or escape_markdown_v2(node.url)}]"
                       f"({escape_link_url(node.url)})")
        elif isinstance(node, _Link):  # an image's description holding a link: its words
            _restyle(styles, marks, out)
            _write(node.children, out, in_link=True)
        elif isinstance(node, _Code) and not in_link:
            _restyle(styles, (), out)
            out.append(f"`{escape_code(node.s)}`")
        elif node.s:
            _restyle(styles, marks, out)
            out.append(escape_markdown_v2(node.s))
    _restyle(styles, (), out)


def _leaves(nodes: list, marks: tuple[str, ...]):
    """``(the markers over it, node)`` for each text, code span and link in *nodes*, in order.
    Emphasis already in force is not opened again inside itself."""
    for node in nodes:
        if isinstance(node, _Span):
            inner = marks if node.marker in marks else (*marks, node.marker)
            yield from _leaves(node.children, inner)
        else:
            yield marks, node


def _restyle(open_now: list[str], wanted: tuple[str, ...], out: list[str]) -> None:
    """Close the emphasis in force that *wanted* does not keep, innermost first, then open what
    it adds. What both share stays open, so no marker is closed and reopened back to back: two
    ``_`` in a row would read as underline."""
    keep = 0
    while keep < min(len(open_now), len(wanted)) and open_now[keep] == wanted[keep]:
        keep += 1
    out.extend(reversed(open_now[keep:]))
    out.extend(wanted[keep:])
    open_now[:] = wanted


# ── Splitting a reply into messages ──

#: The longest info string (a code block's language) carried onto a block reopened in the next
#: part. Anything longer, or with a space in it, is not a language tag, and the reopened block
#: goes without it.
_MAX_FENCE_INFO = 32


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
        state = before = fence
        while i < len(lines):
            after = _fence_after(state, lines[i])
            if not fits(_chunk(head + body + [lines[i]], after)):
                break
            body.append(lines[i])
            before, state = state, after
            i += 1
        #: Whether the part's last line is the one that opened the code block it ends in.
        opened = bool(body) and not before and bool(state)
        if i < len(lines) and len(body) > 1 and opened:
            # A part does not end on the line that opens a code block: the block would
            # arrive empty, and its code as the next part's.
            body.pop()
            i -= 1
            state = ""
        elif i < len(lines) and (not body or opened):
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
    return f"{text}\n{_ticks(fence)}" if fence else text


def _fence_after(fence: str, line: str) -> str:
    """The code block the text is in after *line*: "" outside one, else the line that opened it.
    It is the rule :func:`to_markdown_v2` reads blocks by, so a part is cut where its renderer
    sees the block, and closed the way it closes one."""
    if fence:
        close = _FENCE_CLOSE_RE.match(line)
        return "" if close and len(close.group(1)) >= len(_ticks(fence)) else fence
    return line.strip() if _FENCE_OPEN_RE.match(line) else ""


def _ticks(fence: str) -> str:
    """The run of backticks that opened the code block *fence*: what closes it."""
    return fence[: len(fence) - len(fence.lstrip("`"))]


def _reopening(fence: str) -> str:
    """The line that reopens the code block *fence* opened, in the next part."""
    ticks = _ticks(fence)
    info = fence[len(ticks):].strip()
    if info and len(info) <= _MAX_FENCE_INFO and " " not in info:
        return f"{ticks}{info}"
    return ticks


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
