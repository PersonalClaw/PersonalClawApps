"""A rich mail's HTML part is written by this app from the mail's own text, and is nothing else.

A mail client shows the HTML part of a mail in place of its plain part. So the HTML part says
what the plain part says, loads nothing when the mail is opened, runs nothing, and shows where
every link goes. Core hands ``deliver_rich`` a payload shaped for another channel beside the
message's text, and markup in either one reaches the reader as the characters it is, never as
markup.

Each test reads the part the way a mail client does, with a parser, off what went on the wire.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from email_runtime import html_part
from email_runtime.delivery import EmailDelivery, ThreadStore
from _fakes import FakeSmtpServer

AGENT = "agent@example.com"
BOB = "bob@example.com"

#: What core hands every channel's ``deliver_rich``: Block Kit, the format the agent's notify tool
#: asks for, beside the message's text.
BLOCKS = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Weekly report* is ready"}}]

#: Every element the HTML part may hold. The one attribute is a link's address.
ALLOWED = {"html", "body", "p", "br", "strong", "em", "del", "code", "pre", "hr", "a"}


class _Read(HTMLParser):
    """An HTML part as a mail client reads it: its elements, their attributes, each link's
    address and words, and the text it shows, a line break for each ``<br>`` and block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[str] = []
        self.attributes: list[tuple[str, str]] = []
        self.links: list[tuple[str, str]] = []
        self._shown: list[str] = []
        self._href: str | None = None
        self._words: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append(tag)
        self.attributes.extend((tag, name) for name, _ in attrs)
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._words = []
        if tag in ("br", "p", "pre", "hr"):
            self._shown.append("\n")

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._words)))
            self._href = None

    def handle_data(self, data):
        self._shown.append(data)
        if self._href is not None:
            self._words.append(data)

    @property
    def shown(self) -> str:
        """The lines the part shows, blank ones dropped."""
        lines = "".join(self._shown).split("\n")
        return "\n".join(line for line in lines if line.strip())


@pytest.fixture
def mail(tmp_path):
    """A delivery over a fake SMTP sink, with thread state in a tmp file."""
    smtp = FakeSmtpServer()
    store = ThreadStore(path_provider=lambda: tmp_path / "threads.json")
    return EmailDelivery(smtp, AGENT, owner_id=AGENT, threads=store), smtp


def _parts(smtp: FakeSmtpServer) -> dict[str, str]:
    """The last mail's text parts, by type."""
    return {
        part.get_content_type(): part.get_content()
        for part in smtp.last.walk()
        if part.get_content_maintype() == "text"
    }


def _read(markup: str) -> _Read:
    reader = _Read()
    reader.feed(markup)
    reader.close()
    return reader


def _wire(smtp: FakeSmtpServer) -> str:
    return smtp.last.as_string()


class TestTheHtmlPartIsTheText:
    @pytest.mark.asyncio
    async def test_it_says_what_the_plain_part_says_with_its_formatting(self, mail):
        delivery, smtp = mail
        text = "Ship **today**: see [the plan](https://plans.example.com/q3) & tell <the team>."
        await delivery.deliver_rich(BOB, BLOCKS, text)

        parts = _parts(smtp)
        assert parts["text/plain"].strip() == text
        read = _read(parts["text/html"])
        assert "strong" in read.elements
        assert read.shown == (
            "Ship today: see the plan (https://plans.example.com/q3) & tell <the team>."
        )

    @pytest.mark.asyncio
    async def test_markup_the_payload_carries_is_not_sent(self, mail):
        """A payload of another shape, a page of HTML included, is not what goes out: the mail is
        its text."""
        delivery, smtp = mail
        payload = {"html": '<p>Q3 chart</p><img src="https://charts.example.com/q3.png">'}
        await delivery.deliver_rich(BOB, payload, "The Q3 chart is attached.")

        read = _read(_parts(smtp)["text/html"])
        assert "img" not in read.elements
        assert "charts.example.com" not in _wire(smtp)
        assert read.shown == "The Q3 chart is attached."

    @pytest.mark.asyncio
    async def test_markup_in_the_text_is_shown_as_the_characters_it_is(self, mail):
        delivery, smtp = mail
        text = (
            'The page read: <img src="https://images.example.com/a.png" width="1"> and <b>sale</b>'
        )
        await delivery.deliver_rich(BOB, BLOCKS, text)

        read = _read(_parts(smtp)["text/html"])
        assert set(read.elements) <= ALLOWED
        assert "img" not in read.elements and "b" not in read.elements
        assert read.shown == text

    @pytest.mark.asyncio
    async def test_only_a_web_or_mail_address_is_a_link_and_each_shows_where_it_goes(self, mail):
        delivery, smtp = mail
        text = (
            "Read [the notes](file:///home/user/notes.txt), [the report](reports/q3.md), "
            "[write to Ana](mailto:ana@example.com) and https://example.com/a."
        )
        await delivery.deliver_rich(BOB, BLOCKS, text)

        read = _read(_parts(smtp)["text/html"])
        assert read.links == [
            ("mailto:ana@example.com", "mailto:ana@example.com"),
            ("https://example.com/a", "https://example.com/a"),
        ]
        assert read.shown == (
            "Read [the notes](file:///home/user/notes.txt), [the report](reports/q3.md), "
            "write to Ana (mailto:ana@example.com) and https://example.com/a."
        )

    @pytest.mark.asyncio
    async def test_a_link_whose_words_look_like_another_address_shows_its_own(self, mail):
        delivery, smtp = mail
        await delivery.deliver_rich(
            BOB, BLOCKS, "Sign in at [https://bank.example.com](https://other.example.net/login)."
        )

        read = _read(_parts(smtp)["text/html"])
        assert read.links == [
            ("https://other.example.net/login", "https://other.example.net/login")
        ]
        assert read.shown == (
            "Sign in at https://bank.example.com (https://other.example.net/login)."
        )

    @pytest.mark.asyncio
    async def test_it_is_made_of_formatting_alone(self, mail):
        delivery, smtp = mail
        text = "\n".join([
            "# Weekly report",
            "",
            "- **Build** passed, *mostly*",
            "- ~~Deploy~~ moved to Friday",
            "",
            "```html",
            "<b>kept as written</b>",
            "```",
            "",
            "| Team | Status |",
            "|------|--------|",
            "| Web  | green  |",
            "",
            "---",
            "Thanks, `ana` & co",
        ])
        await delivery.deliver_rich(BOB, BLOCKS, text)

        read = _read(_parts(smtp)["text/html"])
        assert set(read.elements) <= ALLOWED
        assert {"strong", "em", "del", "pre", "code", "hr"} <= set(read.elements)
        assert read.attributes == []
        assert "<b>kept as written</b>" in read.shown
        assert "| Web  | green  |" in read.shown

    @pytest.mark.asyncio
    async def test_line_breaks_are_kept(self, mail):
        delivery, smtp = mail
        await delivery.deliver_rich(BOB, BLOCKS, "Hi Bob,\nthe build passed.\n\nThanks")

        html = _parts(smtp)["text/html"]
        assert "<p>Hi Bob,<br>the build passed.</p><p>Thanks</p>" in html


class TestAPartTheCheckRefusesGoesAsPlainText:
    """What the renderer writes is read back by a parser before it is sent. Anything it was not
    meant to write, or a renderer that fails, sends the mail as its plain text alone."""

    @pytest.mark.asyncio
    async def test_an_element_outside_the_formatting_sends_the_text_alone(self, mail, monkeypatch):
        delivery, smtp = mail
        monkeypatch.setattr(
            html_part, "render",
            lambda text: f"<html><body><table><tr><td>{text}</td></tr></table></body></html>",
        )
        await delivery.deliver_rich(BOB, BLOCKS, "Status: green")

        assert smtp.last.get_content_type() == "text/plain"
        assert smtp.body_text().strip() == "Status: green"

    @pytest.mark.asyncio
    async def test_a_renderer_that_fails_sends_the_text_alone(self, mail, monkeypatch):
        delivery, smtp = mail

        def fails(text):
            raise ValueError("cannot render")

        monkeypatch.setattr(html_part, "render", fails)
        await delivery.deliver_rich(BOB, BLOCKS, "Status: green")

        assert smtp.last.get_content_type() == "text/plain"
        assert smtp.body_text().strip() == "Status: green"


class TestTheCheck:
    def test_what_the_renderer_writes_passes(self):
        markup = html_part.render(
            "**Done**: see https://example.com/a and [Ana](mailto:ana@example.com)\n\n---\n`x`"
        )
        assert html_part.refusal(markup) == ""

    @pytest.mark.parametrize(
        "markup",
        [
            "<p>a table</p><table><tr><td>1</td></tr></table>",
            '<p title="note">a paragraph with an attribute</p>',
            '<a href="https://example.com/a">https://example.com/b</a>',
            '<a href="file:///home/user/notes.txt">file:///home/user/notes.txt</a>',
            '<a href="https://example.com/a" title="a">https://example.com/a</a>',
            "<p>a remark</p><!-- a comment -->",
        ],
    )
    def test_anything_else_is_refused(self, markup):
        assert html_part.refusal(markup)


class TestTheFormatting:
    @pytest.mark.parametrize(
        ("text", "html"),
        [
            ("snake_case_name", "snake_case_name"),
            ("2 * 3 * 4", "2 * 3 * 4"),
            ("**bold *both* bold**", "<strong>bold <em>both</em> bold</strong>"),
            ("*a* and _b_", "<em>a</em> and <em>b</em>"),
            ("__strong__ and ~~old~~", "<strong>strong</strong> and <del>old</del>"),
            ("`a < b` & *c*", "<code>a &lt; b</code> &amp; <em>c</em>"),
            ("**see https://example.com/a**", (
                '<strong>see <a href="https://example.com/a">https://example.com/a</a></strong>'
            )),
            ("(https://example.com/a).", (
                '(<a href="https://example.com/a">https://example.com/a</a>).'
            )),
        ],
    )
    def test_a_line_reads_as_markdown_does(self, text, html):
        assert html_part.render(text) == f"<html><body><p>{html}</p></body></html>"

    def test_an_address_holding_a_format_character_is_text_and_the_rest_is_formatted(self):
        """A zero-width space, often left in an address copied from a page, or any other format
        character can make an address read as another one: it is shown as text, not as a link."""
        read = _read(html_part.render("Open https://example.com/a​b **now**"))
        assert read.links == []
        assert "strong" in read.elements

    def test_a_heading_is_a_bold_line_and_indentation_is_kept(self):
        assert html_part.render("## Next steps\n- one\n  - two") == (
            "<html><body><p><strong>Next steps</strong></p>"
            "<p>- one<br>&nbsp;&nbsp;- two</p></body></html>"
        )

    def test_an_empty_or_overlong_text_has_no_html_part(self):
        assert html_part.html_for("") == ""
        assert html_part.html_for("  \n ") == ""
        assert html_part.html_for("x" * (html_part.MAX_FORMATTED_CHARS + 1)) == ""
