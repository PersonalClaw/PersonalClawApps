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


# ── pixel rules ──────────────────────────────────────────────────────────────


def test_a_low_contrast_capture_is_flagged(tmp_path):
    img = _canvas((400, 400))
    ImageDraw.Draw(img).rectangle([20, 20, 380, 150], fill=(0xF2, 0xF2, 0xF2))
    path = tmp_path / "faint.png"
    img.save(path)
    findings, summary = analyze_image(path)
    assert "a11y.rendered-contrast" in {f.id for f in findings}
    assert summary["background"] == "#ffffff"


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
