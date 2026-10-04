"""The brief: every part whole or said to be too long, in the dashboard's own words.

The words are checked against core's own tables and composer (``personalclaw.approval_brief``,
what the dashboard card's words are pinned to), the way ``test_manifest`` checks the manifest
against core's own parser. This app cannot import core at run time: it runs on a Mac whose only
PersonalClaw may be the gateway it points at.
"""

from __future__ import annotations

import itertools
import random
import string

import pytest
from _rows import ASKED_FOR, DENY_ENDS_THE_TURN, approval, radius
from menubar_companion import brief as brief_mod
from menubar_companion.brief import (
    ARGUMENT_LINES,
    CONTINUED,
    FACET_WORDS,
    INDENT,
    RISK_WORDS,
    UNREADABLE,
    WIDTH,
    brief_of,
    excerpt,
    verbatim,
    visible,
)


def _summary_of(row: dict) -> str:
    """The summary line a row with nothing but a tool, a radius and a risk shows."""
    lines = brief_of({"id": "a1", "tool": "bash", **row}).lines
    return " ".join(lines[1:])


# ── the words are core's ──


def test_the_risk_and_facet_words_are_cores():
    core = pytest.importorskip(
        "personalclaw.approval_brief",
        reason="core is installed in CI's tests job; skip where it is absent",
    )
    assert RISK_WORDS == core.RISK_LABELS
    assert FACET_WORDS == tuple(
        (key, core.FACET_COPY[key]["label"]) for key in core.BLAST_RADIUS_FACET_ORDER
    )


def test_the_summary_line_is_cores_for_every_radius_and_risk():
    """Every combination of the five facets with every risk, and no radius at all."""
    core = pytest.importorskip(
        "personalclaw.approval_brief",
        reason="core is installed in CI's tests job; skip where it is absent",
    )
    keys = [key for key, _words in FACET_WORDS]
    combinations = itertools.product((False, True), repeat=len(keys))
    radii = [None] + [dict(zip(keys, bits, strict=True)) for bits in combinations]
    checked = 0
    for blast_radius, risk in itertools.product(radii, ["", *RISK_WORDS]):
        ours = _summary_of({"blast_radius": blast_radius, "risk": risk})
        assert ours == core.summary_line(blast_radius, risk), (blast_radius, risk)
        checked += 1
    assert checked == 33 * 5


def test_vacuity_floor_a_word_out_of_step_with_core_is_seen(monkeypatch):
    core = pytest.importorskip(
        "personalclaw.approval_brief",
        reason="core is installed in CI's tests job; skip where it is absent",
    )
    monkeypatch.setattr(brief_mod, "RISK_WORDS", {**RISK_WORDS, "caution": "Careful"})
    row = {"blast_radius": radius(writes=True), "risk": "caution"}
    assert _summary_of(row) != core.summary_line(row["blast_radius"], "caution")


# ── whole or not at all ──


def test_every_part_that_fits_is_shown_and_answers_are_offered():
    shown = brief_of(approval(deny_effect=DENY_ENDS_THE_TURN))
    assert shown.approvable and shown.deniable
    assert shown.title == "bash — from chat “Release prep”"
    assert shown.lines[0] == "Permission needed to run bash"
    assert all(len(line) <= WIDTH for line in shown.lines)


def test_a_call_someone_else_asked_for_names_them_before_the_answers():
    """Core names who asked for a call's turn when it was not the owner; the menu says it, as the
    dashboard's card does, and says nothing of the kind for her own call."""
    theirs = brief_of(approval(asked_for=ASKED_FOR))
    hers = brief_of(approval())
    assert " ".join(theirs.lines).count("Jonas (U0JONASCOL) on teamchat asked for this") == 1
    assert theirs.approvable and theirs.deniable
    assert not any("asked for this" in line for line in hers.lines)


@pytest.mark.parametrize(
    ("field", "value", "said"),
    [
        ("tool_input", "\n".join(["echo one"] * (ARGUMENT_LINES + 1)), "What it would run"),
        ("tool_purpose", "Why it asks. " * 60, "Its purpose"),
        ("source_label", "chat “" + "long name " * 60 + "”", "Where it came from"),
        ("reach", "It reaches a host. " * 40, "Where it reaches"),
        ("asked_for", "Someone else asked for this. " * 40, "Who asked"),
    ],
)
def test_a_part_too_long_is_said_to_be_and_approve_goes(field, value, said):
    shown = brief_of(approval(**{field: value}))
    assert f"{said} is too long to show here." in shown.lines
    assert not shown.approvable
    assert shown.deniable, "what Deny does is still shown whole"


def test_arguments_longer_than_the_menu_could_hold_are_not_laid_out(monkeypatch):
    """A call that writes a whole file is judged too long by its length, not by wrapping it.

    The brief is composed on every read of the menu; laying out a large file each time is work
    whose answer is already known.
    """

    def must_not_run(_text):
        raise AssertionError("a part that cannot fit was laid out")

    monkeypatch.setattr(brief_mod, "verbatim", must_not_run)
    content = "lorem ipsum dolor sit amet\n" * 20_000
    shown = brief_of(approval(tool="write_file", tool_input=content))
    assert "What it would run is too long to show here." in shown.lines
    assert not shown.approvable and shown.deniable


def test_the_arguments_at_their_limit_still_fit():
    """The boundary: exactly :data:`ARGUMENT_LINES` lines is whole, one more is not."""
    at_limit = brief_of(approval(tool_input="\n".join(["echo one"] * ARGUMENT_LINES)))
    assert at_limit.approvable
    assert at_limit.lines.count("  echo one") == ARGUMENT_LINES


def test_a_row_that_cannot_be_read_says_so_and_offers_nothing():
    shown = brief_of(approval(blast_radius={"writes": "yes"}))
    assert shown.lines == (UNREADABLE,)
    assert not shown.approvable and not shown.deniable
    assert shown.title == "bash — from chat “Release prep”", "its row still says which call"
    nameless = brief_of({"id": "a1", "tool": 3})
    assert nameless.title == "An approval" and nameless.lines == (UNREADABLE,)


def test_a_call_input_that_is_a_dict_reads_as_its_json():
    """Core sends a string; a call's dict, the way the dashboard shows one, is its JSON."""
    shown = brief_of(approval(tool_input={"command": "ls build"}))
    assert '  {"command": "ls build"}' in shown.lines
    assert shown.approvable


# ── shown as text ──


def test_characters_that_print_nothing_are_shown_as_their_code_point():
    assert visible("build\u200b/cache") == "build\\u200b/cache"
    assert visible("one\u2028two") == "one\\u2028two"
    assert visible("bell\x07") == "bell\\u0007"
    assert visible("tag\U000e0041") == "tag\\U000e0041"


def test_ordinary_text_is_shown_as_written():
    ordinary = "naïve “quoted” 日本語 ✓ — tab\tand newline\n"
    assert visible(ordinary) == ordinary


def test_the_arguments_come_back_exactly_from_their_lines():
    """For any line, joining its pieces gives it back: nothing trimmed, collapsed or dropped."""
    rng = random.Random(7)
    alphabet = string.ascii_letters + string.digits + "  \t-_/.\"'{}:,;"
    for _ in range(500):
        line = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 300)))
        pieces = verbatim(line)
        assert pieces[0].startswith(INDENT)
        assert all(piece.startswith(CONTINUED) for piece in pieces[1:])
        rebuilt = pieces[0][len(INDENT) :] + "".join(p[len(CONTINUED) :] for p in pieces[1:])
        assert rebuilt == line.expandtabs(4)
        assert all(len(piece) <= WIDTH for piece in pieces)


def test_an_excerpt_says_when_it_is_cut():
    long = "word " * 60
    lines = excerpt(long, 2)
    assert len(lines) == 2 and lines[-1].endswith(" …")
    assert excerpt("a short question", 2) == ("a short question",)


def test_a_title_too_long_for_its_row_is_cut_at_a_word():
    title = brief_of(approval(source_label="loop “" + "Fix the README " * 8 + "”")).title
    assert len(title) <= WIDTH and title.endswith(" …")
    assert brief_of(approval(tool="x" * 100, source_label="")).title == "x" * (WIDTH - 2) + " …"
