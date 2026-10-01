"""A reply's code spans, and the underscores inside its words, reach Telegram as they were written.

Two ways a reply used to arrive changed. A code span inside bold came back as the renderer's own
numbered placeholder between two NUL characters ("Parcel \\x000\\x00:"), because bold was stashed
with the code's placeholder still inside it and only one level of stash was ever put back. And an
underscore between two letters was read as italics, so a tool named
``list_allowed_directories`` arrived as "listalloweddirectories" with "allowed" slanted.

Every case is judged by ``_v2``, a MarkdownV2 parser written from the Bot API's rules rather
than from the renderer: what Telegram would show, and which spans it would format.
"""

from __future__ import annotations

import random

import pytest

from _v2 import in_entity, parse_markdown_v2
from telegram_runtime.delivery import TelegramDelivery
from telegram_runtime.format import render_parts, to_markdown_v2

from test_delivery import FakeAPI


def shown(source: str) -> tuple[str, list[dict]]:
    """What Telegram shows for *source*, and the entities it formats."""
    rendered = to_markdown_v2(source)
    assert "\x00" not in rendered, f"a placeholder leaked: {rendered!r}"
    return parse_markdown_v2(rendered)


def kinds(entities: list[dict]) -> set[str]:
    return {e["type"] for e in entities}


class TestCodeSpansNextToBold:
    def test_a_code_span_inside_a_bold_label_arrives_as_code(self):
        plain, entities = shown(
            "- **Parcel `httpx.ReadTimeout`:** issue `ORDERS-API-4F2` is marked regressed."
        )
        assert plain == "- Parcel httpx.ReadTimeout: issue ORDERS-API-4F2 is marked regressed."
        assert in_entity(plain, entities, "httpx.ReadTimeout", "code")
        assert in_entity(plain, entities, "ORDERS-API-4F2", "code")
        assert in_entity(plain, entities, "Parcel ", "bold")
        assert in_entity(plain, entities, ":", "bold")

    def test_a_bold_label_that_opens_with_a_code_span(self):
        plain, entities = shown(
            "- **`event_dedupe` table:** check that it's still under 60M rows (retention job)."
        )
        assert plain == "- event_dedupe table: check that it's still under 60M rows (retention job)."
        assert in_entity(plain, entities, "event_dedupe", "code")
        assert in_entity(plain, entities, " table:", "bold")

    def test_a_whole_reply_keeps_every_code_span(self):
        reply = (
            "Here's what I found.\n\n"
            "**On call**\n"
            "- **Parcel `httpx.ReadTimeout`:** issue `ORDERS-API-4F2` is marked regressed.\n"
            "- **`event_dedupe` table:** check that it's still under 60M rows.\n"
            "- **#412 (yours):** the double-escaping bug (`&amp;amp;`) is still open.\n\n"
            "Per [Lesson 1], keep the tone conversational."
        )
        [part] = render_parts(reply)
        assert "\x00" not in part.markdown_v2
        plain, entities = parse_markdown_v2(part.markdown_v2)
        for code in ("httpx.ReadTimeout", "ORDERS-API-4F2", "event_dedupe", "&amp;amp;"):
            assert in_entity(plain, entities, code, "code"), code
        assert "Per [Lesson 1], keep the tone conversational." in plain
        assert in_entity(plain, entities, "On call", "bold")


class TestUnderscoresInsideWords:
    @pytest.mark.parametrize("name", [
        "mcp/notes-vault/list_allowed_directories",
        "mcp/notes-vault/read_multiple_files",
        "mcp/github/list_pull_requests",
        "list_dir",
        "a_b_c_d",
    ])
    def test_a_name_in_a_reply_keeps_its_underscores(self, name):
        plain, entities = shown(f"Called {name} twice.")
        assert plain == f"Called {name} twice."
        assert entities == []

    @pytest.mark.asyncio
    async def test_a_progress_line_shows_the_tool_title_as_written(self):
        """A progress line's title is a tool's name or purpose, plain text: nothing in it is
        markup, so a star or a backtick shows as itself."""
        titles = [
            "mcp/notes-vault/list_allowed_directories",
            "mcp/notes-vault/read_multiple_files",
            "list_dir",
            "Find *.py files under src/*",
            "Run `make test` in C:\\work\\repo",
        ]
        d = TelegramDelivery(FakeAPI(), lambda: "42")
        clock = {"t": 0.0}
        d._now = lambda: clock["t"]
        sts = await d.start_stream("123", initial_text="Thinking…")
        for i, title in enumerate(titles):
            clock["t"] += 5.0
            await d.append_stream_task("123", sts, f"tool_{i}", title, "complete")
        plain, entities = parse_markdown_v2(d._api.edits[-1]["text"])
        assert plain == "\n".join(["Thinking…", *(f"✅ {t}" for t in titles)])
        assert entities == []
        await d.stop_stream("123", sts)
        plain, entities = parse_markdown_v2(d._api.edits[-1]["text"])
        assert plain == "\n".join(f"✅ {t}" for t in titles)
        assert entities == []


#: (source, what Telegram shows, [(text, entity it lies in)], every entity kind formatted)
TRICKY = [
    ("**a `b` c**", "a b c", [("a ", "bold"), ("b", "code"), (" c", "bold")], {"bold", "code"}),
    ("*see `x_y` here*", "see x_y here", [("see ", "italic"), ("x_y", "code")], {"italic", "code"}),
    ("_note: `a*b`_", "note: a*b", [("note: ", "italic"), ("a*b", "code")], {"italic", "code"}),
    ("_`x`_", "x", [("x", "code")], {"code"}),
    ("Tool **`read_text_file`** failed", "Tool read_text_file failed",
     [("read_text_file", "code")], {"code"}),
    ("**`alpha` and `beta`**", "alpha and beta",
     [("alpha", "code"), (" and ", "bold"), ("beta", "code")], {"bold", "code"}),
    ("***both***", "both", [("both", "bold"), ("both", "italic")], {"bold", "italic"}),
    ("**bold _and italic_ inside**", "bold and italic inside",
     [("bold and italic inside", "bold"), ("and italic", "italic")], {"bold", "italic"}),
    ("**a**_b_", "ab", [("a", "bold"), ("b", "italic")], {"bold", "italic"}),
    ("snake_case_word, __dunder_name and a_b_c", "snake_case_word, __dunder_name and a_b_c",
     [], set()),
    ("file_name.py and _real italic_", "file_name.py and real italic",
     [("real italic", "italic")], {"italic"}),
    ("_a_b", "_a_b", [], set()),
    ("[read_me](https://example.com/a_b_(c))", "read_me", [("read_me", "text_link")],
     {"text_link"}),
    ("see [the `cfg_x` docs](https://example.com/x_y)", "see the cfg_x docs",
     [("the cfg_x docs", "text_link")], {"text_link"}),
    ("**[docs_page](https://example.com/d_e)**", "docs_page",
     [("docs_page", "bold"), ("docs_page", "text_link")], {"bold", "text_link"}),
    ("![chart_1](https://example.com/c_1.png)", "chart_1", [("chart_1", "text_link")],
     {"text_link"}),
    ("``a ` b``", "a ` b", [("a ` b", "code")], {"code"}),
    ("```code```", "code", [("code", "code")], {"code"}),
    ("2 * 3 * 4 = 24", "2 * 3 * 4 = 24", [], set()),
    ("**unclosed bold and `code`", "**unclosed bold and code", [("code", "code")], {"code"}),
    ("`unclosed code and **bold**", "`unclosed code and bold", [("bold", "bold")], {"bold"}),
    ("Reserved: _ * [ ] ( ) ~ ` > # + - = | { } . !",
     "Reserved: _ * [ ] ( ) ~ ` > # + - = | { } . !", [], set()),
    ("C:\\work\\notes\\today", "C:\\work\\notes\\today", [], set()),
    ("\\*not italic\\* and 1\\_000", "*not italic* and 1_000", [], set()),
    ("~~gone~~ and ~5 min", "gone and ~5 min", [("gone", "strikethrough")], {"strikethrough"}),
    ("**🙂 `x`** done", "🙂 x done", [("🙂 ", "bold"), ("x", "code")], {"bold", "code"}),
    ("```\n**not bold** and `x`\n```", "**not bold** and `x`", [("**not bold** and `x`", "pre")],
     {"pre"}),
]


class TestTrickyMarkdown:
    @pytest.mark.parametrize(("source", "plain", "spans", "formatted"), TRICKY)
    def test_it_shows_as_written(self, source, plain, spans, formatted):
        shown_plain, entities = shown(source)
        assert shown_plain == plain
        for text, kind in spans:
            assert in_entity(shown_plain, entities, text, kind), (text, kind, entities)
        assert kinds(entities) == formatted

    def test_a_link_keeps_its_address(self):
        _, entities = shown("[read_me](https://example.com/a_b_(c)) and [x](<https://example.com/a b>)")
        assert [e["url"] for e in entities] == [
            "https://example.com/a_b_(c)", "https://example.com/a b",
        ]


#: Pieces random replies are built from: every MarkdownV2 reserved character, the markers the
#: renderer reads, and words, names and links with underscores in them.
_PIECES = [
    "*", "**", "***", "_", "__", "`", "``", "```", "~", "~~", "[", "]", "(", ")", "![", "\\",
    ">", "#", "+", "-", "=", "|", "{", "}", ".", "!", " ", " ", " ", "\n", "word", "snake_case",
    "read_text_file", "https://example.com/a_b", "[label](https://example.com/x_y)", "🙂",
]


class TestNoReplyArrivesBroken:
    def test_random_replies_always_parse_and_never_carry_a_placeholder(self):
        rng = random.Random(20261001)
        for _ in range(3000):
            source = "".join(rng.choice(_PIECES) for _ in range(rng.randint(1, 40)))
            rendered = to_markdown_v2(source)
            assert "\x00" not in rendered, source
            parse_markdown_v2(rendered)  # raises where Telegram would refuse the message

    def test_random_long_replies_split_into_parts_that_each_parse(self):
        rng = random.Random(7)
        for _ in range(20):
            source = "".join(rng.choice(_PIECES) for _ in range(rng.randint(400, 2400)))
            for part in render_parts(source, limit=600):
                assert "\x00" not in part.markdown_v2
                parse_markdown_v2(part.markdown_v2)
