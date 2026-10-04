"""What a pending approval shows before Approve: the dashboard's brief, in a menu's lines.

The dashboard's approval card for a ``GET /api/approvals`` row shows, above its Allow and Deny:
the tool and its arguments, the purpose the runner gave, where the call came from ("From ..."),
what it can touch and its risk, the line for a call that reaches a host off the allowed hosts,
and what a Deny does when it does more than decline the call. :func:`brief_of` composes the same
parts from the same fields of the row, in the words core uses for a surface that has only text
(its channel brief): the risk words of the card's chip and the facet words of its chips, joined
the way a channel's summary line joins them.

A menu holds less than a card, so every part is shown WHOLE or not at all:

* a part longer than a menu can hold is replaced by a line that says so, and the row cannot be
  approved from the menu: it offers "Review in PersonalClaw" in place of Approve;
* a row the menu cannot read (no id or no tool, a field of the wrong kind, a risk or a facet
  this app has no words for) shows one line saying so and offers only the review: nothing it
  would say about the call could be trusted, so neither answer is offered.

Deny stays on offer under a brief that is too long when what Deny does is shown whole: declining
the call is what Deny means, and that line says when it does more.

Text a call carries is shown as text. A character that prints nothing, or that moves the text
around it, is shown as its code point instead (:func:`visible`), so what the lines say is what
will run.
"""

from __future__ import annotations

import json
import textwrap
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass

#: The widest line the brief shows, in characters. A menu grows to its widest item; past this a
#: line continues on the next one.
WIDTH = 64

#: The most lines the call's arguments may take. A short command or script fits; anything longer
#: is reviewed in the dashboard, which shows all of it.
ARGUMENT_LINES = 12

#: The most lines any other part may take. Core's own sentences (where a call reaches, what a
#: Deny does) take four at most.
PART_LINES = 6

#: How the first line of each line of the arguments starts, and how a line that continues the
#: one above it starts, so a line break in what will run reads differently from a wrapped line.
INDENT = "  "
CONTINUED = "↪ "

#: The dashboard card's risk chip, by the risk level a row carries (core's ``RISK_LABELS``).
RISK_WORDS = {
    "safe": "Safe",
    "caution": "Caution",
    "destructive": "Destructive",
    "unchecked": "Not checked",
}

#: The dashboard card's blast-radius chips, in their order: broadest consequence first, the
#: claims that a call only reads last (core's ``FACET_COPY`` and ``BLAST_RADIUS_FACET_ORDER``).
FACET_WORDS = (
    ("writes", "Writes files"),
    ("shell", "Runs a command"),
    ("network", "Uses the network"),
    ("saysReadOnly", "Server says it only reads"),
    ("readOnly", "Reads only"),
)

#: The facets that claim what a call does NOT do, so never phrased as something it "can" do.
READ_CLAIMS = frozenset({"saysReadOnly", "readOnly"})

#: The one line a row the menu cannot read shows.
UNREADABLE = "This menu can't read this approval's details."

#: Unicode categories that print nothing a reader can trust: controls, format characters (the
#: ones that reorder text or join and hide it), surrogates, private use, unassigned, and the
#: line and paragraph separators.
_SHOWN_AS_CODE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


@dataclass(frozen=True)
class Brief:
    """One pending approval as the menu shows it: its row, its lines, and what may answer it."""

    #: The approval's row in the menu: the tool, and where the call came from.
    title: str
    #: Everything shown above the answers, in order.
    lines: tuple[str, ...]
    #: Every part of the brief was read and is shown whole, so Approve may be offered.
    approvable: bool
    #: What Deny does was read and is shown whole, so Deny may be offered.
    deniable: bool


class _Unreadable(ValueError):
    """A field of the row is not the kind the brief reads."""


@dataclass(frozen=True)
class _Part:
    #: How the line that stands in for it, when it is too long, names it.
    what: str
    #: Its lines, or ``None`` when it is longer than its lines could ever hold.
    lines: tuple[str, ...] | None
    limit: int

    @property
    def whole(self) -> bool:
        return self.lines is not None and len(self.lines) <= self.limit


def _part(what: str, text: str, limit: int, lay_out: Callable[[str], tuple[str, ...]]) -> _Part:
    """One part of the brief: *text* laid out, unless no layout could fit it in *limit* lines.

    A line holds at most :data:`WIDTH` characters and one break, so a text longer than that many
    is too long however it breaks: it is not laid out at all. The brief is composed on every read
    of the menu, and a call's arguments can be a whole file.
    """
    if len(text) > limit * (WIDTH + 1):
        return _Part(what, None, limit)
    return _Part(what, lay_out(text), limit)


def brief_of(row: Mapping[str, object]) -> Brief:
    """The brief for one ``GET /api/approvals`` row."""
    title = _title(row)
    try:
        parts = _parts(row)
    except _Unreadable:
        return Brief(title=title, lines=(UNREADABLE,), approvable=False, deniable=False)
    lines: list[str] = []
    for part in parts:
        if part.whole and part.lines is not None:
            lines.extend(part.lines)
        else:
            lines.append(f"{part.what} is too long to show here.")
    return Brief(
        title=title,
        lines=tuple(lines),
        approvable=all(part.whole for part in parts),
        deniable=parts[-1].whole,
    )


def visible(text: str) -> str:
    """*text* with every character that prints nothing shown as its code point.

    Line breaks and tabs are kept: the callers lay them out. Everything else in
    :data:`_SHOWN_AS_CODE` becomes ``\\u`` and four hex digits (``\\U`` and eight past the
    basic plane), which is how the character is written in the source of nearly every language.
    """
    out = []
    for ch in text:
        if ch in "\n\t" or unicodedata.category(ch) not in _SHOWN_AS_CODE:
            out.append(ch)
        elif ord(ch) > 0xFFFF:
            out.append(f"\\U{ord(ch):08x}")
        else:
            out.append(f"\\u{ord(ch):04x}")
    return "".join(out)


def prose(text: str) -> tuple[str, ...]:
    """*text* as menu lines: each paragraph wrapped at :data:`WIDTH`."""
    lines: list[str] = []
    for paragraph in visible(text).split("\n"):
        lines.extend(textwrap.wrap(paragraph, WIDTH, break_on_hyphens=False))
    return tuple(lines)


def excerpt(text: str, limit: int) -> tuple[str, ...]:
    """The first *limit* lines of :func:`prose`, the last ending in an ellipsis when it is cut.

    For text the menu only points at (a loop's question): the whole of it is a click away. Only
    the head of the text that could fill the lines is laid out.
    """
    head = text[: (limit + 1) * (WIDTH + 1)]
    lines = prose(head)
    if len(lines) <= limit and head == text:
        return lines
    kept = list(lines[:limit])
    if kept:
        kept[-1] = f"{kept[-1]} …"
    return tuple(kept)


def verbatim(text: str) -> tuple[str, ...]:
    """The call's arguments as menu lines, every character kept.

    Each line of *text* starts with :data:`INDENT`; a line too wide for the menu goes on in the
    lines below it, each starting with :data:`CONTINUED`. Joining a line's pieces gives it back
    exactly (tabs as spaces): nothing is trimmed, collapsed or dropped.
    """
    if not text:
        return ()
    lines: list[str] = []
    for line in visible(text).split("\n"):
        pieces = textwrap.wrap(
            line,
            WIDTH - len(INDENT),
            tabsize=4,
            replace_whitespace=False,
            drop_whitespace=False,
            break_on_hyphens=False,
        ) or [""]
        lines.append(INDENT + pieces[0])
        lines.extend(CONTINUED + piece for piece in pieces[1:])
    return tuple(lines)


def _parts(row: Mapping[str, object]) -> list[_Part]:
    """The brief's parts in the card's order. The last is what Deny does."""
    approval_id = _text(row, "id")
    tool = _text(row, "tool")
    if not approval_id.strip() or not tool.strip():
        raise _Unreadable("an approval with no id or no tool")
    source = _text(row, "source_label")
    return [
        _part("The tool's name", f"Permission needed to run {tool}", PART_LINES, prose),
        _part("What it would run", _arguments(row), ARGUMENT_LINES, verbatim),
        _part("Its purpose", _text(row, "tool_purpose"), PART_LINES, prose),
        _part("Where it came from", f"From {source}" if source.strip() else "", PART_LINES, prose),
        _part("What it can touch", _summary(row), PART_LINES, prose),
        _part("Where it reaches", _text(row, "reach"), PART_LINES, prose),
        _part("Who asked", _text(row, "asked_for"), PART_LINES, prose),
        _part("What Deny does", _text(row, "deny_effect"), PART_LINES, prose),
    ]


def _text(row: Mapping[str, object], key: str) -> str:
    """A text field: ``""`` when the row leaves it out, as the dashboard reads it."""
    value = row.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise _Unreadable(key)


def _arguments(row: Mapping[str, object]) -> str:
    """The call's input as text: core sends a string; a call's dict or list reads as its JSON."""
    value = row.get("tool_input")
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    raise _Unreadable("tool_input")


def _summary(row: Mapping[str, object]) -> str:
    """What the call can touch and its risk, in one line (core's channel ``summary_line``).

    ``"Can: writes files, runs a command · Risk: Caution"``; a read claim is its own phrase
    (``"Reads only · Risk: Safe"``); ``""`` when the row establishes neither. A facet is shown
    only when the row establishes it: absent evidence is never said as a reassurance.
    """
    risk = _text(row, "risk")
    if risk and risk not in RISK_WORDS:
        raise _Unreadable("risk")
    facets = _facets(row.get("blast_radius"))
    established = [key for key, _words in FACET_WORDS if key in facets]
    words = dict(FACET_WORDS)
    consequences = [words[key].lower() for key in established if key not in READ_CLAIMS]
    parts = [f"Can: {', '.join(consequences)}"] if consequences else []
    parts.extend(words[key] for key in established if key in READ_CLAIMS)
    if risk:
        parts.append(f"Risk: {RISK_WORDS[risk]}")
    return " · ".join(parts)


def _facets(radius: object) -> frozenset[str]:
    """The facets *radius* establishes. ``None`` establishes none.

    Anything else must name every facet this app knows, each true or false: a radius missing one,
    holding something other than true or false, or establishing a facet this app has no words for
    cannot be shown whole.
    """
    if radius is None:
        return frozenset()
    known = {key for key, _words in FACET_WORDS}
    if (
        not isinstance(radius, dict)
        or not known <= radius.keys()
        or not all(isinstance(value, bool) for value in radius.values())
        or any(value and key not in known for key, value in radius.items())
    ):
        raise _Unreadable("blast_radius")
    return frozenset(key for key in known if radius[key])


def _title(row: Mapping[str, object]) -> str:
    """The approval's row in the menu: the tool and where it came from, cut at a word to fit.

    Only a label: what the call does is in the brief under it, whole.
    """
    tool = row.get("tool")
    tool = tool if isinstance(tool, str) and tool.strip() else "An approval"
    source = row.get("source_label")
    text = f"{tool} — from {source}" if isinstance(source, str) and source.strip() else tool
    # Only the head that could fill the row is read: the rest is cut anyway.
    flat = " ".join(visible(text[: 4 * WIDTH]).split())
    if len(flat) <= WIDTH:
        return flat
    cut = flat[: WIDTH - 2]
    # At the last word that fits, unless that would leave less than half the line.
    space = cut.rfind(" ")
    return f"{cut[:space] if space > WIDTH // 2 else cut} …"
