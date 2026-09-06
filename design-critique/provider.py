"""Design-critique tool provider — ``design_critique_page`` / ``design_critique_image``
/ ``design_critique_rubric``.

An installable ``tool`` provider app. It imports core only through ``personalclaw.sdk.*``
and its ``app.json`` points ``provider.implementation`` at ``provider:create_provider``.

**What this is for.** The review a colleague would give you, before you spend their
attention: concrete craft and accessibility findings about a page or a screenshot, each with
the evidence that produced it and the change that clears it.

**The two halves, kept honest.** Judgement about a design splits into what a machine can
MEASURE and what it must LOOK at:

* Measurable, and therefore done here, deterministically: markup accessibility (label
  association, alt text, heading order, focus order, declared colour contrast) and
  pixel-level craft (contrast of the palette actually rendered, palette sprawl, margin
  balance, alignment discipline, colour-vision safety, density).
* Not measurable, and therefore NOT faked here: whether the hierarchy matches the user's
  goal, whether the copy earns its space, whether an affordance reads as clickable.
  ``design_critique_rubric`` hands those back as an explicit checklist for the caller's own
  vision pass, so the visual half of the review is a stated procedure rather than an
  improvisation that changes every run.

A finding is never invented to fill a report. When a check cannot run — no Pillow, a page
that refuses the fetch, a client-rendered shell with no static markup — the tool says so in
place of the finding and names what to do instead.

**Reuse, not new backends.** The page path fetches through the SDK's guarded egress
chokepoint (``sdk.net.fetch``, so the operator's Security → Network posture applies to this
app exactly as it does to core) and takes its rendered-content reading from
``sdk.net.web_fetch(render=True)`` — the shipped headless-render path — rather than driving
a browser of its own.

``list_tools`` is STATIC: the definitions exist independent of Pillow being importable or a
network being reachable, so the tool surface a host advertises never depends on the machine
it mounted on — a missing decoder produces a refusal at invoke time rather than a tool that
silently vanished.
"""

from __future__ import annotations

import colorsys
import logging
import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from personalclaw.sdk.tool import (
    RiskLevel,
    ToolDefinition,
    ToolProvider,
    ToolResult,
)

logger = logging.getLogger("design_critique")

# ── budgets ──────────────────────────────────────────────────────────────────
#: A critique is only useful if it can be acted on in one sitting, so the report is
#: capped and severity-ordered rather than exhaustive.
_DEFAULT_MAX_FINDINGS = 25
_MAX_MAX_FINDINGS = 80
_MAX_OUTPUT_CHARS = 14_000
#: Per-finding evidence: enough to locate the instance, never a dump of the page.
_MAX_EVIDENCE_ITEMS = 4
_EVIDENCE_CHARS = 90
#: The parse-side bound on markup (the egress policy caps fetched bytes separately).
_MAX_MARKUP_CHARS = 2_000_000
#: Screenshot statistics run on a downscaled copy — the findings are about proportions,
#: and a 4K capture costs seconds at full size for the same answer.
_STAT_MAX_EDGE = 480
#: Refuse an image whose declared pixel count is absurd before decoding it.
_MAX_IMAGE_PIXELS = 80_000_000
_MAX_IMAGE_BYTES = 40 * 1024 * 1024

_SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}

RESPONSE_TYPE_PAGE = "design.critique.page"
RESPONSE_TYPE_IMAGE = "design.critique.image"
RESPONSE_TYPE_RUBRIC = "design.critique.rubric"


@dataclass(frozen=True)
class Finding:
    """One reviewable defect: what, where, why it matters, and the change that clears it."""

    id: str
    kind: str  # "a11y" | "heuristic"
    severity: str  # "blocker" | "major" | "minor"
    title: str
    detail: str
    fix: str
    guideline: str = ""  # WCAG reference for an a11y finding; empty for craft heuristics
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "fix": self.fix,
            "guideline": self.guideline,
            "evidence": list(self.evidence),
        }


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 3), f.kind, f.id))


def _clip(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= _EVIDENCE_CHARS else flat[: _EVIDENCE_CHARS - 1] + "…"


def _evidence(items: list[str]) -> tuple[str, ...]:
    """First few instances, clipped — the report locates a defect, it doesn't inventory it."""
    shown = [_clip(i) for i in items[:_MAX_EVIDENCE_ITEMS]]
    extra = len(items) - len(shown)
    if extra > 0:
        shown.append(f"…and {extra} more")
    return tuple(shown)


# ── colour ───────────────────────────────────────────────────────────────────
# A small named-colour map rather than the full CSS list: these are the names that
# actually appear in hand-written styles, and an unknown name resolves to None (no
# finding) instead of a guess.
_NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "lime": (0, 255, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "aqua": (0, 255, 255),
    "magenta": (255, 0, 255),
    "fuchsia": (255, 0, 255),
    "silver": (192, 192, 192),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "darkgray": (169, 169, 169),
    "darkgrey": (169, 169, 169),
    "lightgray": (211, 211, 211),
    "lightgrey": (211, 211, 211),
    "whitesmoke": (245, 245, 245),
    "gainsboro": (220, 220, 220),
    "navy": (0, 0, 128),
    "teal": (0, 128, 128),
    "orange": (255, 165, 0),
    "purple": (128, 0, 128),
    "maroon": (128, 0, 0),
    "olive": (128, 128, 0),
}

_HEX_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
_RGB_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", re.I)


def parse_color(value: str) -> tuple[int, int, int] | None:
    """A CSS colour token as RGB, or ``None`` when it isn't one this app understands.

    Deliberately narrow: hex, ``rgb()``/``rgba()`` and the common names. ``currentColor``,
    ``var(--x)``, ``hsl()`` and gradients resolve to None, so no finding is fabricated from
    a value whose real colour is only known at render time.
    """
    token = (value or "").strip().lower()
    if not token:
        return None
    if token in _NAMED_COLORS:
        return _NAMED_COLORS[token]
    m = _HEX_RE.search(token)
    if m:
        digits = m.group(1)
        if len(digits) in (3, 4):
            r, g, b = (int(c * 2, 16) for c in digits[:3])
            return (r, g, b)
        if len(digits) in (6, 8):
            return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
        return None
    m = _RGB_RE.search(token)
    if m:
        r, g, b = (min(255, int(part)) for part in m.groups())
        return (r, g, b)
    return None


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.2 relative luminance."""

    def channel(value: int) -> float:
        s = value / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG 2.2 contrast ratio between two colours (1.0 … 21.0)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def simulate_deuteranopia(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Approximate how ``rgb`` reads to the most common form of colour blindness.

    The standard channel-mixing approximation for deuteranopia. Its red/green block is
    near-singular by design — that near-collapse IS the condition being modelled — so a
    red-green pair comes out of it far closer together than it went in, while a
    blue/orange pair comes out barely changed.

    It is used only comparatively (see :func:`analyze_image`): the caller asks how much of
    a pair's separation SURVIVES the transform, never what the absolute simulated colour
    is, so the approximation's inaccuracy cannot by itself decide a finding.
    """
    r, g, b = (c / 255.0 for c in rgb)
    mixed = (
        0.625 * r + 0.375 * g,
        0.700 * r + 0.300 * g,
        0.300 * g + 0.700 * b,
    )
    out = tuple(max(0, min(255, round(c * 255))) for c in mixed)
    return (out[0], out[1], out[2])


def _rgb_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


# ── markup analysis ──────────────────────────────────────────────────────────

#: Link text that names no destination. Matched against the whole accessible name.
_VAGUE_LINK_TEXT = frozenset(
    {
        "click here",
        "here",
        "read more",
        "more",
        "learn more",
        "this link",
        "link",
        "continue",
        "details",
        "info",
        ">",
        "»",
        "→",
    }
)

#: Input types that carry their own name or take no user entry, so a missing label is
#: not a defect.
_UNLABELLED_OK_INPUT_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})

_TEXTUAL_TAGS = frozenset({"a", "button", "label", "h1", "h2", "h3", "h4", "h5", "h6"})

_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([0-9.]+)px", re.I)
_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;{}]+)", re.I)
_DECL_RE = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;]+)")


class _Markup(HTMLParser):
    """Collect exactly the facts the rules below need — no DOM, no dependencies.

    A stdlib parse rather than a real DOM is a deliberate ceiling, and it is why every
    markup rule here is phrased over declarations VISIBLE IN THE SOURCE: the rules never
    claim to know computed style or post-JavaScript structure.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_attrs: dict[str, str] = {}
        self.title = ""
        self.metas: list[dict[str, str]] = []
        self.ids: list[str] = []
        self.label_for: set[str] = set()
        self.headings: list[tuple[int, str]] = []
        self.images: list[dict[str, str]] = []
        self.iframes: list[dict[str, str]] = []
        self.controls: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.buttons: list[dict[str, Any]] = []
        self.positive_tabindex: list[str] = []
        self.autoplay_media: list[str] = []
        self.tables: list[dict[str, Any]] = []
        self.inline_styles: list[str] = []
        self.style_blocks: list[str] = []
        self.landmarks: set[str] = set()
        self.blank_target_no_noopener: list[str] = []
        self.script_count = 0
        self.text_chars = 0
        self._text_stack: list[list[str]] = []
        self._open_textual: list[str] = []
        self._label_depth = 0
        self._in_title = False
        self._in_style = False
        self._in_script = False
        self._table_stack: list[dict[str, Any]] = []

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _as_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {k.lower(): (v if v is not None else "") for k, v in attrs}

    def _push_text(self, tag: str) -> None:
        self._open_textual.append(tag)
        self._text_stack.append([])

    def _pop_text(self, tag: str) -> str:
        """Close the innermost buffer for ``tag``, discarding buffers left open by
        unclosed inner tags — real-world markup is not balanced."""
        if tag not in self._open_textual:
            return ""
        while self._open_textual:
            open_tag = self._open_textual.pop()
            buf = self._text_stack.pop() if self._text_stack else []
            if open_tag == tag:
                return " ".join("".join(buf).split())
        return ""

    # -- HTMLParser hooks -------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        a = self._as_dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        if a.get("style"):
            self.inline_styles.append(a["style"])
        try:
            if int(a.get("tabindex") or "0") > 0:
                self.positive_tabindex.append(f"<{tag} tabindex={a.get('tabindex')}>")
        except ValueError:
            pass
        if a.get("role") in ("main", "navigation", "banner", "contentinfo", "search"):
            self.landmarks.add(a["role"])
        implicit_landmark = {
            "main": "main",
            "nav": "navigation",
            "header": "banner",
            "footer": "contentinfo",
        }.get(tag)
        if implicit_landmark:
            self.landmarks.add(implicit_landmark)

        if tag == "html":
            self.html_attrs = a
        elif tag == "title":
            self._in_title = True
        elif tag == "style":
            self._in_style = True
        elif tag == "script":
            self._in_script = True
            self.script_count += 1
        elif tag == "meta":
            self.metas.append(a)
        elif tag == "img":
            self.images.append(a)
        elif tag == "iframe":
            self.iframes.append(a)
        elif tag == "label":
            self._label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        elif tag in ("input", "select", "textarea"):
            self.controls.append({**a, "tag": tag, "in_label": self._label_depth > 0})
        elif tag == "table":
            self._table_stack.append({"th": 0, "scope": 0, "rows": 0})
        elif tag == "th" and self._table_stack:
            self._table_stack[-1]["th"] += 1
            if a.get("scope"):
                self._table_stack[-1]["scope"] += 1
        elif tag == "tr" and self._table_stack:
            self._table_stack[-1]["rows"] += 1
        elif tag in ("video", "audio") and "autoplay" in a:
            self.autoplay_media.append(f"<{tag} autoplay>")

        if tag == "a" and a.get("target", "").lower() == "_blank":
            if "noopener" not in a.get("rel", "").lower():
                self.blank_target_no_noopener.append(
                    f'<a href="{a.get("href", "")}" target=_blank>'
                )
        if tag in _TEXTUAL_TAGS:
            self._push_text(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A self-closing textual tag opens and closes in one event; routing it through
        # handle_starttag alone would leave an unbalanced text buffer behind.
        self.handle_starttag(tag, attrs)
        if tag.lower() in _TEXTUAL_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "style":
            self._in_style = False
        elif tag == "script":
            self._in_script = False
        elif tag == "label":
            self._label_depth = max(0, self._label_depth - 1)
        elif tag == "table" and self._table_stack:
            self.tables.append(self._table_stack.pop())

        if tag in _TEXTUAL_TAGS:
            text = self._pop_text(tag)
            if tag == "a":
                self.links.append({"text": text})
            elif tag == "button":
                self.buttons.append({"text": text})
            elif tag.startswith("h"):
                self.headings.append((int(tag[1]), text))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._in_style:
            self.style_blocks.append(data)
            return
        if self._in_script:
            return
        if data.strip():
            self.text_chars += len(data.strip())
        for buf in self._text_stack:
            buf.append(data)


def _control_is_labelled(ctrl: dict[str, Any], label_for: set[str]) -> bool:
    if ctrl.get("in_label"):
        return True
    if ctrl.get("id") and ctrl["id"] in label_for:
        return True
    return bool(
        ctrl.get("aria-label", "").strip()
        or ctrl.get("aria-labelledby", "").strip()
        or ctrl.get("title", "").strip()
    )


def _declared_pairs(style_text: str) -> list[tuple[tuple[int, int, int], tuple[int, int, int]]]:
    """Foreground/background colour pairs declared in the SAME declaration block.

    Same-block only. A ``color`` in one rule and a ``background`` in another may never
    meet on screen, and pairing them would manufacture contrast failures.
    """
    decls = {k.strip().lower(): v.strip() for k, v in _DECL_RE.findall(style_text)}
    fg = parse_color(decls.get("color", ""))
    bg = parse_color(decls.get("background-color", "") or decls.get("background", ""))
    return [(fg, bg)] if fg and bg else []


def _rule_bodies(css: str) -> list[str]:
    """Each ``{...}`` body in a stylesheet — the unit a colour pair must share."""
    return re.findall(r"\{([^{}]*)\}", css)


def analyze_markup(markup: str, *, url: str = "") -> tuple[list[Finding], dict[str, Any]]:
    """Static accessibility + craft findings for one HTML document.

    Every rule reads a DECLARATION in the source, so a page whose colours come from a
    stylesheet this document does not inline yields no contrast finding rather than a
    wrong one.
    """
    doc = _Markup()
    doc.feed(markup[:_MAX_MARKUP_CHARS])
    doc.close()
    out: list[Finding] = []

    # ── document level ───────────────────────────────────────────────────
    if not doc.html_attrs.get("lang", "").strip():
        out.append(
            Finding(
                id="a11y.html-lang",
                kind="a11y",
                severity="major",
                title="The document declares no language",
                detail=(
                    "<html> has no lang attribute, so a screen reader picks a voice and "
                    "pronunciation from the user's default rather than the page's language."
                ),
                fix='Add lang to the root element, e.g. <html lang="en">.',
                guideline="WCAG 2.2 3.1.1 Language of Page (A)",
            )
        )
    if not doc.title.strip():
        out.append(
            Finding(
                id="a11y.title",
                kind="a11y",
                severity="major",
                title="The page has no title",
                detail=(
                    "<title> is missing or empty. It is the first thing announced, the tab "
                    "label and the bookmark name."
                ),
                fix="Give the page a title that names the page first and the site second.",
                guideline="WCAG 2.2 2.4.2 Page Titled (A)",
            )
        )

    viewport = next((m for m in doc.metas if m.get("name", "").lower() == "viewport"), None)
    if viewport is None:
        out.append(
            Finding(
                id="a11y.viewport-missing",
                kind="a11y",
                severity="major",
                title="No viewport meta tag",
                detail=(
                    "Without a viewport declaration a mobile browser renders at a desktop "
                    "width and scales down, so every text size lands below what was designed."
                ),
                fix='Add <meta name="viewport" content="width=device-width, initial-scale=1">.',
                guideline="WCAG 2.2 1.4.10 Reflow (AA)",
            )
        )
    else:
        content = viewport.get("content", "").lower().replace(" ", "")
        max_scale = re.search(r"maximum-scale=([0-9.]+)", content)
        if "user-scalable=no" in content or (max_scale and float(max_scale.group(1)) < 2):
            out.append(
                Finding(
                    id="a11y.viewport-zoom-locked",
                    kind="a11y",
                    severity="blocker",
                    title="Pinch-zoom is disabled",
                    detail=(
                        "The viewport meta blocks or caps user scaling, which removes the one "
                        "magnification path a low-vision user has on a phone."
                    ),
                    fix="Drop user-scalable=no and any maximum-scale below 2.",
                    guideline="WCAG 2.2 1.4.4 Resize Text (AA)",
                    evidence=(_clip(viewport.get("content", "")),),
                )
            )

    if "main" not in doc.landmarks:
        out.append(
            Finding(
                id="a11y.no-main-landmark",
                kind="a11y",
                severity="minor",
                title="No main landmark",
                detail=(
                    "Nothing marks where the page content starts, so 'skip to content' has no "
                    "target and landmark navigation lands nowhere useful."
                ),
                fix='Wrap the primary content in <main> (or role="main"), exactly once.',
                guideline="WCAG 2.2 1.3.1 Info and Relationships (A)",
            )
        )

    duplicate_ids = sorted({i for i in doc.ids if doc.ids.count(i) > 1})
    if duplicate_ids:
        out.append(
            Finding(
                id="a11y.duplicate-id",
                kind="a11y",
                severity="major",
                title=f"{len(duplicate_ids)} id value(s) used more than once",
                detail=(
                    "A duplicated id breaks every reference that resolves by id — label for, "
                    "aria-labelledby, aria-describedby and in-page anchors."
                ),
                fix="Make each id unique, or switch the reference to a different mechanism.",
                guideline="WCAG 2.2 4.1.1 Parsing",
                evidence=_evidence([f"id={i}" for i in duplicate_ids]),
            )
        )

    # ── images, frames, media ────────────────────────────────────────────
    no_alt = [f'<img src="{i.get("src", "")}">' for i in doc.images if "alt" not in i]
    if no_alt:
        out.append(
            Finding(
                id="a11y.img-alt",
                kind="a11y",
                severity="blocker",
                title=f"{len(no_alt)} image(s) have no alt attribute",
                detail=(
                    "An image with no alt attribute is announced by its filename. An image "
                    'that carries no meaning still needs alt="" to be skipped.'
                ),
                fix=(
                    'Describe what the image communicates in alt, or set alt="" if it is '
                    "decorative."
                ),
                guideline="WCAG 2.2 1.1.1 Non-text Content (A)",
                evidence=_evidence(no_alt),
            )
        )
    filename_alt = [
        f'alt="{i["alt"]}"'
        for i in doc.images
        if i.get("alt", "").strip() and re.search(r"\.(png|jpe?g|gif|svg|webp)$", i["alt"], re.I)
    ]
    if filename_alt:
        out.append(
            Finding(
                id="a11y.img-alt-filename",
                kind="a11y",
                severity="major",
                title=f"{len(filename_alt)} alt text(s) are a filename",
                detail=(
                    "The alt attribute repeats the file name, which describes the asset "
                    "rather than what the reader is missing."
                ),
                fix="Replace the filename with what the image communicates in context.",
                guideline="WCAG 2.2 1.1.1 Non-text Content (A)",
                evidence=_evidence(filename_alt),
            )
        )
    untitled_frames = [
        f'<iframe src="{f.get("src", "")}">' for f in doc.iframes if not f.get("title", "").strip()
    ]
    if untitled_frames:
        out.append(
            Finding(
                id="a11y.iframe-title",
                kind="a11y",
                severity="major",
                title=f"{len(untitled_frames)} iframe(s) have no title",
                detail=(
                    "An embedded frame with no title is announced as 'frame', so the reader "
                    "cannot tell a video from an advert from a payment form."
                ),
                fix="Give every iframe a title naming what it contains.",
                guideline="WCAG 2.2 4.1.2 Name, Role, Value (A)",
                evidence=_evidence(untitled_frames),
            )
        )
    if doc.autoplay_media:
        out.append(
            Finding(
                id="a11y.autoplay",
                kind="a11y",
                severity="major",
                title="Media autoplays",
                detail=(
                    "Audio or video that starts by itself talks over a screen reader and "
                    "cannot be anticipated."
                ),
                fix="Remove autoplay, or start muted with a visible, reachable pause control.",
                guideline="WCAG 2.2 1.4.2 Audio Control (A)",
                evidence=_evidence(doc.autoplay_media),
            )
        )

    # ── forms ────────────────────────────────────────────────────────────
    unlabelled: list[str] = []
    placeholder_only: list[str] = []
    for ctrl in doc.controls:
        if ctrl["tag"] == "input":
            if (ctrl.get("type") or "text").lower() in _UNLABELLED_OK_INPUT_TYPES:
                continue
        if _control_is_labelled(ctrl, doc.label_for):
            continue
        unlabelled.append(f'<{ctrl["tag"]} name="{ctrl.get("name", "")}">')
        if ctrl.get("placeholder", "").strip():
            placeholder_only.append(f'placeholder="{ctrl["placeholder"]}"')
    if unlabelled:
        out.append(
            Finding(
                id="a11y.control-label",
                kind="a11y",
                severity="blocker",
                title=f"{len(unlabelled)} form control(s) have no label",
                detail=(
                    "The control has no <label for>, no wrapping label and no aria-label, so "
                    "it is announced only by its type."
                ),
                fix="Add a visible <label for> pointing at the control's id.",
                guideline="WCAG 2.2 3.3.2 Labels or Instructions (A)",
                evidence=_evidence(unlabelled),
            )
        )
    if placeholder_only:
        out.append(
            Finding(
                id="heuristic.placeholder-as-label",
                kind="heuristic",
                severity="major",
                title="Placeholder text is doing a label's job",
                detail=(
                    "Placeholder text disappears the moment typing starts, so the user loses "
                    "the field's name exactly when they want to check their answer."
                ),
                fix=(
                    "Keep a persistent label above the field and use the placeholder for a "
                    "format hint only."
                ),
                evidence=_evidence(placeholder_only),
            )
        )

    # ── links and buttons ────────────────────────────────────────────────
    nameless_links = [link for link in doc.links if not link["text"].strip()]
    if nameless_links:
        out.append(
            Finding(
                id="a11y.link-name",
                kind="a11y",
                severity="blocker",
                title=f"{len(nameless_links)} link(s) have no accessible name",
                detail=(
                    "A link whose only content is an icon, or an image with no alt, is "
                    "announced as its href."
                ),
                fix="Give the link visible text, or an aria-label naming the destination.",
                guideline="WCAG 2.2 2.4.4 Link Purpose (A)",
            )
        )
    vague = [link["text"] for link in doc.links if link["text"].strip().lower() in _VAGUE_LINK_TEXT]
    if vague:
        out.append(
            Finding(
                id="heuristic.link-text-vague",
                kind="heuristic",
                severity="minor",
                title=f"{len(vague)} link(s) name no destination",
                detail=(
                    "Links are read out of context in a screen-reader link list, where "
                    "'read more' three times over is three identical entries."
                ),
                fix="Move the destination into the link text: 'Read the 2026 pricing change'.",
                guideline="WCAG 2.2 2.4.4 Link Purpose (A)",
                evidence=_evidence(vague),
            )
        )
    nameless_buttons = [b for b in doc.buttons if not b["text"].strip()]
    if nameless_buttons:
        out.append(
            Finding(
                id="a11y.button-name",
                kind="a11y",
                severity="blocker",
                title=f"{len(nameless_buttons)} button(s) have no accessible name",
                detail="An icon-only <button> with no aria-label is announced as 'button'.",
                fix="Add aria-label naming the action, not the icon.",
                guideline="WCAG 2.2 4.1.2 Name, Role, Value (A)",
            )
        )
    if doc.positive_tabindex:
        out.append(
            Finding(
                id="a11y.positive-tabindex",
                kind="a11y",
                severity="major",
                title="Focus order is overridden with a positive tabindex",
                detail=(
                    "A positive tabindex pulls an element to the front of the tab order for "
                    "the whole page, so keyboard order stops matching reading order."
                ),
                fix="Use tabindex=0 and order the DOM to match the visual order.",
                guideline="WCAG 2.2 2.4.3 Focus Order (A)",
                evidence=_evidence(doc.positive_tabindex),
            )
        )
    if doc.blank_target_no_noopener:
        out.append(
            Finding(
                id="heuristic.blank-target",
                kind="heuristic",
                severity="minor",
                title="A new-window link opens without rel=noopener",
                detail=(
                    "target=_blank without rel=noopener hands the opened page a handle on "
                    "this one, and an unannounced new window also loses the back button."
                ),
                fix=(
                    'Add rel="noopener noreferrer", and say in the link text that it opens a '
                    "new tab."
                ),
                evidence=_evidence(doc.blank_target_no_noopener),
            )
        )

    # ── headings ─────────────────────────────────────────────────────────
    levels = [lvl for lvl, _ in doc.headings]
    h1s = [text for lvl, text in doc.headings if lvl == 1]
    if doc.headings and not h1s:
        out.append(
            Finding(
                id="a11y.no-h1",
                kind="a11y",
                severity="major",
                title="The page has headings but no h1",
                detail="Nothing names the page at the top of its own outline.",
                fix="Promote the page's subject to a single h1.",
                guideline="WCAG 2.2 1.3.1 Info and Relationships (A)",
            )
        )
    if len(h1s) > 1:
        out.append(
            Finding(
                id="heuristic.multiple-h1",
                kind="heuristic",
                severity="minor",
                title=f"{len(h1s)} h1 headings",
                detail="More than one h1 means the outline has more than one subject.",
                fix="Keep one h1 and demote the rest to h2.",
                evidence=_evidence(h1s),
            )
        )
    skips = [f"h{prev} → h{cur}" for prev, cur in zip(levels, levels[1:]) if cur - prev > 1]
    if skips:
        out.append(
            Finding(
                id="a11y.heading-skip",
                kind="a11y",
                severity="major",
                title=f"{len(skips)} heading level(s) skipped",
                detail=(
                    "A skipped level tells assistive tech about a section that does not "
                    "exist, and heading navigation jumps over real content."
                ),
                fix="Use the next level down; style it, don't renumber it.",
                guideline="WCAG 2.2 1.3.1 Info and Relationships (A)",
                evidence=_evidence(skips),
            )
        )
    long_headings = [text for _, text in doc.headings if len(text.split()) > 14]
    if long_headings:
        out.append(
            Finding(
                id="heuristic.heading-length",
                kind="heuristic",
                severity="minor",
                title=f"{len(long_headings)} heading(s) run longer than a sentence",
                detail=(
                    "A heading is scanned, not read. Past roughly fourteen words it stops "
                    "being a signpost and becomes body copy set large."
                ),
                fix="Cut the heading to the claim; move the qualification into the paragraph.",
                evidence=_evidence(long_headings),
            )
        )

    # ── tables ───────────────────────────────────────────────────────────
    headerless = [t for t in doc.tables if t["rows"] > 1 and t["th"] == 0]
    if headerless:
        out.append(
            Finding(
                id="a11y.table-headers",
                kind="a11y",
                severity="major",
                title=f"{len(headerless)} data table(s) have no header cells",
                detail=(
                    "Without <th> every cell is read as a bare value, so the reader has to "
                    "remember the column order."
                ),
                fix=(
                    'Mark the header row with <th scope="col"> (and row headers with '
                    'scope="row").'
                ),
                guideline="WCAG 2.2 1.3.1 Info and Relationships (A)",
            )
        )

    # ── declared colour contrast ─────────────────────────────────────────
    pairs: list[tuple[tuple[int, int, int], tuple[int, int, int], str]] = []
    for style in doc.inline_styles:
        for fg, bg in _declared_pairs(style):
            pairs.append((fg, bg, _clip(style)))
    for block in doc.style_blocks:
        for body in _rule_bodies(block):
            for fg, bg in _declared_pairs(body):
                pairs.append((fg, bg, _clip(body)))
    low = [(fg, bg, src) for fg, bg, src in pairs if contrast_ratio(fg, bg) < 4.5]
    if low:
        worst = min(low, key=lambda p: contrast_ratio(p[0], p[1]))
        out.append(
            Finding(
                id="a11y.declared-contrast",
                kind="a11y",
                severity="major",
                title=f"{len(low)} declared color pair(s) fall below 4.5:1",
                detail=(
                    "A foreground and background declared together in one rule do not reach "
                    f"the body-text minimum. Worst pair: {_hex(worst[0])} on "
                    f"{_hex(worst[1])} at {contrast_ratio(worst[0], worst[1]):.2f}:1."
                ),
                fix=(
                    "Darken the text or lighten the surface until the pair reaches 4.5:1 "
                    "(3:1 for text at 24px, or 19px bold)."
                ),
                guideline="WCAG 2.2 1.4.3 Contrast (Minimum) (AA)",
                evidence=_evidence([src for _, _, src in low]),
            )
        )

    # ── typography (craft) ───────────────────────────────────────────────
    all_css = " ".join(doc.inline_styles + doc.style_blocks)
    tiny = sorted({float(v) for v in _FONT_SIZE_RE.findall(all_css) if float(v) < 12})
    if tiny:
        out.append(
            Finding(
                id="heuristic.tiny-type",
                kind="heuristic",
                severity="major",
                title="Type is set below 12px",
                detail=(
                    "Sizes this small are read at a squint on a phone, and they are the first "
                    "thing to break when a user raises their default font size."
                ),
                fix=(
                    "Set body copy at 16px and let a relative unit carry the scale down from "
                    "there."
                ),
                evidence=_evidence([f"font-size: {v:g}px" for v in tiny]),
            )
        )
    families = {
        re.split(r",", f)[0].strip().strip("'\"").lower() for f in _FONT_FAMILY_RE.findall(all_css)
    }
    families.discard("")
    if len(families) > 3:
        out.append(
            Finding(
                id="heuristic.typeface-count",
                kind="heuristic",
                severity="minor",
                title=f"{len(families)} typefaces are declared",
                detail=(
                    "Each extra family is another set of weights, metrics and download "
                    "bytes, and the reader reads it as inconsistency."
                ),
                fix=(
                    "Get to two families — one for text, one for display — and use weight for "
                    "the rest."
                ),
                evidence=_evidence(sorted(families)),
            )
        )
    sizes = {float(v) for v in _FONT_SIZE_RE.findall(all_css)}
    if len(sizes) > 8:
        out.append(
            Finding(
                id="heuristic.type-scale-sprawl",
                kind="heuristic",
                severity="minor",
                title=f"{len(sizes)} distinct font sizes are declared",
                detail=(
                    "A type scale people can perceive has five or six steps. Beyond that, two "
                    "sizes differ without expressing a difference in rank."
                ),
                fix="Collapse to a named scale and reference its steps instead of pixel values.",
                evidence=_evidence([f"{v:g}px" for v in sorted(sizes)]),
            )
        )
    if not any(m.get("name", "").lower() == "description" for m in doc.metas):
        out.append(
            Finding(
                id="heuristic.meta-description",
                kind="heuristic",
                severity="minor",
                title="No meta description",
                detail=(
                    "Search results and link previews fall back to whatever text comes "
                    "first, which is usually navigation."
                ),
                fix="Write a one-sentence description of the page and put it in the meta tag.",
            )
        )

    summary = {
        "url": url,
        "title": " ".join(doc.title.split()),
        "lang": doc.html_attrs.get("lang", ""),
        "text_chars": doc.text_chars,
        "images": len(doc.images),
        "images_missing_alt": len(no_alt),
        "form_controls": len(doc.controls),
        "links": len(doc.links),
        "buttons": len(doc.buttons),
        "headings": [f"h{lvl}" for lvl, _ in doc.headings],
        "scripts": doc.script_count,
        "landmarks": sorted(doc.landmarks),
        "declared_color_pairs": len(pairs),
    }
    return _sorted(out), summary


# ── screenshot analysis ──────────────────────────────────────────────────────

#: Widths that mean something: the ones real devices and design files use. A capture at
#: some other width is not wrong, it just describes no real viewport.
_KNOWN_WIDTHS = (
    320,
    360,
    375,
    390,
    393,
    412,
    414,
    428,
    768,
    810,
    834,
    1024,
    1280,
    1366,
    1440,
    1512,
    1600,
    1728,
    1920,
    2560,
)
#: A colour needs this share of the canvas before it counts as part of the palette
#: rather than as anti-aliasing.
_SIGNIFICANT_SHARE = 0.005
_PALETTE_SHARE = 0.01
#: Quantisation step for the colour census, per channel. Fine on purpose: the interesting
#: contrast failure is a near-white element on white, and a coarse bucket would merge the
#: two colours whose relationship is the finding. Sprawl is counted over FAMILIES of these
#: buckets instead (see :func:`_families`), so precision here does not become noise there.
_QUANTIZE_STEP = 8
#: How far apart two census colours must be to count as different colours rather than two
#: shades of one — the grouping the palette-size and colour-vision rules reason over.
_FAMILY_DISTANCE = 40
#: How far from the dominant colour a pixel must be to count as content ("ink"). Low
#: enough that a barely-visible element still registers as present: a canvas covered in
#: near-invisible content must read as low contrast, not as empty.
_INK_TOLERANCE = 12
_MAX_PALETTE = 12
#: Colour-vision collapse is claimed only for a pair that is unmistakable to most
#: viewers…
_COLLAPSE_START_DISTANCE = 120
#: …and that loses more than this fraction of its separation under the simulation. The
#: test is a RATIO, not an absolute simulated distance: red/green pairs keep roughly half
#: their separation and blue/orange keeps nearly all of it, so the amount lost is the
#: signal and an absolute threshold would only measure how far apart the pair started.
_COLLAPSE_SURVIVING_FRACTION = 0.62


@dataclass(frozen=True)
class _Swatch:
    rgb: tuple[int, int, int]
    share: float


def _palette(image: Any) -> list[_Swatch]:
    """The colours covering the canvas, quantised per channel and ordered by share.

    Quantising at all is what keeps the census meaningful — a gradient or a JPEG artefact
    otherwise reports thousands of unique colours — but the step stays small so two
    colours whose closeness IS the finding are not merged away.
    """
    step = _QUANTIZE_STEP
    # Round to the NEAREST step rather than flooring into the bucket, so pure white and
    # pure black survive the census as themselves — a report that calls a white surface
    # #fcfcfc is quietly wrong about the one colour the reader will check.
    quantized = image.point(lambda v: min(255, round(v / step) * step))
    counts = quantized.getcolors(maxcolors=1 << 20) or []
    total = float(image.width * image.height) or 1.0
    swatches = [_Swatch(rgb=(rgb[0], rgb[1], rgb[2]), share=n / total) for n, rgb in counts]
    return sorted(swatches, key=lambda s: -s.share)


def _families(swatches: list[_Swatch]) -> list[_Swatch]:
    """Collapse shades of one colour into a single representative, share summed.

    "How many colours does this screen use" is a question about colour FAMILIES: a card
    surface and its hover state are one decision, not two. Greedy from the heaviest swatch
    down, so the representative of each family is the shade that actually dominates it.
    """
    out: list[_Swatch] = []
    for swatch in swatches:
        for i, seen in enumerate(out):
            if _rgb_distance(swatch.rgb, seen.rgb) <= _FAMILY_DISTANCE:
                out[i] = _Swatch(rgb=seen.rgb, share=seen.share + swatch.share)
                break
        else:
            out.append(swatch)
    return sorted(out, key=lambda s: -s.share)


def _ink_mask(image: Any, background: tuple[int, int, int]) -> Any:
    """A 0/255 mask of the pixels that differ from the background — the page's content.

    Kept in ``L`` rather than ``1`` so the inked-pixel count comes off ``histogram()``
    instead of walking every pixel in Python.
    """
    from PIL import Image, ImageChops

    flat = Image.new("RGB", image.size, background)
    diff = ImageChops.difference(image, flat).convert("L")
    return diff.point(lambda v: 255 if v > _INK_TOLERANCE else 0)


def _left_edges(mask: Any) -> list[int]:
    """The column each inked row starts at — the raw material for alignment discipline."""
    width, height = mask.size
    pixels = mask.load()
    edges: list[int] = []
    for y in range(height):
        for x in range(width):
            if pixels[x, y]:
                edges.append(x)
                break
    return edges


def analyze_image(path: Path) -> tuple[list[Finding], dict[str, Any]]:
    """Pixel-level craft + accessibility findings for one screenshot.

    Raises :class:`DesignCritiqueError` for anything the caller must fix (missing file, not
    an image, Pillow absent) so the tool returns an actionable refusal instead of an empty
    report that reads like a pass.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover — Pillow ships with this app
        raise DesignCritiqueError(
            "Pillow is not importable, so the screenshot cannot be decoded.",
            recovery_hints=[
                "Reinstall Design Critique from the Store — Pillow ships with this app, "
                "not with PersonalClaw itself.",
                "Restart the gateway after the reinstall (the install reports "
                "restart_required); the URL review works in the meantime.",
            ],
        ) from exc

    if not path.exists():
        raise DesignCritiqueError(
            f"No file at {path} — the screenshot path does not resolve to anything on disk.",
            recovery_hints=[
                "Check the path, or use `list_dir`/`glob` to find the capture you meant.",
            ],
        )
    if not path.is_file():
        raise DesignCritiqueError(
            f"{path} is not a file — a directory or device node cannot be decoded as an image.",
            recovery_hints=["Pass the path of a single PNG/JPEG/WebP screenshot."],
        )
    size_bytes = path.stat().st_size
    if size_bytes > _MAX_IMAGE_BYTES:
        raise DesignCritiqueError(
            f"{path.name} is {size_bytes / 1e6:.0f} MB; images above "
            f"{_MAX_IMAGE_BYTES / 1e6:.0f} MB are refused before decoding.",
            recovery_hints=[
                "Export the capture at screen resolution rather than at print scale.",
            ],
        )
    try:
        with Image.open(path) as opened:
            width, height = opened.size
            # The pixel count is known from the header, so a decompression bomb is refused
            # before a single pixel is decoded.
            if width * height > _MAX_IMAGE_PIXELS:
                raise DesignCritiqueError(
                    f"{path.name} declares {width}×{height} pixels — past the decode "
                    "ceiling this tool will spend memory on.",
                    recovery_hints=[
                        "Downscale the capture (2× screen resolution is plenty) and retry.",
                    ],
                )
            rgb = opened.convert("RGB")
    except DesignCritiqueError:
        raise
    except Exception as exc:
        raise DesignCritiqueError(
            f"{path.name} could not be decoded as an image ({exc}).",
            recovery_hints=[
                "Confirm the file is a PNG/JPEG/WebP capture and not, say, a PDF or an SVG.",
            ],
        ) from exc

    stats = rgb.copy()
    stats.thumbnail((_STAT_MAX_EDGE, _STAT_MAX_EDGE))
    swatches = _palette(stats)
    background = swatches[0].rgb if swatches else (255, 255, 255)
    significant = [s for s in swatches if s.share >= _SIGNIFICANT_SHARE]
    palette = [s for s in swatches if s.share >= _PALETTE_SHARE]
    # Shade-level for the contrast question ("is anything on this surface too faint"),
    # family-level for the questions about how many decisions the screen makes.
    families = [s for s in _families(significant) if s.share >= _PALETTE_SHARE]
    mask = _ink_mask(stats, background)
    bbox = mask.getbbox()
    ink_pixels = mask.histogram()[255]
    ink_share = ink_pixels / float(stats.width * stats.height or 1)
    #: A capture much taller than it is wide is a whole-page scroll, not one viewport, so
    #: the checks that reason about a single screen's composition are skipped for it.
    tall_capture = height > 3 * width

    out: list[Finding] = []

    # ── canvas ───────────────────────────────────────────────────────────
    if width < 320:
        out.append(
            Finding(
                id="heuristic.canvas-narrow",
                kind="heuristic",
                severity="minor",
                title=f"The capture is only {width}px wide",
                detail=(
                    "That is narrower than any mainstream phone, so the layout under review "
                    "is not one a user will see."
                ),
                fix="Recapture at 390px (a current phone) or at your smallest supported width.",
            )
        )
    elif not any(abs(width - known) <= 8 for known in _KNOWN_WIDTHS):
        out.append(
            Finding(
                id="heuristic.canvas-offgrid",
                kind="heuristic",
                severity="minor",
                title=f"{width}px is not a device or breakpoint width",
                detail=(
                    "A window sized by hand hides which breakpoint is under review, so a "
                    "layout defect cannot be reproduced from the screenshot alone."
                ),
                fix="Recapture at one of your declared breakpoints and note which one.",
                evidence=(f"{width}×{height}",),
            )
        )

    # ── contrast ─────────────────────────────────────────────────────────
    faint = [s for s in palette[1:] if contrast_ratio(s.rgb, background) < 3.0 and s.share < 0.4]
    if faint:
        worst = min(faint, key=lambda s: contrast_ratio(s.rgb, background))
        out.append(
            Finding(
                id="a11y.rendered-contrast",
                kind="a11y",
                severity="major",
                title=f"{len(faint)} rendered color(s) sit under 3:1 against the background",
                detail=(
                    f"Against the dominant {_hex(background)} surface these read as barely "
                    f"there — the faintest is {_hex(worst.rgb)} at "
                    f"{contrast_ratio(worst.rgb, background):.2f}:1. Text or an icon in them "
                    "fails the minimum, and a UI border in them disappears entirely."
                ),
                fix=(
                    "Take text to 4.5:1 and non-text boundaries to 3:1 against the surface "
                    "they sit on."
                ),
                guideline="WCAG 2.2 1.4.3 Contrast (Minimum) / 1.4.11 Non-text Contrast (AA)",
                evidence=_evidence(
                    [
                        f"{_hex(s.rgb)} at {contrast_ratio(s.rgb, background):.2f}:1 "
                        f"({s.share * 100:.1f}% of canvas)"
                        for s in sorted(faint, key=lambda s: contrast_ratio(s.rgb, background))
                    ]
                ),
            )
        )
    near_white = _rgb_distance(background, (255, 255, 255)) <= _QUANTIZE_STEP
    if near_white and any(_rgb_distance(s.rgb, (0, 0, 0)) <= _QUANTIZE_STEP for s in palette):
        out.append(
            Finding(
                id="heuristic.pure-black-on-white",
                kind="heuristic",
                severity="minor",
                title="Pure black on pure white",
                detail=(
                    "21:1 is more contrast than paper gives, and it makes long-form reading "
                    "harder rather than easier — the halation is why print uses ink on "
                    "off-white."
                ),
                fix=(
                    "Take the text to a near-black and the surface to a near-white; keep the "
                    "pair above 12:1."
                ),
            )
        )

    # ── palette ──────────────────────────────────────────────────────────
    if len(families) > _MAX_PALETTE:
        out.append(
            Finding(
                id="heuristic.palette-sprawl",
                kind="heuristic",
                severity="major",
                title=f"{len(families)} distinct colors each cover at least 1% of the canvas",
                detail=(
                    "Past about a dozen, color has stopped carrying meaning: nothing is "
                    "emphasised because everything is."
                ),
                fix=(
                    "Pick one accent, one or two neutrals and a semantic set, and delete the "
                    "rest."
                ),
                evidence=_evidence([f"{_hex(s.rgb)} ({s.share * 100:.1f}%)" for s in families]),
            )
        )
    saturated = []
    for swatch in families:
        _, sat, val = colorsys.rgb_to_hsv(*(c / 255 for c in swatch.rgb))
        if sat > 0.9 and val > 0.9:
            saturated.append(swatch)
    if saturated:
        out.append(
            Finding(
                id="heuristic.oversaturated",
                kind="heuristic",
                severity="minor",
                title=f"{len(saturated)} fully saturated color(s) in the palette",
                detail=(
                    "Maximum saturation at maximum brightness vibrates against neighbouring "
                    "color and leaves nothing louder for a genuine alert."
                ),
                fix=(
                    "Pull saturation back toward 70–85% and reserve the loudest value for one "
                    "state."
                ),
                evidence=_evidence([_hex(s.rgb) for s in saturated]),
            )
        )
    collapsing: list[str] = []
    for i, a in enumerate(families):
        for b in families[i + 1 :]:
            normal = _rgb_distance(a.rgb, b.rgb)
            if normal < _COLLAPSE_START_DISTANCE:
                continue
            simulated = _rgb_distance(simulate_deuteranopia(a.rgb), simulate_deuteranopia(b.rgb))
            if simulated < _COLLAPSE_SURVIVING_FRACTION * normal:
                collapsing.append(
                    f"{_hex(a.rgb)} vs {_hex(b.rgb)} keeps {simulated / normal * 100:.0f}% "
                    "of its separation"
                )
    if collapsing:
        out.append(
            Finding(
                id="a11y.color-vision-collapse",
                kind="a11y",
                severity="major",
                title=f"{len(collapsing)} palette pair(s) collapse for red-green color blindness",
                detail=(
                    "These colors are clearly different to most viewers and nearly identical "
                    "to the roughly 8% of men with deuteranomaly. If either one encodes "
                    "meaning — pass/fail, series, status — that meaning is lost."
                ),
                fix="Separate the pair in lightness as well as hue, and add a shape or a label.",
                guideline="WCAG 2.2 1.4.1 Use of Color (A)",
                evidence=_evidence(collapsing),
            )
        )

    # ── space and alignment ──────────────────────────────────────────────
    if ink_share < 0.04:
        out.append(
            Finding(
                id="heuristic.near-empty",
                kind="heuristic",
                severity="minor",
                title=f"Only {ink_share * 100:.1f}% of the canvas differs from the background",
                detail=(
                    "Either the capture caught a loading or empty state, or the screen is so "
                    "sparse the user has to hunt for what it wants from them."
                ),
                fix="Recapture with real content, or give the screen a clear primary action.",
            )
        )
    elif ink_share > 0.6:
        out.append(
            Finding(
                id="heuristic.dense",
                kind="heuristic",
                severity="major",
                title=f"{ink_share * 100:.0f}% of the canvas is content",
                detail=(
                    "With this little breathing room the eye gets no grouping cues, so "
                    "related things stop reading as related."
                ),
                fix="Add space between groups before adding rules or boxes between them.",
            )
        )
    if bbox and not tall_capture:
        left, _top, right, _bottom = bbox
        margin_left, margin_right = left, stats.width - right
        larger, smaller = max(margin_left, margin_right), min(margin_left, margin_right)
        if larger >= 12 and larger > smaller * 2.2:
            out.append(
                Finding(
                    id="heuristic.margin-imbalance",
                    kind="heuristic",
                    severity="minor",
                    title="The content block is not optically centred",
                    detail=(
                        "The left and right gutters differ by more than a factor of two, "
                        "which reads as a mistake rather than as a choice "
                        f"({margin_left}px vs {margin_right}px on the analysed "
                        f"{stats.width}px width)."
                    ),
                    fix="Centre the container, or make the asymmetry large and deliberate.",
                )
            )
        edges = _left_edges(mask)
        clusters: list[int] = []
        for edge in sorted(edges):
            if not clusters or edge - clusters[-1] > 6:
                clusters.append(edge)
        row_threshold = max(2, int(0.02 * len(edges)))
        strong = [c for c in clusters if sum(1 for e in edges if abs(e - c) <= 6) >= row_threshold]
        if len(strong) > 6:
            out.append(
                Finding(
                    id="heuristic.alignment-sprawl",
                    kind="heuristic",
                    severity="major",
                    title=f"{len(strong)} distinct left edges",
                    detail=(
                        "Content starts at that many different x positions, so there is no "
                        "column the eye can follow down the page."
                    ),
                    fix=(
                        "Snap every block to a shared grid; indentation should mean hierarchy, "
                        "not chance."
                    ),
                    evidence=_evidence([f"x={c}" for c in strong]),
                )
            )

    summary = {
        "path": str(path),
        "width": width,
        "height": height,
        "bytes": size_bytes,
        "aspect": round(width / height, 3) if height else 0,
        "full_page_capture": tall_capture,
        "background": _hex(background),
        "palette": [f"{_hex(s.rgb)} {s.share * 100:.1f}%" for s in families],
        "shades": len(palette),
        "significant_colors": len(significant),
        "content_share": round(ink_share, 4),
        "content_bbox": list(bbox) if bbox else [],
        "analysed_at": f"{stats.width}x{stats.height}",
    }
    return _sorted(out), summary


# ── the review rubric (the vision half) ──────────────────────────────────────

_RUBRIC: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "screenshot": (
        "one screen, judged as a first impression",
        (
            (
                "One-glance job",
                "Cover everything but the top third. Can you still say what this screen is "
                "for and what it wants next? If not, the hierarchy is decorative.",
            ),
            (
                "Rank matches importance",
                "List what is visually loudest, in order. Compare that with what actually "
                "matters to the user here. Every mismatch is a defect.",
            ),
            (
                "One primary action",
                "Count the things styled as primary. More than one means the screen has no "
                "opinion about what to do.",
            ),
            (
                "Affordance honesty",
                "Does everything that looks clickable act, and does everything that acts look "
                "clickable? Name the ones that lie.",
            ),
            (
                "Alignment and rhythm",
                "Squint at it. Do edges line up and does the spacing repeat, or does each "
                "block invent its own padding?",
            ),
            (
                "Copy earning its space",
                "Read every label aloud. Cut the words that survive removal. Flag any label "
                "that names the system's concept rather than the user's.",
            ),
        ),
    ),
    "flow": (
        "a sequence of screens, judged as a path",
        (
            (
                "Where am I, what next",
                "At every step, are the current position and the next action obvious without "
                "reading the whole screen?",
            ),
            (
                "Step count honesty",
                "Count the steps and the fields. Which could be inferred, defaulted or "
                "deferred? Each one removed is the cheapest win available.",
            ),
            (
                "Reversibility",
                "Can each step be undone or corrected in place, or does a mistake mean "
                "starting over?",
            ),
            (
                "Continuity",
                "Does the entered data — and the vocabulary — carry forward unchanged between "
                "steps? A renamed concept mid-flow reads as a different product.",
            ),
            (
                "The unhappy path",
                "Walk it with a validation error, an empty result and a dropped connection. A "
                "flow reviewed only on its happy path has not been reviewed.",
            ),
        ),
    ),
    "page": (
        "a live page, judged as a whole",
        (
            (
                "The promise above the fold",
                "Does the first viewport say what this is, for whom, and what to do? Vague "
                "here costs everything below.",
            ),
            (
                "Scan path",
                "Follow the headings alone. Do they tell the story on their own? If they "
                "don't, no one will read the paragraphs.",
            ),
            (
                "Keyboard pass",
                "Tab through it. Is focus always visible, does the order match the layout, and "
                "can every control be reached and left?",
            ),
            (
                "Responsive pass",
                "Narrow it to 390px. What overflows, truncates, or reflows into nonsense?",
            ),
            (
                "Motion and restraint",
                "Does anything move that isn't communicating a change? Does it still work with "
                "reduced motion?",
            ),
            (
                "States",
                "Find the empty, loading, error and too-much-data states. An unstyled state is "
                "a shipped defect.",
            ),
        ),
    ),
}


# ── errors ───────────────────────────────────────────────────────────────────


class DesignCritiqueError(Exception):
    """A refusal the caller can act on — one plain sentence, plus recovery hints."""

    def __init__(self, message: str, recovery_hints: list[str] | None = None) -> None:
        super().__init__(message)
        self.recovery_hints = list(recovery_hints or [])


# ── fetch seams ──────────────────────────────────────────────────────────────
# Both network reads live in module-level functions so the whole analysis path can be
# exercised offline by substituting them — the same seam shape the other bundled apps use
# for their one unmockable dependency.


async def _fetch_markup(url: str, *, timeout_s: int) -> tuple[str, str, int]:
    """``(final_url, html, status)`` through core's guarded egress chokepoint."""
    from personalclaw.sdk.net import CONNECTOR, egress_policy_for, fetch

    policy = egress_policy_for(CONNECTOR)
    if timeout_s > 0:
        policy = replace(policy, timeout_s=float(timeout_s))
    response = await fetch(url, policy=policy)
    return response.url or url, response.text, response.status


async def _fetch_rendered(url: str) -> tuple[bool, str, str]:
    """``(ok, rendered_text, note)`` from the shipped headless-render path."""
    from personalclaw.sdk.net import web_fetch

    outcome = await web_fetch(url, render=True, require_provenance=False)
    if not outcome.ok:
        return False, "", outcome.error or "the rendered fetch did not complete"
    return True, outcome.content or "", outcome.extractor or ""


# ── rendering ────────────────────────────────────────────────────────────────

_KIND_LABEL = {"a11y": "Accessibility", "heuristic": "Craft"}


def render_report(
    *, heading: str, findings: list[Finding], summary: dict[str, Any], notes: list[str]
) -> str:
    lines = [heading, ""]
    for note in notes:
        lines.append(f"NOTE: {note}")
    if notes:
        lines.append("")
    if not findings:
        lines.append(
            "No findings from the checks this tool can measure. That is not a pass — run "
            "`design_critique_rubric` and do the visual pass it describes."
        )
    else:
        counts = {sev: sum(1 for f in findings if f.severity == sev) for sev in _SEVERITY_ORDER}
        lines.append(
            f"{len(findings)} finding(s): "
            + ", ".join(f"{n} {sev}" for sev, n in counts.items() if n)
        )
        lines.append("")
        for finding in findings:
            label = _KIND_LABEL.get(finding.kind, finding.kind)
            lines.append(f"[{finding.severity.upper()}] {label} · {finding.id}")
            lines.append(f"  {finding.title}")
            lines.append(f"  Why: {finding.detail}")
            if finding.guideline:
                lines.append(f"  Guideline: {finding.guideline}")
            for item in finding.evidence:
                lines.append(f"  Evidence: {item}")
            lines.append(f"  Fix: {finding.fix}")
            lines.append("")
    lines.append("Measured context:")
    for key, value in summary.items():
        if value in ("", [], {}, None):
            continue
        rendered = ", ".join(str(v) for v in value[:12]) if isinstance(value, list) else value
        lines.append(f"  {key}: {rendered}")
    lines.append("")
    lines.append(
        "The visual half of the review is NOT in this report — call "
        "`design_critique_rubric` and answer it against the target yourself."
    )
    return "\n".join(lines)


def _clamp_findings(findings: list[Finding], limit: int) -> tuple[list[Finding], list[str]]:
    if len(findings) <= limit:
        return findings, []
    dropped = len(findings) - limit
    return findings[:limit], [
        f"{dropped} lower-severity finding(s) withheld to stay inside max_findings={limit} — "
        "clear these and re-run, or raise max_findings."
    ]


def _limit(args: dict[str, Any]) -> int:
    raw = args.get("max_findings")
    try:
        value = int(raw) if raw is not None else _DEFAULT_MAX_FINDINGS
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_FINDINGS
    return max(1, min(_MAX_MAX_FINDINGS, value))


# ── the provider ─────────────────────────────────────────────────────────────


class DesignCritiqueProvider(ToolProvider):
    """Serve the design-critique surface: a page, a screenshot, and the vision rubric."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        try:
            self._timeout = int(self._config.get("timeout_secs", 20))
        except (TypeError, ValueError):
            self._timeout = 20

    @property
    def name(self) -> str:
        return "design-critique"

    @property
    def display_name(self) -> str:
        return "Design Critique"

    async def list_tools(self) -> list[ToolDefinition]:
        # STATIC — independent of Pillow importing or a network existing, so the tool
        # surface never varies with the machine the app mounted on.
        kinds_param = {
            "type": "string",
            "enum": ["all", "a11y", "heuristic"],
            "description": (
                "Narrow the report to accessibility findings or craft findings. Default all."
            ),
        }
        max_findings_param = {
            "type": "integer",
            "description": (
                f"Cap the report, highest severity first (default {_DEFAULT_MAX_FINDINGS}, "
                f"max {_MAX_MAX_FINDINGS})."
            ),
        }
        return [
            ToolDefinition(
                name="design_critique_page",
                description=(
                    "Review a live URL and return structured accessibility and craft findings "
                    "— label association, alt text, heading order, focus order, declared "
                    "color contrast, type scale — each with its evidence and the fix. Reads "
                    "the static markup through the guarded fetch and, unless render=false, "
                    "also reports what the headless renderer sees. Use it before asking a "
                    "person to look at a page."
                ),
                provider="design-critique",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The http(s) URL to review."},
                        "render": {
                            "type": "boolean",
                            "description": (
                                "Also fetch through the headless renderer to report what a "
                                "browser actually shows (default true). Set false to skip the "
                                "second, slower fetch."
                            ),
                        },
                        "kinds": kinds_param,
                        "max_findings": max_findings_param,
                    },
                    "required": ["url"],
                },
                requires_approval=False,
                # Reaches the network (through core's egress guard), so not SAFE.
                risk_level=RiskLevel.CAUTION,
                max_output=_MAX_OUTPUT_CHARS,
            ),
            ToolDefinition(
                name="design_critique_image",
                description=(
                    "Review a screenshot on disk and return structured accessibility and "
                    "craft findings measured from the pixels — rendered contrast, palette "
                    "sprawl, red-green color-vision collapse, density, margin balance, "
                    "alignment discipline — each with its evidence and the fix. Pair it with "
                    "your own look at the image: this measures, it does not see."
                ),
                provider="design-critique",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to a PNG/JPEG/WebP screenshot.",
                        },
                        "kinds": kinds_param,
                        "max_findings": max_findings_param,
                    },
                    "required": ["path"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=_MAX_OUTPUT_CHARS,
            ),
            ToolDefinition(
                name="design_critique_rubric",
                description=(
                    "The review checklist for the half of a design critique no measurement can "
                    "answer — hierarchy, one-glance comprehension, affordance honesty, copy, "
                    "states, the unhappy path. Call it alongside design_critique_page or "
                    "design_critique_image and answer it against the target yourself, so the "
                    "visual pass is the same procedure every time."
                ),
                provider="design-critique",
                parameters={
                    "type": "object",
                    "properties": {
                        "surface": {
                            "type": "string",
                            "enum": ["screenshot", "flow", "page"],
                            "description": (
                                "Which rubric to return. Default screenshot (a single screen)."
                            ),
                        }
                    },
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=_MAX_OUTPUT_CHARS,
            ),
        ]

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        args = dict(arguments or {})
        try:
            if tool_name == "design_critique_page":
                return await self._page(args)
            if tool_name == "design_critique_image":
                return self._image(args)
            if tool_name == "design_critique_rubric":
                return self._rubric(args)
        except DesignCritiqueError as exc:
            return ToolResult(success=False, error=str(exc), recovery_hints=exc.recovery_hints)
        except Exception as exc:  # a broken review must never take the turn down
            logger.exception("design-critique: %s failed", tool_name)
            return ToolResult(
                success=False,
                error=f"{tool_name} could not complete: {type(exc).__name__}: {exc}",
                recovery_hints=[
                    "Retry with a simpler target; if it repeats, review by hand with "
                    "design_critique_rubric.",
                ],
            )
        return ToolResult(
            success=False,
            error=f"Unknown tool: {tool_name}",
            recovery_hints=[
                "This provider exposes: design_critique_page, design_critique_image, "
                "design_critique_rubric.",
            ],
        )

    # -- tools ------------------------------------------------------------
    async def _page(self, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url:
            raise DesignCritiqueError(
                "design_critique_page needs a 'url' — there is nothing to review without one.",
                recovery_hints=[
                    "Pass the http(s) URL of the page, e.g. https://example.com/pricing.",
                ],
            )
        if not re.match(r"^https?://", url, re.I):
            raise DesignCritiqueError(
                f"{url!r} is not an http(s) URL — only http and https are fetchable "
                "through the guarded egress path.",
                recovery_hints=[
                    "Pass a full https:// URL, or use design_critique_image for a local "
                    "capture.",
                ],
            )
        try:
            final_url, markup, status = await _fetch_markup(url, timeout_s=self._timeout)
        except Exception as exc:
            raise DesignCritiqueError(
                f"{url} could not be fetched: {type(exc).__name__}: {exc}",
                recovery_hints=[
                    "Check the URL, and check Settings → Security → Network if the host is "
                    "private or denied.",
                    "For a page you can already see, screenshot it and use "
                    "design_critique_image.",
                ],
            ) from exc
        if status >= 400:
            raise DesignCritiqueError(
                f"{final_url} answered HTTP {status} — an error page is not the design "
                "under review.",
                recovery_hints=[
                    "Check the URL, and any auth it needs, before reviewing it.",
                ],
            )

        findings, summary = analyze_markup(markup, url=final_url)
        summary["http_status"] = status
        summary["markup_bytes"] = len(markup)
        notes = [
            "Markup checks read the STATIC HTML only — computed style, and anything the "
            "browser builds after load, are outside what this can see."
        ]

        if args.get("render", True):
            ok, rendered, note = await _fetch_rendered(url)
            if ok:
                summary["rendered_chars"] = len(rendered)
                summary["renderer"] = note
                # A static document with almost no text whose rendered form has plenty is the
                # tell for a client-rendered shell. Say so, because it bounds every markup
                # finding above.
                if summary["text_chars"] < 200 and len(rendered) > 600:
                    findings = _sorted(
                        findings
                        + [
                            Finding(
                                id="heuristic.client-rendered-shell",
                                kind="heuristic",
                                severity="major",
                                title="The page's content only exists after JavaScript runs",
                                detail=(
                                    f"The static HTML carries {summary['text_chars']} "
                                    f"characters of text; the rendered page carries "
                                    f"{len(rendered)}. Search engines, link previews and "
                                    "reader modes see the near-empty version — and so did "
                                    "the markup checks above."
                                ),
                                fix=(
                                    "Server-render or pre-render the content that matters, "
                                    "then re-run this review for real markup findings."
                                ),
                            )
                        ]
                    )
            else:
                notes.append(
                    f"The headless render did not complete ({note}), so this report covers "
                    "static markup only."
                )
        else:
            notes.append("render=false: the headless-render pass was skipped.")

        return self._report(
            heading=f"Design critique — {final_url}",
            response_type=RESPONSE_TYPE_PAGE,
            findings=findings,
            summary=summary,
            notes=notes,
            args=args,
            target=final_url,
        )

    def _image(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("path") or "").strip()
        if not raw:
            raise DesignCritiqueError(
                "design_critique_image needs a 'path' — there is nothing to review "
                "without one.",
                recovery_hints=[
                    "Pass the path of a screenshot, e.g. ~/Desktop/checkout.png.",
                ],
            )
        path = Path(raw).expanduser()
        findings, summary = analyze_image(path)
        notes = [
            "Measured from pixels: this reports contrast, palette, density and alignment. It "
            "cannot read the screen's intent — do that pass with design_critique_rubric."
        ]
        if summary["full_page_capture"]:
            notes.append(
                "This is a tall full-page capture, so the composition checks that assume one "
                "viewport were skipped; recapture a single screen for those."
            )
        return self._report(
            heading=f"Design critique — {path.name}",
            response_type=RESPONSE_TYPE_IMAGE,
            findings=findings,
            summary=summary,
            notes=notes,
            args=args,
            target=str(path),
        )

    def _rubric(self, args: dict[str, Any]) -> ToolResult:
        surface = str(args.get("surface") or "screenshot").strip().lower()
        if surface not in _RUBRIC:
            return ToolResult(
                success=False,
                error=(
                    f"Unknown surface {surface!r}. Choose one of: "
                    + ", ".join(sorted(_RUBRIC))
                    + "."
                ),
            )
        subtitle, items = _RUBRIC[surface]
        lines = [
            f"Design-critique rubric — {subtitle}",
            "",
            "Answer each of these against the target yourself and write the answer down. An "
            "unanswered item is an unreviewed screen, not a passing one.",
            "",
        ]
        for index, (name, prompt) in enumerate(items, start=1):
            lines.append(f"{index}. {name}")
            lines.append(f"   {prompt}")
        lines.append("")
        lines.append(
            "Then run design_critique_page (a URL) or design_critique_image (a capture) for "
            "the measurable half — contrast, labels, heading order, palette, alignment."
        )
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={
                "response_type": RESPONSE_TYPE_RUBRIC,
                "surface": surface,
                "items": [{"name": name, "prompt": prompt} for name, prompt in items],
            },
        )

    # -- shared -----------------------------------------------------------
    def _report(
        self,
        *,
        heading: str,
        response_type: str,
        findings: list[Finding],
        summary: dict[str, Any],
        notes: list[str],
        args: dict[str, Any],
        target: str,
    ) -> ToolResult:
        kinds = str(args.get("kinds") or "all").strip().lower()
        if kinds in ("a11y", "heuristic"):
            findings = [f for f in findings if f.kind == kinds]
        elif kinds != "all":
            return ToolResult(
                success=False,
                error=f"Unknown kinds {kinds!r} — use 'all', 'a11y' or 'heuristic'.",
            )
        kept, clamp_notes = _clamp_findings(findings, _limit(args))
        return ToolResult(
            success=True,
            output=render_report(
                heading=heading, findings=kept, summary=summary, notes=notes + clamp_notes
            ),
            metadata={
                "response_type": response_type,
                "target": target,
                "findings": [f.as_dict() for f in kept],
                "counts": {
                    "matched": len(findings),
                    "reported": len(kept),
                    "a11y": sum(1 for f in kept if f.kind == "a11y"),
                    "heuristic": sum(1 for f in kept if f.kind == "heuristic"),
                    **{
                        severity: sum(1 for f in kept if f.severity == severity)
                        for severity in _SEVERITY_ORDER
                    },
                },
                "measured": summary,
            },
        )


def create_provider(config: dict[str, Any] | None = None) -> ToolProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return DesignCritiqueProvider(config)
