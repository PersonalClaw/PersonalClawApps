"""The HTML part of an outgoing mail, written here from the mail's own text.

A mail client shows the HTML part of a ``multipart/alternative`` mail in place of its plain part,
so the HTML has to say what the plain part says and do nothing else. It is never markup a caller
hands over. A model can write markup, and markup in a mail can load an image, a style sheet or a
font from the network as the mail is opened, each telling its server that the mail was read, when
and from where; it can run script, carry a form, or show a link whose words are not where it goes.
So the HTML part is the text itself, every character of it escaped, with the formatting its
markdown asks for and nothing else:

* paragraphs and line breaks as the text has them, and its indentation;
* bold, italic, struck-through and code text, from ``**``/``__``, ``*``/``_``, ``~~`` and backticks;
* a fenced code block or a table as preformatted text, and a rule;
* a heading as a bold line;
* a link only to a web or mail address (``http``, ``https``, ``mailto``), and always showing where
  it goes: ``[the plan](https://example.com/q3)`` reads "the plan (https://example.com/q3)", with
  the address as the link. A link to anything else is the text that was written.

Nothing in it loads anything when the mail is opened. :func:`html_for` reads what was written back
with a parser before it is used (:func:`refusal`): an element other than those, an attribute other
than a link's address, or a link whose words are not its address, and the mail goes out as its
plain text alone. So does a text too long to format, or one the renderer fails on.
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: The elements the HTML part is made of. The one attribute any of them has is a link's address.
ELEMENTS = frozenset({"html", "body", "p", "br", "strong", "em", "del", "code", "pre", "hr", "a"})
#: The addresses a link may point at: the web and mail.
LINK_SCHEMES = frozenset({"http", "https", "mailto"})
#: A text longer than this goes out as plain text alone, so formatting it never holds up a send.
MAX_FORMATTED_CHARS = 100_000

# ── blocks ──

#: A fence opening a code block: three or more backticks or tildes, indented at most three spaces.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
#: A heading: one to six ``#`` and a space.
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
#: A rule: three or more of one of ``-``, ``*``, ``_``, spaces between them allowed.
_RULE_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
#: A table's row: a line that starts and ends with ``|``.
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")

# ── within a line ──

#: What a line is read as, left to right: a code span (a run of backticks closed by a run of
#: exactly as many), shown as written; a markdown link ``[words](address)``, whose address may hold
#: one level of brackets; an address in angle brackets; or a bare web address. Whichever starts
#: first wins, so an address written in code stays code.
_TOKEN_RE = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`)(?P<body>.+?)(?<!`)(?P=ticks)(?!`))"
    r"|\[(?P<label>[^\[\]]+)\]\((?P<url>(?:[^()\s]|\([^()\s]*\))+)\)"
    r"|<(?P<angle>(?:https?|mailto):[^\s<>]+)>"
    r"|(?P<bare>(?<![\w/])https?://[^\s<>]+)",
    re.IGNORECASE,
)
#: What ends a sentence or closes emphasis right after a bare address, and is not part of it.
_URL_TRAILERS = ".,:;!?'\"*_~"
#: Where a piece already written as HTML (a code span, a link) sits in a line while its emphasis is
#: read. The line is stripped of NUL first, so no text can spell one.
_SLOT_RE = re.compile("\x00([0-9]+)\x00")

#: Emphasis, read off a line whose text is already escaped and whose code and links are slots, so
#: what each one wraps is text or a slot and every tag in the result is one written here. A marker
#: never opens before a space or closes after one, and never sits inside a word, so ``2 * 3`` and
#: ``snake_case`` keep their characters. Italic is read before bold and again after it, so either
#: can hold the other. No marker's content holds its own marker, which keeps each pass linear.
_EMPHASIS = tuple(
    (re.compile(pattern), tag)
    for pattern, tag in (
        (r"(?<![*\w])\*\*\*(?=[^\s*])([^*]+?)(?<=[^\s*])\*\*\*(?![*\w])", "strong><em"),
        (r"(?<![*\w])\*(?=[^\s*])([^*]+?)(?<=[^\s*])\*(?![*\w])", "em"),
        (r"(?<![_\w])_(?=[^\s_])([^_]+?)(?<=[^\s_])_(?![_\w])", "em"),
        (r"(?<![*\w])\*\*(?=[^\s*])([^*]+?)(?<=[^\s*])\*\*(?![*\w])", "strong"),
        (r"(?<![_\w])__(?=[^\s_])([^_]+?)(?<=[^\s_])__(?![_\w])", "strong"),
        (r"(?<![*\w])\*(?=[^\s*])([^*]+?)(?<=[^\s*])\*(?![*\w])", "em"),
        (r"(?<![_\w])_(?=[^\s_])([^_]+?)(?<=[^\s_])_(?![_\w])", "em"),
        (r"(?<!~)~~(?=[^\s~])([^~]+?)(?<=[^\s~])~~(?!~)", "del"),
    )
)


def html_for(text: str) -> str:
    """The HTML part for a mail whose plain part is *text*, or ``""`` when the mail goes out as its
    plain text alone: an empty text, one past :data:`MAX_FORMATTED_CHARS`, one the renderer fails
    on, or HTML that :func:`refusal` refuses."""
    if not text.strip() or len(text) > MAX_FORMATTED_CHARS:
        return ""
    try:
        markup = render(text)
        problem = refusal(markup)
    except Exception:  # noqa: BLE001 - the plain part is still the whole message
        logger.warning(
            "email: the HTML part could not be written; sent as plain text", exc_info=True
        )
        return ""
    if problem:
        logger.warning("email: the HTML part held %s; sent as plain text", problem)
        return ""
    return markup


def render(text: str) -> str:
    """*text*, a mail's markdown, as an HTML document of :data:`ELEMENTS` (see the module)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").split("\n")
    blocks: list[str] = []
    paragraph: list[str] = []

    def end_paragraph() -> None:
        if paragraph:
            blocks.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            end_paragraph()
            mark = fence.group(1)
            closing = re.compile(rf"^ {{0,3}}{re.escape(mark[0])}{{{len(mark)},}}[ \t]*$")
            code: list[str] = []
            i += 1
            while i < len(lines) and not closing.match(lines[i]):
                code.append(lines[i])
                i += 1
            i += 1  # the closing fence; an unclosed block runs to the end of the text
            blocks.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            continue
        if not line.strip():
            end_paragraph()
        elif _TABLE_ROW_RE.match(line):
            end_paragraph()
            rows: list[str] = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                rows.append(lines[i].strip())
                i += 1
            blocks.append("<pre>" + html.escape("\n".join(rows)) + "</pre>")
            continue
        elif _RULE_RE.match(line):
            end_paragraph()
            blocks.append("<hr>")
        elif heading := _HEADING_RE.match(line):
            end_paragraph()
            blocks.append("<p><strong>" + _inline(heading.group(1)) + "</strong></p>")
        else:
            paragraph.append(_line(line))
        i += 1
    end_paragraph()
    return "<html><body>" + "".join(blocks) + "</body></html>"


def _line(line: str) -> str:
    """One line of a paragraph: its indentation as spaces a mail client keeps, then its text."""
    body = line.lstrip(" \t")
    indent = line[: len(line) - len(body)].replace("\t", "    ")
    return "&nbsp;" * len(indent) + _inline(body.rstrip())


def _inline(text: str, *, links: bool = True) -> str:
    """One line of markdown outside code as HTML: every character escaped, its code spans and links
    written as such (with *links* off every address is text: a link's own words), then its
    emphasis."""
    pieces: list[str] = []
    out: list[str] = []
    pos = 0

    def slot(written: str) -> None:
        out.append(f"\x00{len(pieces)}\x00")
        pieces.append(written)

    for m in _TOKEN_RE.finditer(text):
        if m.group("code") is None and not links:
            continue
        out.append(html.escape(text[pos : m.start()]))
        pos = m.end()
        if m.group("code") is not None:
            slot("<code>" + html.escape(m.group("body")) + "</code>")
        elif m.group("url") is not None:
            slot(_link(m.group("url"), m.group("label"), m.group(0)))
        elif m.group("angle") is not None:
            slot(_link(m.group("angle"), "", m.group(0)))
        else:
            address, trail = _trim_address(m.group("bare"))
            if is_link_address(address):
                slot(_anchor(address))
                out.append(html.escape(trail))
            else:
                out.append(html.escape(m.group("bare")))
    out.append(html.escape(text[pos:]))
    marked = "".join(out)
    for pattern, tag in _EMPHASIS:
        closing = "></".join(reversed(tag.split("><")))
        marked = pattern.sub(rf"<{tag}>\1</{closing}>", marked)
    return _SLOT_RE.sub(lambda m: pieces[int(m.group(1))], marked)


def _link(address: str, words: str, written: str) -> str:
    """A link to *address* with *words*: the words, then the address in brackets as the link; the
    address alone when there are no other words. *written*, as text, when the address is not a web
    or mail address."""
    address = address.strip()
    if not is_link_address(address):
        return html.escape(written)
    if not words.strip() or words.strip() == address:
        return _anchor(address)
    return f"{_inline(words.strip(), links=False)} ({_anchor(address)})"


def _anchor(address: str) -> str:
    """A link whose words are its address."""
    shown = html.escape(address)
    return f'<a href="{shown}">{shown}</a>'


def _trim_address(url: str) -> tuple[str, str]:
    """``(address, rest)``: a bare address without the punctuation after it, and a closing bracket
    only when the address opened one."""
    end = len(url)
    while end > 0:
        ch = url[end - 1]
        if ch in _URL_TRAILERS or (ch == ")" and url[:end].count(")") > url[:end].count("(")):
            end -= 1
            continue
        break
    return url[:end], url[end:]


def is_link_address(address: str) -> bool:
    """Whether a link may point at *address*: a web address with a host (``http``, ``https``) or a
    mail address (``mailto``), holding no space, backslash, or control or format character (a
    format character, such as one that reverses the text after it, can make an address read as
    another)."""
    if not address or any(
        c.isspace() or c == "\\" or unicodedata.category(c) in ("Cc", "Cf") for c in address
    ):
        return False
    try:
        parts = urlsplit(address)
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme not in LINK_SCHEMES:
        return False
    return bool(parts.path) if scheme == "mailto" else bool(parts.netloc)


class _Check(HTMLParser):
    """Reads an HTML part back and keeps the first thing in it that is not formatting."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problem = ""
        self._href: str | None = None
        self._words: list[str] = []

    def _refuse(self, problem: str) -> None:
        self.problem = self.problem or problem

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ELEMENTS:
            self._refuse(f"a <{tag}> element")
        elif tag != "a":
            if attrs:
                self._refuse(f"an attribute on <{tag}>")
        elif self._href is not None:
            self._refuse("a link inside a link")
        elif [name for name, _ in attrs] != ["href"]:
            self._refuse("a link with more than its address")
        elif not is_link_address(attrs[0][1] or ""):
            self._refuse("a link to something that is not a web or mail address")
        else:
            self._href, self._words = attrs[0][1] or "", []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._refuse("a link with no words")
        else:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag not in ELEMENTS:
            self._refuse(f"a </{tag}> element")
        elif tag == "a":
            if self._href is None:
                self._refuse("a link closed that was never opened")
            elif "".join(self._words) != self._href:
                self._refuse("a link whose words are not where it goes")
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._words.append(data)

    def handle_comment(self, data: str) -> None:
        self._refuse("a comment")

    def handle_decl(self, decl: str) -> None:
        self._refuse("a declaration")

    def unknown_decl(self, data: str) -> None:
        self._refuse("a declaration")

    def handle_pi(self, data: str) -> None:
        self._refuse("a processing instruction")


def refusal(markup: str) -> str:
    """What in *markup* is not an HTML part's formatting, in words, or ``""`` when it is all
    formatting: :data:`ELEMENTS` alone, no attribute but a link's address, a link only to a web or
    mail address and showing it as its words."""
    check = _Check()
    check.feed(markup)
    check.close()
    if check._href is not None:
        check._refuse("a link left open")
    return check.problem
