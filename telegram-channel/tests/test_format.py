"""MarkdownV2 escaping — table-driven over the full reserved set + markup preservation.

Telegram's MarkdownV2 reserves 18 characters that must be backslash-escaped in plain
text, with narrower rules inside code spans and link targets. A miss means the Bot
API rejects the whole message with ``400 can't parse entities``, so the escaper is
pinned exhaustively here."""

from __future__ import annotations

import pytest

from telegram_runtime.format import (
    TELEGRAM_MAX_TEXT,
    MessagePart,
    escape_code,
    escape_link_url,
    escape_markdown_v2,
    render_parts,
    to_markdown_v2,
    utf16_len,
)

from _v2 import parse_markdown_v2

# The full MarkdownV2 reserved set (Bot API docs, "MarkdownV2 style").
_RESERVED = list("_*[]()~`>#+-=|{}.!")


class TestEscapePlainText:
    @pytest.mark.parametrize("ch", _RESERVED)
    def test_every_reserved_char_is_backslash_escaped(self, ch):
        assert escape_markdown_v2(ch) == "\\" + ch

    def test_whole_reserved_run(self):
        src = "".join(_RESERVED)
        expected = "".join("\\" + c for c in _RESERVED)
        assert escape_markdown_v2(src) == expected

    @pytest.mark.parametrize("ch", ["a", "Z", "9", " ", "\n", "é", "\x00"])
    def test_non_reserved_chars_pass_through(self, ch):
        assert escape_markdown_v2(ch) == ch

    def test_empty(self):
        assert escape_markdown_v2("") == ""

    def test_mixed_sentence(self):
        assert escape_markdown_v2("Cost: $5 (was $9.99)!") == r"Cost: $5 \(was $9\.99\)\!"


class TestEscapeCode:
    def test_only_backtick_and_backslash(self):
        # A reserved char that is NOT ` or \ stays literal inside a code span.
        assert escape_code("a.b-c!") == "a.b-c!"
        assert escape_code("x`y") == "x\\`y"
        assert escape_code("x\\y") == "x\\\\y"

    def test_backslash_escaped_before_backtick(self):
        # Order matters: escape backslashes first so a literal \ + ` doesn't collapse.
        assert escape_code("\\`") == "\\\\\\`"


class TestEscapeLinkUrl:
    def test_only_paren_and_backslash(self):
        assert escape_link_url("https://x.com/a.b?c=d") == "https://x.com/a.b?c=d"
        assert escape_link_url("https://x.com/a(b)") == "https://x.com/a(b\\)"
        assert escape_link_url("a\\b") == "a\\\\b"


class TestToMarkdownV2:
    def test_plain_text_fully_escaped(self):
        assert to_markdown_v2("Hello, world.") == r"Hello, world\."

    def test_bold_double_star_becomes_single(self):
        assert to_markdown_v2("**bold**") == "*bold*"

    def test_bold_double_underscore_becomes_single_star(self):
        assert to_markdown_v2("__bold__") == "*bold*"

    def test_italic_single_star(self):
        assert to_markdown_v2("*em*") == "_em_"

    def test_italic_single_underscore(self):
        assert to_markdown_v2("_em_") == "_em_"

    def test_bold_inner_reserved_is_escaped(self):
        # Inner text of an entity still escapes reserved chars (per plain rules).
        assert to_markdown_v2("**a.b**") == r"*a\.b*"

    def test_inline_code_preserved_with_code_escaping(self):
        # Reserved chars inside code stay literal; backticks/backslashes escape.
        assert to_markdown_v2("use `a.b-c` here") == "use `a.b-c` here"

    def test_fenced_code_block(self):
        out = to_markdown_v2("```python\nx = 1.0\n```")
        assert out.startswith("```\n") and out.endswith("\n```")
        assert "x = 1.0" in out  # dot NOT escaped inside a fence

    def test_link_preserved_url_reserved_chars_left_literal(self):
        # A URL's reserved chars (., -, _) are NOT escaped inside the link target;
        # only ) and \ are (escape_link_url is unit-tested directly above).
        out = to_markdown_v2("see [my site](https://x.com/a-b_c.d)")
        assert out == r"see [my site](https://x.com/a-b_c.d)"

    def test_link_label_reserved_escaped(self):
        out = to_markdown_v2("[a.b](https://x.com)")
        assert r"[a\.b](https://x.com)" == out

    def test_ansi_stripped(self):
        assert to_markdown_v2("\x1b[31mred\x1b[0m") == "red"

    def test_no_stray_placeholder_sentinels(self):
        # The internal \x00N\x00 stash must never leak into output.
        out = to_markdown_v2("**a** and `b` and [c](http://d.e)")
        assert "\x00" not in out

    def test_mixed_document_parses_structurally(self):
        src = "Title\n\n**Important:** run `make test` — see [docs](https://x.com/y_z)."
        out = to_markdown_v2(src)
        assert "\x00" not in out
        assert "*Important:*" in out
        assert "`make test`" in out
        assert "[docs](https://x.com/y_z)" in out
        # trailing period outside any entity is escaped
        assert out.endswith(r"\.")


class TestRenderParts:
    """The source is split, then each part rendered on its own — never the rendering split."""

    def test_short_text_is_one_part(self):
        assert render_parts("Hello. **bold**") == [MessagePart(r"Hello\. *bold*", "Hello. **bold**")]

    def test_empty_and_blank_text_is_no_parts(self):
        assert render_parts("") == []
        assert render_parts("\n\n  \n") == []
        assert render_parts("\x1b[31m\x1b[0m") == []

    def test_ansi_is_stripped_from_both_halves(self):
        [part] = render_parts("\x1b[31mred\x1b[0m")
        assert part == MessagePart("red", "red")

    def test_splits_at_a_line_break(self):
        text = "a" * 3000 + "\n" + "b" * 3000
        parts = render_parts(text)
        assert [p.plain for p in parts] == ["a" * 3000, "b" * 3000]

    def test_blank_lines_at_a_cut_are_not_sent(self):
        text = "a" * 3000 + "\n\n\n" + "b" * 3000
        assert [p.plain for p in render_parts(text)] == ["a" * 3000, "b" * 3000]

    def test_the_rendering_is_what_must_fit(self):
        """Every `.` gains a backslash: 3,000 dots are 6,000 characters of MarkdownV2."""
        parts = render_parts(".\n" * 3000)
        assert len(parts) >= 2
        assert all(utf16_len(p.markdown_v2) <= TELEGRAM_MAX_TEXT for p in parts)
        assert sum(p.plain.count(".") for p in parts) == 3000

    def test_limit_is_counted_in_utf16_units(self):
        parts = render_parts(("🙂" * 30 + "\n") * 100, limit=1000)
        assert all(utf16_len(p.markdown_v2) <= 1000 for p in parts)
        assert all(utf16_len(p.plain) <= 1000 for p in parts)
        assert sum(p.plain.count("🙂") for p in parts) == 3000

    def test_every_part_parses_on_its_own(self):
        text = ("Step 1. Run `make test` — see [docs](https://x.com/a_b).\n" * 120)
        for part in render_parts(text):
            parse_markdown_v2(part.markdown_v2)  # raises if Telegram would refuse it

    def test_a_cut_code_block_is_closed_and_reopened_with_its_language(self):
        text = "intro\n```python\n" + "x = 1\n" * 1000 + "```\nafter"
        parts = render_parts(text)
        assert len(parts) >= 2
        assert parts[0].plain.startswith("intro\n```python\n")
        for part in parts[:-1]:
            assert part.plain.endswith("\n```")
        for part in parts[1:]:
            assert part.plain.startswith("```python\n")
        assert parts[-1].plain.endswith("```\nafter")
        for part in parts:
            plain, entities = parse_markdown_v2(part.markdown_v2)
            assert [e["type"] for e in entities] == ["pre"]
        assert sum(p.plain.count("x = 1") for p in parts) == 1000

    def test_a_long_or_spaced_info_string_is_not_carried_over(self):
        for info in ("a" * 33, "python title=x.py"):
            parts = render_parts(f"```{info}\n" + "y\n" * 3000 + "```")
            assert len(parts) >= 2
            assert all(p.plain.startswith("```\n") for p in parts[1:])

    def test_a_part_does_not_end_on_the_line_that_opens_a_block(self):
        text = "p" * 4080 + "\n```\ncode line\n```"
        parts = render_parts(text)
        assert parts[0].plain == "p" * 4080
        assert parts[1].plain == "```\ncode line\n```"

    def test_a_line_longer_than_a_part_is_cut_at_a_space(self):
        parts = render_parts("word " * 2000)
        assert len(parts) >= 3
        assert all(token == "word" for p in parts for token in p.plain.split())
        assert sum(len(p.plain.split()) for p in parts) == 2000

    def test_a_run_with_no_space_is_cut_where_it_must_be(self):
        parts = render_parts("x" * 5000)
        assert [len(p.plain) for p in parts] == [TELEGRAM_MAX_TEXT, 5000 - TELEGRAM_MAX_TEXT]

    def test_a_code_line_longer_than_a_part_is_cut_whole_and_stays_code(self):
        parts = render_parts("```\n" + "z " * 3000 + "\n```")
        assert len(parts) >= 2
        assert "".join(parse_markdown_v2(p.markdown_v2)[0] for p in parts) == "z " * 3000
        for part in parts:
            assert part.plain.startswith("```\n") and part.plain.endswith("\n```")

    def test_default_limit_is_telegram_max(self):
        assert TELEGRAM_MAX_TEXT == 4096
        assert len(render_parts("x" * 4096)) == 1
        assert len(render_parts("x" * 4097)) == 2
