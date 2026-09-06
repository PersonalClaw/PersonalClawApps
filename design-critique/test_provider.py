"""Tests for the design-critique tool provider and its two analysis engines.

Everything here runs with no network, no credentials and no gateway. The page path's two
fetches live behind module-level seams (``_fetch_markup`` / ``_fetch_rendered``) precisely
so the whole pipeline — parse, rules, rendered-shell detection, report — is exercised
against markup the test controls; the screenshot path runs against images the test draws.
That is deliberate: a test that needed a live URL could only ever tell you the app worked
once.

Contract: personalclaw.sdk.tool:ToolProvider
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from personalclaw.sdk.manifest import AppManifest

from provider import (
    RESPONSE_TYPE_IMAGE,
    RESPONSE_TYPE_PAGE,
    RESPONSE_TYPE_RUBRIC,
    DesignCritiqueError,
    DesignCritiqueProvider,
    analyze_image,
    analyze_markup,
    contrast_ratio,
    create_provider,
    parse_color,
    simulate_deuteranopia,
)
import provider as mod

HERE = Path(__file__).parent

CONTRACT_METHODS = ("display_name", "invoke", "list_tools", "name")
EXPECTED_TOOLS = {"design_critique_page", "design_critique_image", "design_critique_rubric"}


@pytest.fixture()
def app():
    return create_provider({})


def _ids(findings) -> set[str]:
    return {f["id"] for f in findings}


def _canvas(size, color=(255, 255, 255)) -> Image.Image:
    return Image.new("RGB", size, color)


# ── the provider contract ────────────────────────────────────────────────────


def test_factory_returns_the_provider() -> None:
    assert isinstance(create_provider({}), DesignCritiqueProvider)


def test_factory_accepts_no_config() -> None:
    assert isinstance(create_provider(None), DesignCritiqueProvider)


def test_nothing_abstract_is_left() -> None:
    """An unimplemented abstract method makes the provider uninstantiable."""
    assert not getattr(DesignCritiqueProvider, "__abstractmethods__", frozenset())


def test_registers_under_the_app_name() -> None:
    """Every per-type registry keys a provider by `.name`."""
    assert create_provider({}).name == "design-critique"


def test_declares_its_display_name() -> None:
    assert create_provider({}).display_name == "Design Critique"


def test_every_contract_method_is_declared_on_the_provider() -> None:
    """Inherited-but-unimplemented is the drift this catches."""
    for name in CONTRACT_METHODS:
        assert name in vars(DesignCritiqueProvider), f"{name} is not implemented"


def test_settings_reach_the_provider() -> None:
    assert create_provider({"timeout_secs": 5})._timeout == 5


def test_a_junk_timeout_setting_does_not_break_construction() -> None:
    """Settings arrive from a JSON blob a user can edit, so a bad value must degrade."""
    assert create_provider({"timeout_secs": "soon"})._timeout == 20


def test_list_tools_is_static_and_complete(app) -> None:
    """The definitions must not be derived from the environment: a host advertising a tool
    surface that varies by machine is a surface nobody can rely on."""
    tools = asyncio.run(app.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.provider == "design-critique"
        assert tool.description.strip()
        assert tool.parameters.get("type") == "object"
        assert tool.requires_approval is False


def test_unknown_tool_is_refused_not_raised(app) -> None:
    result = asyncio.run(app.invoke("design_critique_everything", {}))
    assert not result.success
    assert "design_critique_everything" in result.error


# ── the manifest ─────────────────────────────────────────────────────────────


def test_the_manifest_parses_against_cores_own_reader():
    manifest = AppManifest.from_json_file(HERE / "app.json")
    assert manifest.validate() == []
    assert manifest.name == "design-critique"
    assert manifest.provider is not None
    assert manifest.provider.type == "tool"
    assert manifest.provider.implementation == "provider:create_provider"


def test_the_manifest_declares_the_posture_the_code_actually_has():
    """`network: true` because the page tool fetches; `storage: false` because nothing in
    the bundle writes — a declaration that drifts from the code is worse than none."""
    manifest = AppManifest.from_json_file(HERE / "app.json")
    assert manifest.permissions.network is True
    assert manifest.permissions.storage is False
    sources = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(HERE.glob("*.py"))
        if p.name != "test_provider.py"
    )
    for writer in (".write_text(", ".write_bytes(", ".mkdir(", "shutil.", "os.remove"):
        assert writer not in sources, f"{writer} appeared — storage:false is no longer true"
    # The only open() in the bundle is Pillow decoding the file the caller named.
    assert set(re.findall(r"[\w.]*open\(", sources)) <= {"Image.open("}


def test_the_bundle_ships_the_app_creation_contract_artifacts():
    for name in ("app.json", "provider.py", "app_cli.py", "test_provider.py", "README.md", "LICENSE"):
        assert (HERE / name).is_file(), f"{name} is missing from the bundle"


# ── colour maths ─────────────────────────────────────────────────────────────


def test_contrast_ratio_matches_the_wcag_anchors():
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio((255, 255, 255), (255, 255, 255)) == pytest.approx(1.0)
    # #767676 on white is the canonical "exactly passes 4.5:1" grey.
    assert contrast_ratio((0x76, 0x76, 0x76), (255, 255, 255)) == pytest.approx(4.54, abs=0.05)


@pytest.mark.parametrize(
    "token,expected",
    [
        ("#fff", (255, 255, 255)),
        ("#ff8800", (255, 136, 0)),
        ("rgb(10, 20, 30)", (10, 20, 30)),
        ("rgba(10,20,30,0.5)", (10, 20, 30)),
        ("whitesmoke", (245, 245, 245)),
    ],
)
def test_parse_color_reads_the_forms_it_claims(token, expected):
    assert parse_color(token) == expected


@pytest.mark.parametrize("token", ["currentColor", "var(--brand)", "hsl(10 50% 50%)", "", "#ff"])
def test_parse_color_refuses_what_it_cannot_resolve(token):
    """A colour whose real value is only known at render time must yield None — a guess
    here becomes a fabricated contrast finding."""
    assert parse_color(token) is None


def test_the_colour_vision_simulation_separates_a_risky_pair_from_a_safe_one():
    """The classic unsafe pair (red vs green) must lose much more of its separation than
    the classic safe pair (blue vs orange). That difference IS the check."""

    def surviving(a, b):
        normal = mod._rgb_distance(a, b)
        simulated = mod._rgb_distance(simulate_deuteranopia(a), simulate_deuteranopia(b))
        return simulated / normal

    assert surviving((0xD6, 0x27, 0x28), (0x2C, 0xA0, 0x2C)) < mod._COLLAPSE_SURVIVING_FRACTION
    assert surviving((0x1F, 0x77, 0xB4), (0xFF, 0x7F, 0x0E)) > mod._COLLAPSE_SURVIVING_FRACTION


# ── markup rules ─────────────────────────────────────────────────────────────

BROKEN_PAGE = """
<html>
<head>
  <meta name="viewport" content="width=device-width, user-scalable=no">
  <style>
    .muted { color: #aaaaaa; background-color: #ffffff; font-size: 9px }
    .a { font-family: Inter, sans-serif } .b { font-family: Georgia, serif }
    .c { font-family: Courier, monospace } .d { font-family: Comic Sans MS, cursive }
  </style>
</head>
<body>
  <h2>Second level first</h2>
  <h4>And a skipped level</h4>
  <img src="hero.png">
  <img src="logo.svg" alt="logo.svg">
  <iframe src="https://player.example/embed"></iframe>
  <video autoplay src="loop.mp4"></video>
  <form>
    <input type="email" name="email" placeholder="Email address">
    <select name="plan"><option>Pro</option></select>
  </form>
  <a href="/pricing">read more</a>
  <a href="/x"></a>
  <button></button>
  <span id="dup"></span><span id="dup"></span>
  <div tabindex="3">Pulled to the front</div>
  <a href="https://other.example" target="_blank">Partner</a>
  <table><tr><td>1</td></tr><tr><td>2</td></tr></table>
</body>
</html>
"""

CLEAN_PAGE = """
<html lang="en">
<head>
  <title>Pricing — Example</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="What Example costs and what each plan includes.">
</head>
<body>
  <main>
    <h1>Pricing</h1>
    <h2>Plans</h2>
    <img src="chart.png" alt="Monthly cost rises with seats, flattening above 50.">
    <form>
      <label for="seats">Seats</label>
      <input id="seats" name="seats" type="number">
    </form>
    <a href="/pricing/enterprise">Compare the enterprise plan</a>
    <table>
      <tr><th scope="col">Plan</th></tr>
      <tr><td>Pro</td></tr>
    </table>
  </main>
</body>
</html>
"""


def test_a_broken_page_yields_the_findings_it_earns():
    findings, summary = analyze_markup(BROKEN_PAGE, url="https://example.test/")
    expected = {
        "a11y.html-lang",
        "a11y.title",
        "a11y.viewport-zoom-locked",
        "a11y.no-main-landmark",
        "a11y.duplicate-id",
        "a11y.img-alt",
        "a11y.img-alt-filename",
        "a11y.iframe-title",
        "a11y.autoplay",
        "a11y.control-label",
        "a11y.link-name",
        "a11y.button-name",
        "a11y.positive-tabindex",
        "a11y.no-h1",
        "a11y.heading-skip",
        "a11y.table-headers",
        "a11y.declared-contrast",
        "heuristic.placeholder-as-label",
        "heuristic.link-text-vague",
        "heuristic.blank-target",
        "heuristic.tiny-type",
        "heuristic.typeface-count",
        "heuristic.meta-description",
    }
    ids = {f.id for f in findings}
    assert expected <= ids, f"missed: {sorted(expected - ids)}"
    assert summary["images_missing_alt"] == 1
    assert summary["form_controls"] == 2


def test_findings_are_ordered_worst_first_and_every_one_is_actionable():
    findings, _ = analyze_markup(BROKEN_PAGE)
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: mod._SEVERITY_ORDER[s])
    assert findings[0].severity == "blocker"
    for finding in findings:
        assert finding.fix.strip(), f"{finding.id} has no fix"
        assert finding.detail.strip(), f"{finding.id} has no detail"
        assert finding.kind in ("a11y", "heuristic")
        if finding.kind == "a11y":
            assert finding.guideline.startswith("WCAG"), finding.id


def test_a_clean_page_earns_no_findings():
    """The rules must be quiet on correct markup, or the report is noise."""
    findings, summary = analyze_markup(CLEAN_PAGE)
    assert [f.id for f in findings] == []
    assert summary["lang"] == "en"
    assert summary["landmarks"] == ["main"]


def test_contrast_is_only_claimed_within_one_rule():
    """A colour in one rule and a background in another may never meet on screen; pairing
    them across rules would manufacture a failure."""
    split = (
        "<html lang=en><head><title>t</title>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        '<meta name=description content="d">'
        "<style>.a { color: #cccccc } .b { background-color: #ffffff }</style>"
        "</head><body><main><h1>h</h1></main></body></html>"
    )
    findings, summary = analyze_markup(split)
    assert "a11y.declared-contrast" not in {f.id for f in findings}
    assert summary["declared_color_pairs"] == 0


def test_a_label_wrapping_its_control_counts_as_labelled():
    findings, _ = analyze_markup("<html><body><label>Email <input name=email></label></body></html>")
    assert "a11y.control-label" not in {f.id for f in findings}


def test_a_label_for_id_counts_as_labelled():
    findings, _ = analyze_markup(
        '<html><body><label for="e">Email</label><input id="e" name=email></body></html>'
    )
    assert "a11y.control-label" not in {f.id for f in findings}


def test_a_submit_input_needs_no_label():
    findings, _ = analyze_markup('<html><body><input type="submit" value="Go"></body></html>')
    assert "a11y.control-label" not in {f.id for f in findings}


def test_a_capped_maximum_scale_counts_as_zoom_locked():
    findings, _ = analyze_markup(
        '<html><head><meta name="viewport" content="width=device-width, maximum-scale=1.0">'
        "</head><body></body></html>"
    )
    assert "a11y.viewport-zoom-locked" in {f.id for f in findings}


def test_unbalanced_markup_does_not_raise():
    """Real pages are not well-formed; a parse crash would take the whole review down."""
    findings, _ = analyze_markup("<html><body><a href=/x><button>Buy<div></body>")
    assert isinstance(findings, list)


# ── layout tables vs data tables ─────────────────────────────────────────────
#
# 🔴 On a real page `a11y.table-headers` reported "4 data table(s) have no header cells"
# when all ten tables there were LAYOUT tables — `WIDTH` / `BORDER=0` / `CELLSPACING=0` /
# `BGCOLOR`, the pre-CSS positioning idiom. The discriminator was `rows > 1 and th == 0`,
# which nothing about a layout table fails.
#
# The false positive is not the worst of it: the offered fix told the author to add
# `<th scope="col">` to a spacer. Following that advice invents a header for data that does
# not exist and puts a meaningless row into the a11y tree — advice that damages the page.

_LAYOUT_PAGE = """
<html lang="en"><head><title>Legacy</title></head>
<body><main>
  <table width="100%" border="0" cellspacing="0" cellpadding="0" bgcolor="#ffffff">
    <tr><td>Masthead</td></tr>
    <tr><td>Body copy that this table is only positioning.</td></tr>
  </table>
</main></body></html>
"""


def test_a_layout_table_is_not_called_a_headerless_data_table():
    findings, _ = analyze_markup(_LAYOUT_PAGE)
    ids = {f.id for f in findings}
    assert "a11y.table-headers" not in ids, sorted(ids)
    # Re-filed, not silenced: the observation is true, so it is still reported — with advice
    # that is correct whichever way the table is meant.
    hit = [f for f in findings if f.id == "heuristic.layout-table"]
    assert hit, sorted(ids)
    assert 'role="presentation"' in hit[0].fix
    assert "border=0" in " ".join(hit[0].evidence)


def test_the_layout_table_advice_never_says_only_add_a_header_row():
    """The specific wrong advice, pinned. A fix that names ONLY the header row is the
    instruction that made this finding harmful rather than merely noisy."""
    findings, _ = analyze_markup(_LAYOUT_PAGE)
    fix = next(f.fix for f in findings if f.id == "heuristic.layout-table")
    assert fix.index('role="presentation"') < fix.index("<th"), fix


@pytest.mark.parametrize(
    "table",
    [
        # An author who declared the table presentational has already answered the question.
        '<table role="presentation"><tr><td>a</td></tr><tr><td>b</td></tr></table>',
        '<table role="none"><tr><td>a</td></tr><tr><td>b</td></tr></table>',
        # A spacer: no cell holds any text, so there is no data for a header to head.
        '<table><tr><td>&nbsp;</td></tr><tr><td><img src="dot.gif" alt=""></td></tr></table>',
    ],
)
def test_a_table_with_no_data_earns_no_header_finding(table):
    findings, _ = analyze_markup(
        f"<html lang=en><head><title>T</title></head><body><main>{table}</main></body></html>"
    )
    assert "a11y.table-headers" not in {f.id for f in findings}


def test_a_genuine_headerless_data_table_is_still_flagged():
    """The direction a one-sided fix would have broken: plain markup, real values, no <th>.
    Disabling the rule, or skipping every table that lacks a header, would pass a test that
    only checked the layout page."""
    findings, _ = analyze_markup(
        "<html lang=en><head><title>T</title></head><body><main><table>"
        "<tr><td>Pro</td><td>$20</td></tr><tr><td>Team</td><td>$50</td></tr>"
        "</table></main></body></html>"
    )
    hit = [f for f in findings if f.id == "a11y.table-headers"]
    assert hit, sorted(f.id for f in findings)
    assert "scope" in hit[0].fix


def test_a_table_that_draws_its_own_grid_is_data_even_with_a_width():
    """`border="1"` says rows and columns ARE the point, so one presentational attribute
    beside it does not reclassify the table."""
    findings, _ = analyze_markup(
        '<html lang=en><head><title>T</title></head><body><main><table border="1" width="100%">'
        "<tr><td>Pro</td></tr><tr><td>Team</td></tr>"
        "</table></main></body></html>"
    )
    assert "a11y.table-headers" in {f.id for f in findings}


# ── pixel rules ──────────────────────────────────────────────────────────────


def _faint_ink(tmp_path, name, ink, fill=(255, 255, 255), size=(1200, 800)):
    """A canvas covered in thin strokes of ``ink`` — text-shaped content, not a panel."""
    img = _canvas(size, fill)
    draw = ImageDraw.Draw(img)
    for row in range(40):
        for col in range(60):
            x, y = 40 + col * 19, 40 + row * 18
            draw.rectangle([x, y, x + 12, y + 3], fill=ink)
    path = tmp_path / f"{name}.png"
    img.save(path)
    return path


def test_faint_ink_is_flagged(tmp_path):
    # The fixture is thin STROKES, not a filled rectangle. It used to be a 360×130 #f2f2f2
    # panel on white, and a panel is a surface: see the elevation-ramp tests below for why a
    # fill one step off the page fill is not a contrast failure. The rule's own claim is
    # about text, icons and borders, so the test now presents one.
    findings, summary = analyze_image(_faint_ink(tmp_path, "faint", (0xEC, 0xEC, 0xEC)))
    hit = [f for f in findings if f.id == "a11y.rendered-contrast"]
    assert hit, sorted(f.id for f in findings)
    assert summary["background"] == "#ffffff"


def test_a_faint_panel_is_a_surface_not_a_contrast_failure(tmp_path):
    # The other half of the same property: near-identical contrast, different geometry.
    img = _canvas((400, 400))
    ImageDraw.Draw(img).rectangle([20, 20, 380, 150], fill=(0xF2, 0xF2, 0xF2))
    path = tmp_path / "panel.png"
    img.save(path)
    findings, _ = analyze_image(path)
    assert "a11y.rendered-contrast" not in {f.id for f in findings}


# ── the dark-theme elevation ramp: the real input these rules were wrong about ──
#
# 🔴 Run against a real 1440×1000 PersonalClaw screenshot, `a11y.rendered-contrast` produced
# exactly one finding and it was wrong: "7 rendered colours under 3:1", on a page whose
# background is #0f0f0f, rail #1f1f1f, card fill #1e1f20 and body text #8e8f90 at ≈5.2:1 —
# passing AA. Every colour it named was a surface fill, and the evidence gave it away:
# `#182020 at 1.02:1 (1.0% of canvas)` cannot be text anyone can see. WCAG 1.4.3/1.4.11
# govern content against its background and say nothing about two adjacent backgrounds.
#
# The bundle's own note predicted this — "pixel thresholds are calibrated on synthetic
# canvases only" — so the fixture below is not another flat synthetic canvas. It reproduces
# the real capture's composition AND its antialiasing, by drawing at 3× and downsampling,
# which is what produced the phantom near-background greys (#505050 at 2.36:1, #585858 at
# 2.67:1) that the smoothed 1440×1000 → 480×333 statistics pass invented out of text edges.


def _dark_shell(ink, size=(1440, 1000)):
    """A dark app shell with a page/rail/card/panel elevation ramp and antialiased ink."""
    scale = 3
    width, height = size
    img = _canvas((width * scale, height * scale), (0x0F, 0x0F, 0x0F))
    draw = ImageDraw.Draw(img)
    for box, fill in (
        ([0, 0, 72, height], (0x1F, 0x1F, 0x1F)),  # rail
        ([120, 80, 760, 420], (0x1E, 0x1F, 0x20)),  # card
        ([800, 80, 1360, 300], (0x28, 0x28, 0x28)),  # panel
        ([800, 340, 1360, 520], (0x28, 0x28, 0x30)),  # tinted panel
    ):
        draw.rectangle([v * scale for v in box], fill=fill)
    for row in range(28):
        for col in range(46):
            x, y = (140 + col * 13) * scale, (110 + row * 11) * scale
            draw.rectangle([x, y, x + 7 * scale, y + 2 * scale], fill=ink)
    for row in range(16):
        for col in range(80):
            x, y = (120 + col * 15) * scale, (560 + row * 12) * scale
            draw.rectangle([x, y, x + 9 * scale, y + 2 * scale], fill=ink)
    return img.resize(size, Image.LANCZOS)


def test_a_dark_elevation_ramp_earns_no_contrast_finding(tmp_path):
    """The real-world input that fooled the rule: AA-passing ink, sub-3:1 surfaces."""
    path = tmp_path / "shell.png"
    _dark_shell((0x8E, 0x8F, 0x90)).save(path)
    findings, summary = analyze_image(path)
    assert "a11y.rendered-contrast" not in {f.id for f in findings}, [
        f.evidence for f in findings if f.id == "a11y.rendered-contrast"
    ]
    # The ramp really is sub-3:1 — the rule is silent because those colours are SURFACES,
    # not because the capture came out bland. If this stops holding, the fixture has drifted.
    assert summary["background"] == "#101010"
    assert contrast_ratio((0x1E, 0x1F, 0x20), (0x0F, 0x0F, 0x0F)) < 1.5


def test_the_same_shell_with_faint_ink_is_still_flagged(tmp_path):
    """The other direction. Nothing changes but the ink colour, so a rule that had merely
    been disabled — or that keyed on the dark palette, the canvas size or the ramp itself —
    would go quiet here too."""
    path = tmp_path / "shell-faint.png"
    _dark_shell((0x3A, 0x3A, 0x3B)).save(path)
    findings, _ = analyze_image(path)
    hit = [f for f in findings if f.id == "a11y.rendered-contrast"]
    assert hit, sorted(f.id for f in findings)
    # …and it names the INK, not any of the four surface fills the ink sits on.
    named = " ".join(hit[0].evidence)
    assert "#383838" in named, named
    for surface in ("#1f1f1f", "#202020", "#282828", "#282830"):
        assert surface not in named, named


def test_a_red_green_palette_is_flagged_for_colour_vision(tmp_path):
    img = _canvas((400, 400))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 399, 120], fill=(0xD6, 0x27, 0x28))
    draw.rectangle([0, 130, 399, 250], fill=(0x2C, 0xA0, 0x2C))
    path = tmp_path / "status.png"
    img.save(path)
    findings, _ = analyze_image(path)
    assert "a11y.color-vision-collapse" in {f.id for f in findings}


def test_a_blue_orange_palette_is_not_flagged_for_colour_vision(tmp_path):
    """The safe pair must stay quiet, or the check is just 'you used two colours'."""
    img = _canvas((400, 400))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 399, 120], fill=(0x1F, 0x77, 0xB4))
    draw.rectangle([0, 130, 399, 250], fill=(0xFF, 0x7F, 0x0E))
    path = tmp_path / "series.png"
    img.save(path)
    findings, _ = analyze_image(path)
    assert "a11y.color-vision-collapse" not in {f.id for f in findings}


def test_ragged_left_edges_are_flagged(tmp_path):
    img = _canvas((390, 200))
    draw = ImageDraw.Draw(img)
    for i in range(8):
        left = 5 + 9 * i
        draw.rectangle([left, 10 + 12 * i, left + 120, 18 + 12 * i], fill=(0x22, 0x22, 0x22))
    path = tmp_path / "ragged.png"
    img.save(path)
    findings, summary = analyze_image(path)
    assert "heuristic.alignment-sprawl" in {f.id for f in findings}
    assert summary["width"] == 390


def test_a_nearly_empty_capture_says_so(tmp_path):
    path = tmp_path / "blank.png"
    _canvas((390, 600)).save(path)
    findings, summary = analyze_image(path)
    assert "heuristic.near-empty" in {f.id for f in findings}
    assert summary["content_share"] == 0


def test_a_hand_sized_window_is_named_as_off_grid(tmp_path):
    path = tmp_path / "odd.png"
    _canvas((1103, 700)).save(path)
    findings, _ = analyze_image(path)
    assert "heuristic.canvas-offgrid" in {f.id for f in findings}


def test_a_device_width_capture_is_not_named_as_off_grid(tmp_path):
    path = tmp_path / "phone.png"
    _canvas((390, 844)).save(path)
    findings, _ = analyze_image(path)
    assert "heuristic.canvas-offgrid" not in {f.id for f in findings}


def test_a_tall_capture_skips_the_single_viewport_checks(tmp_path):
    """A full-page scroll is not one screen, so the composition checks that assume one
    must not fire on it."""
    img = _canvas((390, 2400))
    draw = ImageDraw.Draw(img)
    for i in range(40):
        draw.rectangle([5 + 9 * (i % 8), 10 + 50 * i, 300, 40 + 50 * i], fill=(0x33, 0x33, 0x33))
    path = tmp_path / "fullpage.png"
    img.save(path)
    findings, summary = analyze_image(path)
    assert summary["full_page_capture"] is True
    ids = {f.id for f in findings}
    assert "heuristic.alignment-sprawl" not in ids
    assert "heuristic.margin-imbalance" not in ids


@pytest.mark.parametrize(
    "make,expected",
    [
        (lambda p: None, "No file at"),
        (lambda p: p.mkdir(), "is not a file"),
        (lambda p: p.write_text("not an image"), "could not be decoded"),
    ],
)
def test_image_refusals_name_the_fix(tmp_path, make, expected):
    path = tmp_path / "target"
    make(path)
    with pytest.raises(DesignCritiqueError) as exc:
        analyze_image(path)
    assert expected in str(exc.value)
    assert exc.value.recovery_hints


def test_an_oversized_declared_pixel_count_is_refused_before_decoding(tmp_path, monkeypatch):
    """The bomb guard reads the header, so it must trip without the pixels being decoded."""
    path = tmp_path / "bomb.png"
    _canvas((10, 10)).save(path)
    monkeypatch.setattr(mod, "_MAX_IMAGE_PIXELS", 4)
    with pytest.raises(DesignCritiqueError) as exc:
        analyze_image(path)
    assert "declares 10×10 pixels" in str(exc.value)


def test_an_oversized_file_is_refused_before_decoding(tmp_path, monkeypatch):
    path = tmp_path / "big.png"
    _canvas((10, 10)).save(path)
    monkeypatch.setattr(mod, "_MAX_IMAGE_BYTES", 1)
    with pytest.raises(DesignCritiqueError) as exc:
        analyze_image(path)
    assert "refused before decoding" in str(exc.value)


# ── the page tool, end to end over the seams ─────────────────────────────────


def _patch_fetches(monkeypatch, *, markup, status=200, rendered="", render_ok=True):
    async def fake_markup(url, *, timeout_s):
        return url, markup, status

    async def fake_rendered(url):
        return (True, rendered, "playwright") if render_ok else (False, "", "no renderer")

    monkeypatch.setattr(mod, "_fetch_markup", fake_markup)
    monkeypatch.setattr(mod, "_fetch_rendered", fake_rendered)


def test_a_url_yields_both_a11y_and_craft_findings(app, monkeypatch):
    _patch_fetches(monkeypatch, markup=BROKEN_PAGE, rendered="x" * 900)
    result = asyncio.run(
        app.invoke("design_critique_page", {"url": "https://example.test/checkout"})
    )
    assert result.success
    assert result.metadata["response_type"] == RESPONSE_TYPE_PAGE
    assert result.metadata["counts"]["a11y"] > 0
    assert result.metadata["counts"]["heuristic"] > 0
    assert "a11y.img-alt" in _ids(result.metadata["findings"])
    assert result.metadata["measured"]["rendered_chars"] == 900
    # The report states the ceiling of what a source-level parse can see.
    assert "STATIC HTML" in result.output


def test_a_client_rendered_shell_is_called_out(app, monkeypatch):
    shell = '<html lang="en"><head><title>App</title></head><body><div id="root"></div></body></html>'
    _patch_fetches(monkeypatch, markup=shell, rendered="real content " * 200)
    result = asyncio.run(app.invoke("design_critique_page", {"url": "https://spa.test/"}))
    assert "heuristic.client-rendered-shell" in _ids(result.metadata["findings"])


def test_a_failed_render_degrades_to_static_markup(app, monkeypatch):
    _patch_fetches(monkeypatch, markup=BROKEN_PAGE, render_ok=False)
    result = asyncio.run(app.invoke("design_critique_page", {"url": "https://example.test/"}))
    assert result.success
    assert "no renderer" in result.output
    assert "rendered_chars" not in result.metadata["measured"]


def test_render_false_skips_the_second_fetch(app, monkeypatch):
    calls: list[str] = []

    async def fake_rendered(url):
        calls.append(url)
        return True, "", ""

    _patch_fetches(monkeypatch, markup=CLEAN_PAGE)
    monkeypatch.setattr(mod, "_fetch_rendered", fake_rendered)
    result = asyncio.run(
        app.invoke("design_critique_page", {"url": "https://example.test/", "render": False})
    )
    assert result.success
    assert calls == []
    assert "render=false" in result.output


def test_kinds_narrows_the_report(app, monkeypatch):
    _patch_fetches(monkeypatch, markup=BROKEN_PAGE, render_ok=False)
    result = asyncio.run(
        app.invoke("design_critique_page", {"url": "https://example.test/", "kinds": "a11y"})
    )
    assert result.metadata["counts"]["heuristic"] == 0
    assert all(f["kind"] == "a11y" for f in result.metadata["findings"])


def test_an_unknown_kinds_value_is_refused(app, monkeypatch):
    _patch_fetches(monkeypatch, markup=CLEAN_PAGE, render_ok=False)
    result = asyncio.run(
        app.invoke("design_critique_page", {"url": "https://example.test/", "kinds": "vibes"})
    )
    assert not result.success
    assert "vibes" in result.error


def test_max_findings_clamps_and_says_what_it_withheld(app, monkeypatch):
    _patch_fetches(monkeypatch, markup=BROKEN_PAGE, render_ok=False)
    result = asyncio.run(
        app.invoke("design_critique_page", {"url": "https://example.test/", "max_findings": 3})
    )
    assert len(result.metadata["findings"]) == 3
    assert result.metadata["counts"]["matched"] > 3
    assert "withheld" in result.output


@pytest.mark.parametrize("url", ["", "example.com", "file:///etc/passwd", "ftp://host/x"])
def test_a_non_http_target_is_refused_before_any_fetch(app, monkeypatch, url):
    async def exploding(*a, **k):
        raise AssertionError("the guard must refuse before fetching")

    monkeypatch.setattr(mod, "_fetch_markup", exploding)
    result = asyncio.run(app.invoke("design_critique_page", {"url": url}))
    assert not result.success
    assert result.recovery_hints


def test_an_error_status_is_not_reviewed(app, monkeypatch):
    _patch_fetches(monkeypatch, markup="<html><body>404</body></html>", status=404)
    result = asyncio.run(app.invoke("design_critique_page", {"url": "https://example.test/x"}))
    assert not result.success
    assert "HTTP 404" in result.error


def test_a_blocked_fetch_becomes_an_actionable_refusal(app, monkeypatch):
    async def blocked(url, *, timeout_s):
        raise RuntimeError("egress denied: private host")

    monkeypatch.setattr(mod, "_fetch_markup", blocked)
    result = asyncio.run(app.invoke("design_critique_page", {"url": "http://10.0.0.1/"}))
    assert not result.success
    assert "egress denied" in result.error
    assert "Security → Network" in " ".join(result.recovery_hints)


def test_the_page_tool_passes_the_configured_timeout(monkeypatch):
    seen: dict[str, int] = {}

    async def fake_markup(url, *, timeout_s):
        seen["timeout"] = timeout_s
        return url, CLEAN_PAGE, 200

    monkeypatch.setattr(mod, "_fetch_markup", fake_markup)
    asyncio.run(
        create_provider({"timeout_secs": 7}).invoke(
            "design_critique_page", {"url": "https://x.test/", "render": False}
        )
    )
    assert seen["timeout"] == 7


def test_the_page_fetch_rides_cores_egress_guard():
    """The one thing a mocked fetch cannot prove: the real seam uses the guarded
    chokepoint and core's layered operator policy, not a bare HTTP client."""
    source = (HERE / "provider.py").read_text(encoding="utf-8")
    assert "from personalclaw.sdk.net import CONNECTOR, egress_policy_for, fetch" in source
    for forbidden in ("import aiohttp", "import requests", "urllib.request", "httpx"):
        assert forbidden not in source, f"{forbidden} bypasses the egress guard"


# ── the screenshot tool + rubric, through invoke ─────────────────────────────


def test_a_screenshot_yields_both_a11y_and_craft_findings(app, tmp_path):
    # A status board that earns one finding of each kind: red/green bands that collapse
    # for red-green colour blindness, a near-invisible row, and eight ragged left edges.
    img = _canvas((390, 400))
    draw = ImageDraw.Draw(img)
    draw.rectangle([16, 0, 374, 100], fill=(0xD6, 0x27, 0x28))
    draw.rectangle([16, 110, 374, 210], fill=(0x2C, 0xA0, 0x2C))
    draw.rectangle([16, 220, 200, 250], fill=(0xF2, 0xF2, 0xF2))
    for i in range(8):
        left = 16 + 12 * i
        draw.rectangle([left, 260 + 16 * i, left + 100, 268 + 16 * i], fill=(0x22, 0x22, 0x22))
    path = tmp_path / "status.png"
    img.save(path)
    result = asyncio.run(app.invoke("design_critique_image", {"path": str(path)}))
    assert result.success
    assert result.metadata["response_type"] == RESPONSE_TYPE_IMAGE
    assert result.metadata["counts"]["a11y"] > 0
    assert result.metadata["counts"]["heuristic"] > 0
    assert "design_critique_rubric" in result.output


def test_a_missing_screenshot_is_refused_not_raised(app):
    result = asyncio.run(app.invoke("design_critique_image", {"path": "/nope/none.png"}))
    assert not result.success
    assert "No file at" in result.error
    assert result.recovery_hints


def test_a_home_relative_path_is_expanded(app, monkeypatch, tmp_path):
    seen: dict[str, Path] = {}

    def fake_analyze(path):
        seen["path"] = path
        return [], {"full_page_capture": False, "path": str(path)}

    monkeypatch.setattr(mod, "analyze_image", fake_analyze)
    monkeypatch.setenv("HOME", str(tmp_path))
    asyncio.run(app.invoke("design_critique_image", {"path": "~/shot.png"}))
    assert seen["path"] == tmp_path / "shot.png"


def test_an_empty_report_is_not_reported_as_a_pass(app, monkeypatch):
    _patch_fetches(monkeypatch, markup=CLEAN_PAGE, render_ok=False)
    result = asyncio.run(app.invoke("design_critique_page", {"url": "https://example.test/"}))
    assert result.success
    assert result.metadata["findings"] == []
    assert "not a pass" in result.output


@pytest.mark.parametrize("surface", ["screenshot", "flow", "page"])
def test_every_rubric_surface_returns_a_procedure(app, surface):
    result = asyncio.run(app.invoke("design_critique_rubric", {"surface": surface}))
    assert result.success
    assert result.metadata["response_type"] == RESPONSE_TYPE_RUBRIC
    assert result.metadata["surface"] == surface
    assert len(result.metadata["items"]) >= 5
    for item in result.metadata["items"]:
        assert item["name"].strip() and item["prompt"].strip()


def test_the_rubric_needs_nothing_from_the_environment(app):
    result = asyncio.run(app.invoke("design_critique_rubric", {}))
    assert result.success
    assert result.metadata["surface"] == "screenshot"


def test_an_unknown_rubric_surface_lists_the_real_ones(app):
    result = asyncio.run(app.invoke("design_critique_rubric", {"surface": "billboard"}))
    assert not result.success
    assert "screenshot" in result.error and "flow" in result.error


def test_an_unexpected_failure_is_reported_not_raised(app, monkeypatch):
    """A broken review must never take the turn down."""

    def explode(*a, **k):
        raise ValueError("rule engine broke")

    monkeypatch.setattr(mod, "analyze_image", explode)
    result = asyncio.run(app.invoke("design_critique_image", {"path": "whatever.png"}))
    assert not result.success
    assert "rule engine broke" in result.error
