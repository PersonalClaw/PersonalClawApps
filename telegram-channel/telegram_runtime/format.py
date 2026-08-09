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
"""

from __future__ import annotations

import re

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
    this function's concern — :func:`split_message` chunks to
    :data:`TELEGRAM_MAX_TEXT` at the delivery layer."""
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


def split_message(text: str, limit: int = TELEGRAM_MAX_TEXT) -> list[str]:
    """Split *text* into parts no longer than *limit*, preferring newline breaks.

    Telegram rejects a text longer than 4096 chars, so long replies stream across
    several messages. Splits on the last newline before the limit when possible,
    else hard-splits."""
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts
