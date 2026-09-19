"""KOCR-2's six clauses, held to account against the REAL engine.

These tests do not stub RapidOCR. Stubbing it would leave every clause that matters
unmeasured: "OCR produces non-empty text with no model bound" is a claim about an engine
actually reading pixels, and a fake that returns a canned string proves only that the test
can be written. The apps CI installs each bundle's declared ``pythonDependencies`` before
running its tests, so the engine is present there — and if it ever is not, these fail
loudly rather than skipping, which is the posture this repo's CI documents.

The ground-truth token is rendered INTO the image and then asserted out of the OCR output,
so nothing but a real recognition pass can make the assertion pass.
"""

from __future__ import annotations

import ast
import importlib.metadata
import os
import pathlib
import re

import pytest
from PIL import Image, ImageDraw

from provider import MAX_IMAGES, RapidOcrProvider, create_provider

from personalclaw.sdk.ocr import OcrRejected, active_ocr

HERE = pathlib.Path(__file__).parent

#: Rendered into the fixture image and then required back out of the OCR result. A single
#: contiguous run of glyphs: the recognizer's word segmentation is its own business, so a
#: token with no internal space cannot be failed by a legitimate difference in spacing.
GROUND_TRUTH = "KOCRTOKEN4711"

#: docs/ENGINE-LICENCE.md's clause-6 allowlist. BSD-3-Clause is deliberately NOT here — a
#: transitive dependency is documented in the note, but the ENGINE and its bundled weights
#: must be Apache-2.0 or MIT, and widening this list would be the exact drift it guards.
PERMISSIVE = {"Apache-2.0", "MIT"}

#: Footprint ceiling from the note. Measured delta was 146.6 MB.
FOOTPRINT_CEILING_MB = 250

#: The engine's own distributions — everything else it needs is already a core dependency.
ENGINE_DISTS = ("rapidocr-onnxruntime", "opencv-python", "shapely", "pyclipper")


def _text_image(path: str, token: str = GROUND_TRUTH) -> str:
    """Render *token* large enough to be legible, and return the path.

    Upscaled after drawing because PIL's default bitmap font is small enough that the
    recognizer's detection stage can miss it entirely — that would make a green test a
    statement about font size rather than about OCR.
    """
    img = Image.new("RGB", (900, 240), "white")
    ImageDraw.Draw(img).text((40, 90), token, fill="black")
    img.resize((1800, 480), Image.LANCZOS).save(path, format="PNG")
    return path


@pytest.fixture
def provider() -> RapidOcrProvider:
    return create_provider({})


@pytest.fixture
def image(tmp_path) -> str:
    return _text_image(str(tmp_path / "page.png"))


def test_engine_is_installed_and_available(provider):
    """The precondition every OCR clause below rests on, asserted rather than assumed.

    A suite that silently skipped here would report green while measuring nothing.
    """
    assert provider.available() is True, (
        "rapidocr-onnxruntime (with its bundled .onnx models) must be installed for this "
        "bundle's tests — it is declared in app.json pythonDependencies"
    )


# ── clause 1: observable OCR with NO model bound ────────────────────────────────────


@pytest.mark.asyncio
async def test_ocr_with_no_model_bound_returns_the_ground_truth(provider, image):
    """No model is bound in this process — `can_resolve_use_case("image_modality")` is
    False, which is the environment where core's VLM OcrNode is skipped — and OCR still
    produces non-empty text carrying the token rendered into the image."""
    from personalclaw.sdk.ocr import assert_image  # noqa: F401  (import-path smoke)

    from personalclaw.knowledge.pipeline.registry import can_resolve_use_case

    assert can_resolve_use_case("image_modality") is False, (
        "this test's whole point is the no-model environment; a bound image model here "
        "would let a VLM satisfy the assertion and the clause would go unmeasured"
    )

    result = await provider.recognize([image])

    assert result.text.strip(), "OCR produced no text at all"
    squashed = re.sub(r"\s+", "", result.text)
    assert GROUND_TRUTH in squashed, f"ground truth {GROUND_TRUTH!r} not in {result.text!r}"
    assert result.engine.startswith("rapidocr-onnxruntime/")


# ── clause 2: SDK boundary ─────────────────────────────────────────────────────────


def test_imports_core_only_via_sdk():
    """Every ``personalclaw`` import in this bundle's non-test modules goes through
    ``personalclaw.sdk.*``. Mirrors core's tests/test_apps_import_boundary.py semantics,
    asserted from inside the bundle so the boundary holds even before the repo-level lint."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        bad = []
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for mod in names:
                parts = mod.split(".")
                if parts[0] == "personalclaw" and (len(parts) < 2 or parts[1] != "sdk"):
                    bad.append(mod)
        if bad:
            offenders[path.name] = sorted(set(bad))
    assert offenders == {}, f"apps may import core only via personalclaw.sdk.*: {offenders}"


# ── clause 3: deterministic ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_image_twice_is_byte_identical(provider, image):
    """No sampling variance: two passes over the same bytes give the same text."""
    first = await provider.recognize([image])
    second = await provider.recognize([image])
    assert first.text == second.text
    assert first.pages == second.pages
    assert provider.deterministic is True


@pytest.mark.asyncio
async def test_page_order_and_blank_pages_are_preserved(provider, tmp_path):
    """``pages`` stays index-aligned with the input, so a blank page does not silently
    shift every later page's text onto the wrong index."""
    a = _text_image(str(tmp_path / "a.png"), "ALPHA111")
    blank = str(tmp_path / "blank.png")
    Image.new("RGB", (600, 200), "white").save(blank, format="PNG")
    c = _text_image(str(tmp_path / "c.png"), "GAMMA333")

    result = await provider.recognize([a, blank, c])

    assert len(result.pages) == 3
    assert "ALPHA111" in re.sub(r"\s+", "", result.pages[0])
    assert result.pages[1].strip() == ""
    assert "GAMMA333" in re.sub(r"\s+", "", result.pages[2])


# ── clause 4: additive / removable ─────────────────────────────────────────────────


def test_importing_the_bundle_registers_nothing():
    """Importing ``provider`` must not register anything: the platform's ``ocr`` type
    handler registers on ENABLE and unregisters on disable, so a module that self-registered
    would stay active after the user removed the app — the opposite of removable."""
    assert active_ocr() is None, (
        "no OCR provider may be active from import alone; registration is core's job "
        "through the ocr type handler"
    )


# ── clause 5: true-type gate ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_png_extension_over_non_image_bytes_is_rejected_before_ocr(provider, tmp_path):
    """A ``.png`` whose bytes are a PDF is refused by MAGIC BYTES, not by its extension."""
    liar = tmp_path / "actually-a-pdf.png"
    liar.write_bytes(b"%PDF-1.7\n%\xc3\xa4\xc3\xbc\n1 0 obj\n<<>>\nendobj\n")

    with pytest.raises(OcrRejected) as excinfo:
        await provider.recognize([str(liar)])

    assert "bytes are" in str(excinfo.value)


@pytest.mark.asyncio
async def test_rejection_happens_before_the_engine_is_built(tmp_path):
    """The gate runs BEFORE recognition, proven by the engine never being constructed.

    A gate that rejected *after* the decoder had already been handed the bytes would pass a
    test that only checked the exception type, so the ordering is asserted directly.
    """
    prov = create_provider({})
    liar = tmp_path / "not-an-image.png"
    liar.write_bytes(b"this is plain text pretending to be a PNG")

    with pytest.raises(OcrRejected):
        await prov.recognize([str(liar)])

    assert prov._engine is None, "input was rejected only after the engine was constructed"


@pytest.mark.asyncio
async def test_extension_and_bytes_must_agree(provider, tmp_path):
    """A real PNG named ``.jpg`` is refused too — the two facts have to agree, or either
    one alone becomes a way past the other."""
    mismatched = str(tmp_path / "png-bytes.jpg")
    _text_image(mismatched)  # writes PNG bytes under a .jpg name

    with pytest.raises(OcrRejected):
        await provider.recognize([mismatched])


@pytest.mark.asyncio
async def test_unlisted_extension_is_rejected(provider, tmp_path):
    """Allowlist, not denylist: a format nobody enumerated is refused, not forwarded."""
    svg = tmp_path / "vector.svg"
    svg.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'><text>hi</text></svg>")

    with pytest.raises(OcrRejected):
        await provider.recognize([str(svg)])


@pytest.mark.asyncio
async def test_image_count_ceiling(provider, image):
    """One call cannot be handed an unbounded page set."""
    with pytest.raises(OcrRejected):
        await provider.recognize([image] * (MAX_IMAGES + 1))


# ── clause 6: licence sign-off + footprint ceiling ─────────────────────────────────


def _note() -> str:
    note = HERE / "docs" / "ENGINE-LICENCE.md"
    assert note.exists(), "docs/ENGINE-LICENCE.md is the clause-6 record and must exist"
    return note.read_text(encoding="utf-8")


def test_note_records_the_engine_id_licence_and_source():
    """The note names what shipped, not a family. Engine id, SPDX licence and a source
    link for both the engine and the weights it carries."""
    text = _note()
    version = importlib.metadata.version("rapidocr-onnxruntime")
    assert f"rapidocr-onnxruntime/{version}" in text, (
        f"the note records a different engine version than the installed {version} — "
        "an unverified licence record is exactly what clause 6 forbids"
    )
    assert "https://github.com/RapidAI/RapidOCR" in text
    assert "https://github.com/PaddlePaddle/PaddleOCR" in text, (
        "the bundled weights derive from PaddleOCR, so their provenance needs its own "
        "source link — it cannot be inferred from the engine's"
    )


def test_recorded_licences_are_permissive():
    """The engine's licence, as INSTALLED, is on the permissive allowlist and matches the
    note. Read from the distribution metadata rather than from the note alone, so the note
    cannot be the only witness to its own claim."""
    meta = importlib.metadata.metadata("rapidocr-onnxruntime")
    declared = (meta.get("License") or "").strip()
    normalized = declared.replace("Apache 2.0", "Apache-2.0")
    assert normalized in PERMISSIVE, (
        f"installed rapidocr-onnxruntime declares licence {declared!r}, which is not on "
        f"the permissive allowlist {sorted(PERMISSIVE)}"
    )
    assert f"| licence | `{normalized}` |" in _note(), (
        "docs/ENGINE-LICENCE.md does not record the licence the installed wheel declares"
    )


def test_every_transitive_dependency_licence_is_recorded():
    """Each distribution the engine adds appears in the note with a licence.

    Not an allowlist assertion — a transitive may legitimately be BSD — but it must be
    WRITTEN DOWN, so nothing arrives in a user's environment undocumented.
    """
    text = _note()
    for dist in ENGINE_DISTS:
        assert f"`{dist}`" in text, f"{dist} is installed by this app but absent from the note"


def test_footprint_under_ceiling():
    """The measured on-disk delta of the engine's own distributions stays under the ceiling
    the note declares, so dependency growth has to be argued for in a PR."""
    total = 0
    for dist in ENGINE_DISTS:
        try:
            files = importlib.metadata.distribution(dist).files or []
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover - guarded above
            pytest.fail(f"{dist} is declared in app.json but not installed")
        base = importlib.metadata.distribution(dist).locate_file("")
        for rel in files:
            path = os.path.join(str(base), str(rel))
            if os.path.isfile(path):
                total += os.path.getsize(path)
    megabytes = total / 1_000_000
    assert megabytes <= FOOTPRINT_CEILING_MB, (
        f"the engine's distributions measure {megabytes:.1f} MB, over the "
        f"{FOOTPRINT_CEILING_MB} MB ceiling recorded in docs/ENGINE-LICENCE.md"
    )
    assert f"Ceiling: {FOOTPRINT_CEILING_MB} MB" in _note()


def test_weights_ship_in_the_wheel():
    """"Offline" is a claim about the install, not about a past download: the ONNX weights
    are present inside the installed package, so a first OCR needs no network."""
    import rapidocr_onnxruntime

    models = os.path.join(os.path.dirname(rapidocr_onnxruntime.__file__), "models")
    onnx = [f for f in os.listdir(models) if f.endswith(".onnx")]
    assert onnx, "no .onnx weights in the installed wheel — this engine would need a download"
