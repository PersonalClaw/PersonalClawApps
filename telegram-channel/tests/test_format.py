"""MarkdownV2 escaping — table-driven over the full reserved set + markup preservation.

Telegram's MarkdownV2 reserves 18 characters that must be backslash-escaped in plain
text, with narrower rules inside code spans and link targets. A miss means the Bot
API rejects the whole message with ``400 can't parse entities``, so the escaper is
pinned exhaustively here."""

from __future__ import annotations

import pytest

from telegram_runtime.format import (
    TELEGRAM_MAX_TEXT,
    escape_code,
    escape_link_url,
    escape_markdown_v2,
    split_message,
    to_markdown_v2,
)

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


class TestSplitMessage:
    def test_short_text_single_part(self):
        assert split_message("hi") == ["hi"]

    def test_empty_is_no_parts(self):
        assert split_message("") == []

    def test_splits_on_newline_boundary(self):
        text = "a" * 3000 + "\n" + "b" * 3000
        parts = split_message(text, limit=4096)
        assert len(parts) == 2
        assert parts[0] == "a" * 3000
        assert parts[1] == "b" * 3000
        assert all(len(p) <= 4096 for p in parts)

    def test_hard_split_when_no_newline(self):
        text = "x" * 5000
        parts = split_message(text, limit=4096)
        assert len(parts) == 2
        assert len(parts[0]) == 4096
        assert len(parts[1]) == 904

    def test_default_limit_is_telegram_max(self):
        assert TELEGRAM_MAX_TEXT == 4096
        assert split_message("x" * 4096) == ["x" * 4096]
        assert len(split_message("x" * 4097)) == 2
