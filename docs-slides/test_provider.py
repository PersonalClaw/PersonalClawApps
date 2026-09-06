"""Tests for the docs-slides tool provider.

Everything here runs with no network, no credentials and no gateway. The two
file-producing tools are proven by RENDERING and then READING BACK: a .pptx is parsed
by core's own pptx parser and a .docx by its docx parser, so "openable" is measured
rather than asserted from a byte count. A test that only checked the file was non-empty
would pass on 30 KB of unopenable zip.

Contract: personalclaw.sdk.tool:ToolProvider
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Test files may reach past the SDK (core's own boundary lint exempts them) — these two
# parsers are how "openable" is measured, and there is no SDK read-half to use instead.
from personalclaw.documents.docx_parser import parse_docx
from personalclaw.documents.pptx_parser import parse_pptx
from personalclaw.sdk.documents import available_formats
from personalclaw.sdk.manifest import AppManifest

import app_cli
from provider import (
    DECK_FORMAT,
    DOCUMENT_FORMATS,
    DocsSlidesProvider,
    create_provider,
    safe_stem,
    title_of,
)

CONTRACT_METHODS = ("display_name", "invoke", "list_tools", "name")

HERE = Path(__file__).parent

BRIEF = """# Q3 platform review

## Where we are
- Nine apps shipped
- Two behind the gate

## What changed
- The document seam is on the app boundary now
- Nothing vendors python-pptx

## Ask
- One reviewer per app
"""


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Point core's config dir at a tmp dir so nothing is written under ~."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "pclaw"))
    return tmp_path


@pytest.fixture
def provider(home) -> DocsSlidesProvider:
    return create_provider({})


# ── The scaffold contract (a change that breaks registration fails here first) ──


def test_factory_returns_the_provider(home) -> None:
    assert isinstance(create_provider({}), DocsSlidesProvider)


def test_factory_accepts_no_config(home) -> None:
    assert isinstance(create_provider(None), DocsSlidesProvider)


def test_nothing_abstract_is_left() -> None:
    """An unimplemented abstract method makes the provider uninstantiable."""
    assert not getattr(DocsSlidesProvider, "__abstractmethods__", frozenset())


def test_registers_under_the_app_name(provider) -> None:
    """Every per-type registry keys a provider by `.name`."""
    assert provider.name == "docs-slides"


def test_declares_its_display_name(provider) -> None:
    assert provider.display_name == "Docs & Slides"


def test_every_contract_method_is_declared(provider) -> None:
    for name in CONTRACT_METHODS:
        assert name in vars(DocsSlidesProvider), f"{name} is not implemented"


def test_settings_reach_the_provider(home) -> None:
    assert create_provider({"max_brief_chars": 500})._max_brief_chars == 500


# ── The manifest is the install contract ──


def test_the_manifest_parses_and_validates() -> None:
    mani = AppManifest.from_dict(json.loads((HERE / "app.json").read_text(encoding="utf-8")))
    mani.validate()
    assert mani.name == "docs-slides"
    assert mani.provider is not None
    assert mani.provider.type == "tool"
    assert mani.provider.implementation == "provider:create_provider"


def test_the_manifest_asks_for_storage_and_nothing_else() -> None:
    """Minimum permissions: the app writes files, so it needs storage — and no network."""
    data = json.loads((HERE / "app.json").read_text(encoding="utf-8"))
    perms = data["permissions"]
    assert perms == {"storage": True, "network": False}


# ── Tool declarations ──


@pytest.mark.asyncio
async def test_declares_the_three_tools(provider) -> None:
    names = [t.name for t in await provider.list_tools()]
    assert names == ["deck_from_brief", "document_from_brief", "docs_slides_formats"]


@pytest.mark.asyncio
async def test_the_document_format_enum_only_offers_renderable_formats(provider) -> None:
    """The enum a model chooses from is derived from the build, not hard-coded."""
    tools = {t.name: t for t in await provider.list_tools()}
    offered = tools["document_from_brief"].parameters["properties"]["format"]["enum"]
    assert offered == [f for f in DOCUMENT_FORMATS if f in available_formats()]
    assert set(offered) <= set(available_formats())


@pytest.mark.asyncio
async def test_the_read_only_tool_is_the_only_safe_one(provider) -> None:
    risk = {t.name: t.risk_level.value for t in await provider.list_tools()}
    assert risk["docs_slides_formats"] == "safe"
    assert risk["deck_from_brief"] == "caution"
    assert risk["document_from_brief"] == "caution"


@pytest.mark.asyncio
async def test_an_unknown_tool_is_refused_with_a_hint(provider) -> None:
    res = await provider.invoke("make_me_a_deck", {})
    assert not res.success
    assert "unknown tool" in res.error
    assert res.recovery_hints


# ── A brief becomes an openable deck ──


@pytest.mark.asyncio
async def test_a_brief_produces_an_openable_pptx(provider) -> None:
    res = await provider.invoke("deck_from_brief", {"brief": BRIEF})
    assert res.success, res.error
    path = Path(res.metadata["path"])
    assert path.suffix == ".pptx" and path.is_file()

    deck, _loss = parse_pptx(path.read_bytes())
    titles = [s.title for s in deck.slides]
    assert "Where we are" in titles
    assert "What changed" in titles
    assert "Ask" in titles
    body = [b.text for s in deck.slides for b in s.bullets]
    assert "Nine apps shipped" in body


@pytest.mark.asyncio
async def test_the_deck_title_comes_from_the_brief_when_unset(provider) -> None:
    res = await provider.invoke("deck_from_brief", {"brief": BRIEF})
    assert res.metadata["title"] == "Q3 platform review"


@pytest.mark.asyncio
async def test_an_explicit_title_wins(provider) -> None:
    res = await provider.invoke("deck_from_brief", {"brief": BRIEF, "title": "Board deck"})
    assert res.metadata["title"] == "Board deck"


# ── A brief becomes a compiled document ──


@pytest.mark.asyncio
async def test_a_brief_produces_a_compiled_document(provider) -> None:
    res = await provider.invoke("document_from_brief", {"brief": BRIEF, "format": "docx"})
    assert res.success, res.error
    path = Path(res.metadata["path"])
    assert path.suffix == ".docx" and path.is_file()

    doc, _loss = parse_docx(path.read_bytes())
    assert doc.title == "Q3 platform review"
    headings = [b.text for b in doc.blocks if b.kind == "heading"]
    assert "Where we are" in headings and "Ask" in headings
    bullets = [item for b in doc.blocks for item in b.items]
    assert "Nine apps shipped" in bullets


@pytest.mark.asyncio
async def test_the_document_format_defaults_to_the_first_renderable_one(provider) -> None:
    res = await provider.invoke("document_from_brief", {"brief": BRIEF})
    assert res.success, res.error
    assert res.metadata["format"] == [f for f in DOCUMENT_FORMATS if f in available_formats()][0]


@pytest.mark.asyncio
async def test_a_deck_format_is_refused_as_a_document(provider) -> None:
    """pptx is a different MODEL, not a document rendering — the tools do not overlap."""
    res = await provider.invoke("document_from_brief", {"brief": BRIEF, "format": DECK_FORMAT})
    assert not res.success
    assert DECK_FORMAT in res.error


@pytest.mark.asyncio
async def test_an_unknown_format_is_refused_before_any_render(provider) -> None:
    res = await provider.invoke("document_from_brief", {"brief": BRIEF, "format": "epub"})
    assert not res.success
    assert "epub" in res.error
    assert not list(provider.out_dir.iterdir())


# ── Refusals ──


@pytest.mark.asyncio
async def test_an_empty_brief_is_refused_with_a_hint(provider) -> None:
    res = await provider.invoke("deck_from_brief", {"brief": "   \n  "})
    assert not res.success
    assert "empty" in res.error
    assert res.recovery_hints


@pytest.mark.asyncio
async def test_a_brief_over_the_cap_is_refused_with_the_number(home) -> None:
    prov = create_provider({"max_brief_chars": 50})
    res = await prov.invoke("deck_from_brief", {"brief": "# T\n" + ("x" * 200)})
    assert not res.success
    assert "50" in res.error
    assert not list(prov.out_dir.iterdir())


# ── Output containment ──


@pytest.mark.asyncio
async def test_a_traversal_filename_cannot_escape_the_data_dir(provider, tmp_path) -> None:
    res = await provider.invoke(
        "deck_from_brief", {"brief": BRIEF, "filename": "../../../../tmp/pwned"}
    )
    assert res.success, res.error
    path = Path(res.metadata["path"])
    assert path.parent == provider.out_dir
    assert path.name == "pwned.pptx"
    assert not (tmp_path / "pwned.pptx").exists()


@pytest.mark.asyncio
async def test_the_output_dir_is_inside_the_apps_own_data_dir(provider, home) -> None:
    assert provider.out_dir.is_relative_to(home / "pclaw" / "apps" / "docs-slides" / "data")


@pytest.mark.asyncio
async def test_no_tool_takes_a_directory_argument(provider) -> None:
    """The absence IS the containment: there is no argument that names a write target."""
    for tool in await provider.list_tools():
        keys = set(tool.parameters.get("properties", {}))
        assert not keys & {"dir", "directory", "path", "out_dir", "output_dir"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("/absolute/deck.pptx", "deck"),
        ("..", "fallback"),
        (".hidden", "hidden"),
        ("", "fallback"),
        ("Q3 platform review", "Q3-platform-review"),
        ("a" * 200, "a" * 80),
    ],
)
def test_a_filename_is_reduced_to_a_bare_stem(raw: str, expected: str) -> None:
    assert safe_stem(raw, "fallback") == expected


def test_a_title_falls_back_to_the_first_heading_then_a_default() -> None:
    assert title_of("# One\n## Two", "") == "One"
    assert title_of("no headings here", "") == "Untitled"
    assert title_of("# One", "Explicit") == "Explicit"


# ── Honest capability reporting ──


@pytest.mark.asyncio
async def test_the_formats_tool_reports_what_this_build_can_render(provider) -> None:
    res = await provider.invoke("docs_slides_formats", {})
    assert res.success
    assert res.metadata["deck_formats"] == provider.deck_formats()
    assert res.metadata["document_formats"] == provider.document_formats()
    assert set(res.metadata["deck_formats"]) <= set(available_formats())


@pytest.mark.asyncio
async def test_a_format_this_build_cannot_render_is_refused_not_attempted(
    provider, monkeypatch
) -> None:
    """Simulate a build with no pptx writer: the tool must refuse, not write a stub file."""
    import provider as provider_mod

    monkeypatch.setattr(provider_mod, "get_writer", lambda fmt: None)
    res = await provider.invoke("deck_from_brief", {"brief": BRIEF})
    assert not res.success
    assert "cannot render" in res.error
    assert not list(provider.out_dir.iterdir())


# ── The doctor probe reports the HOST's renderers, not the bundle's ──


def test_doctor_is_ok_when_both_halves_render() -> None:
    lines = app_cli.doctor()
    assert len(lines) == 1
    assert lines[0].label == "Docs & Slides"
    assert lines[0].status == "ok"
    assert DECK_FORMAT in lines[0].detail


def test_doctor_fails_when_the_build_has_no_writers(monkeypatch) -> None:
    monkeypatch.setattr(app_cli, "available_formats", lambda: [])
    line = app_cli.doctor()[0]
    assert line.status == "fail"
    assert "no document writer" in line.detail


def test_doctor_warns_when_only_one_half_renders(monkeypatch) -> None:
    monkeypatch.setattr(app_cli, "available_formats", lambda: [DECK_FORMAT])
    line = app_cli.doctor()[0]
    assert line.status == "warn"
    assert DECK_FORMAT in line.detail


def test_setup_collects_nothing_and_says_what_renders() -> None:
    printed: list[str] = []

    class Ctx:
        def print(self, msg: str) -> None:
            printed.append(msg)

    app_cli.setup(Ctx())
    assert printed and "Docs & Slides" in printed[0]
