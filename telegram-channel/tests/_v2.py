"""A MarkdownV2 parser that refuses what the Bot API refuses — the judge the split tests use.

The app's own renderer cannot be its own judge: a test that checked a part by asking
``to_markdown_v2`` whether it looks right would pass on exactly the output it produced. This is
an independent reading of the Bot API's "Formatting options", written from Telegram's rules,
not from the app's code:

* ``_ * [ ] ( ) ~ ` > # + - = | { } . !`` are reserved and must be escaped with ``\\`` anywhere
  they are not markup; any character with code 1-126 may be escaped.
* Entities: ``*bold*``, ``_italic_``, ``__underline__``, ``~strike~``, ``||spoiler||``,
  ``[text](url)``, ``![emoji](tg://emoji?id=…)``, `` `code` ``, fenced ````` ```pre``` ````` with an
  optional language on its first line, and ``>`` at the start of a line (a quote). Inside
  code/pre only ``\\`` and `` ` `` are escaped; inside a link URL only ``\\`` and ``)``.
* An entity left open is an error.
* The 4096-character limit applies to the text AFTER parsing, counted in UTF-16 code units.

Stdlib only (the apps boundary lint checks every non-``test_*`` file here), and never collected
by pytest (no ``test_`` prefix).
"""

from __future__ import annotations

RESERVED_V2 = set("_*[]()~`>#+-=|{}.!")
MAX_TEXT = 4096


class ParseError(ValueError):
    """The Bot API's ``can't parse entities`` refusal; ``str()`` is Telegram's description."""


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _byte_offset(text: str, index: int) -> int:
    return len(text[:index].encode("utf-8"))


def _reserved(ch: str) -> ParseError:
    return ParseError(
        f"can't parse entities: Character '{ch}' is reserved and must be escaped with the "
        "preceding '\\'"
    )


def _unclosed(text: str, index: int) -> ParseError:
    return ParseError(
        "can't parse entities: Can't find end of the entity starting at byte offset "
        f"{_byte_offset(text, index)}"
    )


def parse_markdown_v2(text: str) -> tuple[str, list[dict]]:
    """``(plain_text, entities)`` for a MarkdownV2 ``text``, or :class:`ParseError`.

    Offsets and lengths are UTF-16 code units, as the Bot API reports them."""
    out: list[str] = []
    entities: list[dict] = []
    stack: list[tuple[str, int, int]] = []  # (type, plain offset, source index)
    i, n = 0, len(text)

    def plain_off() -> int:
        return utf16_len("".join(out))

    def toggle(kind: str, width: int) -> None:
        nonlocal i
        if stack and stack[-1][0] == kind:
            _, start, _src = stack.pop()
            length = plain_off() - start
            if length:
                entities.append({"type": kind, "offset": start, "length": length})
        else:
            stack.append((kind, plain_off(), i))
        i += width

    while i < n:
        ch = text[i]
        if ch == "\\":
            if i + 1 < n and 1 <= ord(text[i + 1]) <= 126:
                out.append(text[i + 1])
                i += 2
            else:
                out.append(ch)
                i += 1
            continue
        if ch == "`":
            fence = text.startswith("```", i)
            start_src = i
            i += 3 if fence else 1
            buf: list[str] = []
            language = ""
            if fence:
                nl = text.find("\n", i)
                first = text[i:nl] if nl != -1 else ""
                if nl != -1 and first and " " not in first and "`" not in first:
                    language = first
                    i = nl + 1
                elif nl == i:
                    i += 1
            closed = False
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n and text[i + 1] in "\\`":
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if fence and text.startswith("```", i):
                    i += 3
                    closed = True
                    break
                if not fence and c == "`":
                    i += 1
                    closed = True
                    break
                buf.append(c)
                i += 1
            if not closed:
                raise _unclosed(text, start_src)
            start = plain_off()
            body = "".join(buf)
            if fence and body.endswith("\n"):
                body = body[:-1]
            out.append(body)
            if body:
                ent = {"type": "pre" if fence else "code", "offset": start, "length": utf16_len(body)}
                if language:
                    ent["language"] = language
                entities.append(ent)
            continue
        if ch == "*":
            if text.startswith("**>", i) and (i == 0 or text[i - 1] == "\n"):
                i += 3  # expandable blockquote marker
                continue
            toggle("bold", 1)
            continue
        if ch == "_":
            if i + 1 < n and text[i + 1] == "_":
                toggle("underline", 2)
            else:
                toggle("italic", 1)
            continue
        if ch == "~":
            toggle("strikethrough", 1)
            continue
        if ch == "|":
            if i + 1 < n and text[i + 1] == "|":
                toggle("spoiler", 2)
                continue
            raise _reserved(ch)
        if ch == "!" and i + 1 < n and text[i + 1] == "[":
            stack.append(("custom_emoji", plain_off(), i))
            i += 2
            continue
        if ch == "[":
            stack.append(("text_link", plain_off(), i))
            i += 1
            continue
        if ch == "]":
            if not stack or stack[-1][0] not in ("text_link", "custom_emoji"):
                raise _reserved(ch)
            kind, start, src = stack.pop()
            i += 1
            url = ""
            if i < n and text[i] == "(":
                j = i + 1
                ubuf: list[str] = []
                closed = False
                while j < n:
                    c = text[j]
                    if c == "\\" and j + 1 < n and text[j + 1] in "\\)":
                        ubuf.append(text[j + 1])
                        j += 2
                        continue
                    if c == ")":
                        closed = True
                        break
                    ubuf.append(c)
                    j += 1
                if not closed:
                    raise _unclosed(text, src)
                url = "".join(ubuf)
                i = j + 1
            length = plain_off() - start
            if length and kind == "text_link":
                entities.append({"type": "text_link", "offset": start, "length": length, "url": url})
            elif length:
                entities.append({"type": "custom_emoji", "offset": start, "length": length})
            continue
        if ch == ">" and (i == 0 or text[i - 1] == "\n"):
            i += 1  # blockquote marker
            continue
        if ch in RESERVED_V2:
            raise _reserved(ch)
        out.append(ch)
        i += 1
    if stack:
        raise _unclosed(text, stack[-1][2])
    return "".join(out), entities


def in_entity(plain: str, entities: list[dict], needle: str, kind: str) -> bool:
    """Whether every occurrence of *needle* in *plain* lies inside an entity of *kind*."""
    spans = [
        (e["offset"], e["offset"] + e["length"]) for e in entities if e["type"] == kind
    ]
    start = plain.find(needle)
    if start == -1:
        return False
    while start != -1:
        lo = utf16_len(plain[:start])
        hi = lo + utf16_len(needle)
        if not any(a <= lo and hi <= b for a, b in spans):
            return False
        start = plain.find(needle, start + 1)
    return True
