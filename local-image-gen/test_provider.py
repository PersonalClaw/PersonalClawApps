"""Rails for the fully-local image backend (IG-1).

Organised by the five clauses IG-1 has to satisfy, because a rail whose subject is
unclear is the kind that quietly goes vacuous:

* **Clause 1** — generation runs through core's existing SEL-audited
  ``_image_generate`` dispatch. Driven for real: a loopback HTTP server stands in
  for ComfyUI, the provider resolves through the real ``image_gen`` registry, and
  ``_call_tool_inner("image_generate", …)`` is the entry point. No model weights
  are involved anywhere.
* **Clause 2** — it registers exactly as ``fal-image`` does, adds no core vendor
  string and no second generation dispatch.
* **Clause 3** — the recommendation is a genuinely permissive model, and every
  C9-disqualified trap is refused. Includes the rail's own negative case.
* **Clause 4** — the bundle ships no model artifact, so it cannot move a packaging
  size budget. Includes the rail's own negative case.
* **Clause 5** — declared-but-not-pulled degrades to a calm message, never a 500.

Plus the egress posture the ARCC SSRF guidance asks for on an operator-configured
endpoint.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import provider as app
from provider import (
    PERMISSIVE_LICENCES,
    RECOMMENDED_MODEL,
    LocalComfyImageProvider,
    by_name,
    catalog,
    create_provider,
    is_permissive,
    recommended_model,
    setup_help,
)
from personalclaw.sdk.image import ImageGenError, ImageGenProvider

_APP_DIR = Path(__file__).resolve().parent
_MANIFEST = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))

#: A real 1x1 PNG. Small enough to inline, real enough that the artifact store
#: persists genuine image bytes rather than a placeholder string.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/gFj"
    "1UkAAAAASUVORK5CYII="
)

_INSTALLED = "flux1-schnell.safetensors"


# ── A loopback stand-in for ComfyUI ──────────────────────────────────────────


class _FakeComfy:
    """A real HTTP server on 127.0.0.1 speaking the ComfyUI shapes this app uses.

    A real socket, not a patched ``fetch``: that is what makes the clause-1 drive
    evidence about the whole path (egress guard included) instead of evidence about
    a mock. ``checkpoints=[]`` models the runtime-up-but-no-weights state clause 5
    is about.
    """

    def __init__(self, *, checkpoints: list[str] | None = None, fail_queue: bool = False) -> None:
        self.checkpoints = [] if checkpoints is None else list(checkpoints)
        self.fail_queue = fail_queue
        self.graphs: list[dict] = []
        outer = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # keep pytest output clean
                pass

            def _send(self, status, body: bytes, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status, payload):
                self._send(status, json.dumps(payload).encode())

            def do_GET(self):
                if self.path.startswith("/system_stats"):
                    return self._json(200, {"system": {"comfyui_version": "test"}})
                if self.path.startswith("/object_info/CheckpointLoaderSimple"):
                    return self._json(
                        200,
                        {
                            "CheckpointLoaderSimple": {
                                "input": {"required": {"ckpt_name": [outer.checkpoints]}}
                            }
                        },
                    )
                if self.path.startswith("/history/"):
                    pid = self.path.rsplit("/", 1)[-1]
                    return self._json(
                        200,
                        {
                            pid: {
                                "status": {"status_str": "success", "completed": True},
                                "outputs": {
                                    "7": {
                                        "images": [
                                            {
                                                "filename": "personalclaw_00001_.png",
                                                "subfolder": "",
                                                "type": "output",
                                            }
                                        ]
                                    }
                                },
                            }
                        },
                    )
                if self.path.startswith("/view"):
                    return self._send(200, _PNG, "image/png")
                return self._json(404, {"error": "no such path"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                if self.path.startswith("/prompt"):
                    if outer.fail_queue:
                        return self._json(400, {"error": "node validation failed"})
                    outer.graphs.append(json.loads(raw).get("prompt") or {})
                    return self._json(200, {"prompt_id": "pid-1"})
                return self._json(404, {"error": "no such path"})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> _FakeComfy:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def comfy():
    with _FakeComfy(checkpoints=[_INSTALLED]) as srv:
        yield srv


@pytest.fixture
def empty_comfy():
    """Runtime reachable, zero checkpoints pulled — clause 5's exact state."""
    with _FakeComfy(checkpoints=[]) as srv:
        yield srv


@pytest.fixture(autouse=True)
def _no_saved_settings(monkeypatch):
    """Never read the developer's real Settings card during a test."""
    monkeypatch.setattr(app, "_resolve_endpoint", lambda configured="": (
        configured.strip().rstrip("/") if configured.strip() else app._DEFAULT_ENDPOINT
    ))


# ══ Clause 3 — licence discipline (C9) ═══════════════════════════════════════

#: Every trap IG-1 names, with the reason each must be refused. Held here as a
#: literal so the rail is asserted against the atom's own list, not against
#: whatever the catalog happens to contain.
_C9_TRAPS = {
    "flux.1-dev": "non-commercial",
    "sdxl-base-1.0": "OpenRAIL",
    "sd-1.5": "OpenRAIL",
    "sd-3.5-large": "revenue-gated",
    "sdxl-turbo": "non-commercial",
    "sana-1.6b": "Gemma encoder / research-only",
    "janus-pro-7b": "DeepSeek weights licence",
}


class TestClause3LicenceAllowlist:
    def test_allowlist_holds_only_genuinely_permissive_ids(self):
        assert PERMISSIVE_LICENCES == {"apache-2.0", "mit"}

    def test_recommended_model_is_on_the_allowlist(self):
        rec = recommended_model()
        assert rec.name == RECOMMENDED_MODEL
        assert rec.license in PERMISSIVE_LICENCES
        assert not rec.disqualified
        assert is_permissive(rec)

    @pytest.mark.parametrize("name", sorted(_C9_TRAPS))
    def test_every_c9_trap_is_in_the_catalog_and_refused(self, name):
        """Each trap is present AND rejected.

        Presence matters: a trap the catalog simply omits is not evidence of a
        refusal — nothing would notice if the refusal logic were deleted.
        """
        entry = by_name(name)
        assert entry is not None, f"{name} is not in the catalog, so nothing refuses it"
        assert not is_permissive(entry), f"{name} was accepted as permissive"
        assert entry.disqualified, f"{name} carries no recorded reason for its refusal"

    def test_the_licence_tag_alone_would_have_waved_two_traps_through(self):
        """The vacuity proof for ``is_permissive``'s second gate.

        Sana's tag is ``apache-2.0`` and Janus-Pro's is ``mit`` — both ON the
        allowlist. A tag-only check therefore ACCEPTS both. This asserts the naive
        check really would pass them, so the ``disqualified`` gate is load-bearing
        rather than decoration.
        """
        for name in ("sana-1.6b", "janus-pro-7b"):
            entry = by_name(name)
            assert entry is not None
            # the naive check a careless implementation would have written:
            assert entry.license.lower() in PERMISSIVE_LICENCES, (
                f"{name} no longer reproduces the tag-lies trap — reconfirm its card"
            )
            # the real one:
            assert not is_permissive(entry)

    def test_setup_copy_recommends_the_permissive_default_and_names_no_trap(self):
        text = setup_help()
        assert RECOMMENDED_MODEL in text
        for trap in _C9_TRAPS:
            assert trap not in text, f"setup copy names the disqualified {trap}"
        # and it tells the user the weights are theirs to fetch
        assert "does not ship or fetch model weights" in text

    def test_manifest_description_recommends_only_a_permissive_model(self):
        """The manifest is setup copy too — it is what the Store renders."""
        blob = json.dumps(_MANIFEST).lower()
        for trap in _C9_TRAPS:
            assert trap not in blob, f"the manifest names the disqualified {trap}"
        assert RECOMMENDED_MODEL in blob

    # ── the rail's own negative case ──────────────────────────────────────────

    @pytest.mark.parametrize("trap", sorted(_C9_TRAPS))
    def test_the_rail_goes_red_if_a_trap_is_made_the_default(self, monkeypatch, trap):
        """Exercise the failure the clause-3 rail exists to catch.

        Point ``RECOMMENDED_MODEL`` at each disqualified model in turn and assert
        the recommendation path REFUSES rather than shipping it. Without this, the
        rails above only prove that today's default happens to be fine — they
        would not prove they can detect a bad one.
        """
        monkeypatch.setattr(app, "RECOMMENDED_MODEL", trap)
        with pytest.raises(ImageGenError, match="not permissively licensed"):
            recommended_model()
        with pytest.raises(ImageGenError):
            setup_help()

    def test_the_rail_goes_red_on_a_default_that_is_not_in_the_catalog(self, monkeypatch):
        monkeypatch.setattr(app, "RECOMMENDED_MODEL", "some-unvetted-checkpoint")
        with pytest.raises(ImageGenError, match="not in the catalog"):
            recommended_model()


# ══ Clause 4 — no model artifact in the bundle ═══════════════════════════════

#: Extensions that carry model weights. A file with one of these in the bundle is
#: the failure this clause exists to prevent.
_WEIGHT_SUFFIXES = {
    ".safetensors", ".ckpt", ".gguf", ".onnx", ".bin", ".pt", ".pth",
    ".h5", ".pb", ".tflite", ".msgpack", ".npz", ".model",
}

#: The whole bundle is source + docs. 256 KiB is roomy for that and nowhere near
#: any plausible weight file, so it catches a large binary that dodges the suffix
#: list (a weight file named ``weights.dat``).
_BUNDLE_CEILING_BYTES = 256 * 1024

#: Python distributions that would drag weights in as a transitive install.
_WEIGHT_PULLING_DEPS = ("torch", "diffusers", "transformers", "huggingface", "safetensors")


def _bundle_files(root: Path) -> list[Path]:
    return [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
    ]


def model_artifacts_in(root: Path) -> list[str]:
    """Every file under ``root`` that looks like shipped model weights.

    Shared by the real rail and its negative case, so both exercise the SAME
    scanner — a negative case run against a second implementation proves nothing
    about the first.
    """
    found: list[str] = []
    for p in _bundle_files(root):
        if p.suffix.lower() in _WEIGHT_SUFFIXES:
            found.append(f"{p.relative_to(root)} ({p.suffix})")
        elif p.stat().st_size > _BUNDLE_CEILING_BYTES:
            found.append(f"{p.relative_to(root)} ({p.stat().st_size} bytes)")
    return found


class TestClause4NoModelArtifact:
    def test_the_scanner_has_something_to_scan(self):
        """Vacuity floor: a scan of zero files reports zero violations."""
        files = _bundle_files(_APP_DIR)
        assert files, "the bundle scan found no files — the scanner is misrooted"
        assert (_APP_DIR / "provider.py") in files

    def test_no_model_artifact_ships_in_the_bundle(self):
        offenders = model_artifacts_in(_APP_DIR)
        assert not offenders, (
            "the local image bundle ships model weights, which would load a packaging "
            "size budget the model is supposed to stay out of:\n  " + "\n  ".join(offenders)
        )

    def test_the_whole_bundle_is_far_under_any_packaging_budget(self):
        total = sum(p.stat().st_size for p in _bundle_files(_APP_DIR))
        assert total < _BUNDLE_CEILING_BYTES * 4, f"bundle grew to {total} bytes"

    def test_the_bundle_declares_no_weight_pulling_dependency(self):
        """No torch/diffusers: the runtime is the user's ComfyUI, installed by them.

        A declared ``torch`` would put multi-gigabyte wheels into the install path
        even with no checkpoint shipped, which is the same budget problem wearing a
        dependency's clothes.
        """
        declared = json.dumps(_MANIFEST.get("dependencies") or {}).lower()
        for dep in _WEIGHT_PULLING_DEPS:
            assert dep not in declared, f"manifest declares {dep}"

    def test_the_catalog_records_sizes_but_ships_none_of_them(self):
        """Every catalog entry names a footprint, and none of it is in the bundle."""
        assert all(m.approx_gb > 0 for m in catalog()), "a catalog entry declares no size"
        assert sum(p.stat().st_size for p in _bundle_files(_APP_DIR)) < 1_000_000

    # ── the rail's own negative case ──────────────────────────────────────────

    def test_the_rail_detects_a_planted_weight_file(self, tmp_path):
        """Plant the violation and watch the scanner catch it.

        Two plants, because the scanner has two independent detectors and a rail
        with an unexercised branch is half a rail: a weight-suffixed file (caught
        by suffix) and an oversized file with an innocent name (caught by size).
        """
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "provider.py").write_text("# source\n", encoding="utf-8")
        assert model_artifacts_in(bundle) == []

        (bundle / "flux1-schnell.safetensors").write_bytes(b"\x00" * 16)
        offenders = model_artifacts_in(bundle)
        assert any("safetensors" in o for o in offenders), offenders

        (bundle / "flux1-schnell.safetensors").unlink()
        assert model_artifacts_in(bundle) == []

        (bundle / "weights.dat").write_bytes(b"\x00" * (_BUNDLE_CEILING_BYTES + 1))
        offenders = model_artifacts_in(bundle)
        assert any("weights.dat" in o for o in offenders), offenders


# ══ Clause 2 — registers exactly like FAL ════════════════════════════════════

def _core_src() -> Path:
    """The installed core ``personalclaw`` package directory."""
    import personalclaw

    return Path(personalclaw.__file__).resolve().parent


def code_only(src: str) -> str:
    """``src`` with every comment and string literal removed.

    A plain substring scan over a Python file reads its own documentation: the
    first version of the banned-symbol rail below failed because this module's
    docstring *explains* that it must not call ``image_generate``. Dropping
    comments and strings leaves only what the interpreter executes, so the rail
    can no longer be tripped — or satisfied — by prose.
    """
    import io
    import tokenize

    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


class TestClause2RegistersLikeFal:
    def test_manifest_provider_block_matches_the_fal_shape(self):
        """Same seam, same keys: ``type: model`` + ``provider:create_provider`` +
        an ``image_gen`` capability. That trio is what makes core's existing
        ``ModelTypeHandler`` do the registration, with nothing app-specific."""
        prov = _MANIFEST["provider"]
        assert prov["type"] == "model"
        assert prov["implementation"] == "provider:create_provider"
        assert prov["capabilities"] == ["image_gen"]

    def test_the_factory_returns_an_image_gen_provider(self):
        built = create_provider({"endpoint": "http://127.0.0.1:9999"})
        assert isinstance(built, ImageGenProvider)
        assert built.name == "local-image"

    def test_core_model_type_handler_puts_it_in_the_image_gen_registry(self, monkeypatch):
        """Drive core's REAL registration seam, not a stand-in for it.

        ``ModelTypeHandler.register`` is the one place a ``type: model`` app becomes
        a bound capability provider. Running it proves this bundle reaches the
        ``image_gen`` registry by the same route ``fal-image`` does.
        """
        from personalclaw.image_gen import registry as ig_reg
        from personalclaw.providers.registry import ModelTypeHandler

        class _Cfg:
            capabilities = ["image_gen"]
            multiInstance = False

        class _Ext:
            name = "local-image-gen"
            provider_config = _Cfg()

        built = create_provider({"endpoint": "http://127.0.0.1:9999"})
        handler = ModelTypeHandler()
        try:
            handler.register(_Ext(), built)
            assert ig_reg.get_provider("local-image") is built
        finally:
            handler.deregister(_Ext(), built)
        assert ig_reg.get_provider("local-image") is None

    def test_the_app_adds_no_tool_no_route_and_no_audit_call(self):
        """A second generation path would show up as one of these.

        The app is a provider and nothing else: it must not register a tool, mount
        a route, or write its own SEL audit entry — every one of those would be a
        way to generate an image around core's audited dispatch.
        """
        src = code_only((_APP_DIR / "provider.py").read_text(encoding="utf-8"))
        for banned in (
            "log_tool_invocation",
            "register_tool",
            "APIRouter",
            "add_api_route",
            "image_generate",
        ):
            assert banned not in src, f"provider.py references {banned!r} in executable code"
        # ...and it reaches core only through the SDK facade, never a deep module.
        tree = __import__("ast").parse((_APP_DIR / "provider.py").read_text(encoding="utf-8"))
        ast_mod = __import__("ast")
        for node in ast_mod.walk(tree):
            mod = ""
            if isinstance(node, ast_mod.ImportFrom) and node.level == 0:
                mod = node.module or ""
            elif isinstance(node, ast_mod.Import):
                mod = node.names[0].name
            if mod.startswith("personalclaw"):
                assert mod.split(".")[1:2] == ["sdk"], f"deep-core import: {mod}"

    def test_the_code_only_scan_is_not_vacuous(self):
        """The rail above is only as good as this helper.

        Two directions: a banned symbol written in a comment or docstring must NOT
        register, and the same symbol written as real code MUST.
        """
        assert "log_tool_invocation" not in code_only(
            '"""a docstring naming log_tool_invocation"""\n# and a comment: log_tool_invocation\n'
        )
        assert "log_tool_invocation" in code_only("sel().log_tool_invocation(x=1)\n")

    def test_core_still_has_exactly_one_image_generate_dispatch(self):
        """One call site for ``_image_generate``, in the one place that audits it."""
        text = (_core_src() / "mcp_artifacts.py").read_text(encoding="utf-8")
        assert len(re.findall(r"^\s*return _image_generate\(", text, re.M)) == 1
        assert len(re.findall(r"^def _image_generate\(", text, re.M)) == 1

    def test_core_names_no_vendor_string_for_this_backend(self):
        """No core file knows this app exists — that is the whole point of clause 2."""
        offenders = []
        for p in _core_src().rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            body = p.read_text(encoding="utf-8", errors="ignore")
            if "local-image-gen" in body or "LocalComfyImageProvider" in body or "comfyui" in body.lower():
                offenders.append(str(p.relative_to(_core_src())))
        assert not offenders, f"core gained a vendor string for this backend: {offenders}"


# ══ Clause 1 — through the SEL-audited dispatch ══════════════════════════════


@pytest.fixture
def wired(tmp_path, monkeypatch, comfy):
    """Artifact store in tmp + this provider bound through the REAL registry.

    ``active_image_gen`` is NOT patched: the provider is registered for real and
    the binding ref is supplied the way ``active_models.json`` supplies it, so the
    resolution core performs in production is the resolution under test.
    """
    from personalclaw.artifacts import native
    from personalclaw.artifacts import registry as art_reg
    from personalclaw.image_gen import registry as ig_reg
    from personalclaw.providers import use_cases

    store = native.NativeArtifactProvider(root=tmp_path)
    monkeypatch.setattr(art_reg, "get_provider", lambda name="native": store)
    monkeypatch.setattr("personalclaw.mcp_artifacts._resolve_session_key", lambda: None)

    built = create_provider({"endpoint": comfy.endpoint})
    ig_reg.register_provider(built)
    monkeypatch.setattr(
        use_cases, "active_model_refs", lambda uc: ["local-image:flux.1-schnell"]
    )
    try:
        yield store, built, comfy
    finally:
        ig_reg.unregister_provider("local-image")


class TestClause1AuditedDispatch:
    def test_the_real_registry_resolves_this_provider_as_active(self, wired):
        from personalclaw.image_gen.registry import active_image_gen

        _, built, _ = wired
        resolved = active_image_gen()
        assert resolved is not None, "the binding ref did not resolve to this provider"
        assert resolved[0] is built
        assert resolved[1] == "flux.1-schnell"

    def test_image_generate_produces_an_image_artifact_with_no_cloud_provider(self, wired):
        """The clause-1 drive: prompt in, project-scoped image artifact out.

        No cloud image provider is registered, no API key exists anywhere, and no
        model weights are involved — the bytes come off a loopback socket.
        """
        from personalclaw.mcp_artifacts import _call_tool_inner

        store, _, srv = wired
        out = _call_tool_inner("image_generate", {"prompt": "a red bicycle on a wet street"})

        assert "Generated image" in out and "slug:" in out, out
        images = store.list(kind="image")
        assert len(images) == 1
        data, mime = store.raw_bytes(images[0].slug)
        assert data == _PNG
        assert mime == "image/png"
        # and the prompt really reached the runtime's graph
        assert srv.graphs, "the runtime was never asked to render anything"
        assert srv.graphs[0]["2"]["inputs"]["text"] == "a red bicycle on a wet street"
        assert srv.graphs[0]["1"]["inputs"]["ckpt_name"] == _INSTALLED

    def test_the_dispatch_audited_the_invocation(self, wired, monkeypatch):
        """The SEL entry is core's, written by the dispatch this backend routes through."""
        from personalclaw.mcp_artifacts import _call_tool_inner

        logged: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                logged.append(kw)

        monkeypatch.setattr("personalclaw.sel.sel", lambda: _Sel())
        _call_tool_inner("image_generate", {"prompt": "a lighthouse"})

        assert logged, "the generation was not audited"
        assert logged[-1]["tool_name"] == "image_generate"
        assert logged[-1]["outcome"] == "success"
        assert logged[-1]["source"] == "mcp"

    def test_the_artifact_is_reachable_as_a_versioned_image(self, wired):
        from personalclaw.mcp_artifacts import _call_tool_inner

        store, _, _ = wired
        out = _call_tool_inner("image_generate", {"prompt": "a kite", "size": "768x768"})
        slug = re.search(r"slug: ([\w-]+)", out).group(1)
        art = store.get(slug)
        assert art is not None and art.kind == "image" and art.version >= 1
        assert f"/api/artifacts/{slug}/raw?version={art.version}" in out


# ══ Clause 5 — declared but not pulled degrades calmly ═══════════════════════


class TestClause5CalmNoModelState:
    @pytest.mark.asyncio
    async def test_runtime_up_with_no_weights_is_available_but_empty(self, empty_comfy):
        """Available, and every model not-downloaded. Not an error, not a 500."""
        prov = create_provider({"endpoint": empty_comfy.endpoint})
        assert await prov.is_available() is True
        models = await prov.list_models()
        assert models, "the catalog vanished"
        assert all(not m.downloaded for m in models)

    @pytest.mark.asyncio
    async def test_generate_with_no_weights_raises_a_calm_typed_error(self, empty_comfy):
        prov = create_provider({"endpoint": empty_comfy.endpoint})
        with pytest.raises(ImageGenError) as ei:
            await prov.generate("a bicycle", model="flux.1-schnell")
        msg = str(ei.value)
        assert "no model weights yet" in msg
        assert RECOMMENDED_MODEL in msg  # it says what to pull
        assert "does not ship or fetch model weights" in msg

    def test_the_tool_surface_returns_a_message_not_an_exception(
        self, tmp_path, monkeypatch, empty_comfy
    ):
        """The OU-12 invariant at the surface core actually calls.

        ``_image_generate`` catches ``ImageGenError`` and returns its text, so the
        not-pulled state is a sentence in the transcript. This asserts that end of
        it, because a provider raising the right exception into a caller that did
        NOT catch it would still be a 500.
        """
        from personalclaw.artifacts import native
        from personalclaw.artifacts import registry as art_reg
        from personalclaw.image_gen import registry as ig_reg
        from personalclaw.mcp_artifacts import _call_tool_inner
        from personalclaw.providers import use_cases

        store = native.NativeArtifactProvider(root=tmp_path)
        monkeypatch.setattr(art_reg, "get_provider", lambda name="native": store)
        monkeypatch.setattr("personalclaw.mcp_artifacts._resolve_session_key", lambda: None)
        built = create_provider({"endpoint": empty_comfy.endpoint})
        ig_reg.register_provider(built)
        monkeypatch.setattr(
            use_cases, "active_model_refs", lambda uc: ["local-image:flux.1-schnell"]
        )
        try:
            out = _call_tool_inner("image_generate", {"prompt": "a bicycle"})
        finally:
            ig_reg.unregister_provider("local-image")

        assert out.startswith("Error: "), out
        assert "no model weights yet" in out
        assert store.list(kind="image") == []  # nothing half-saved

    @pytest.mark.asyncio
    async def test_runtime_absent_is_unavailable_and_lists_calmly(self):
        """An endpoint with nothing listening: no raise out of either read path."""
        prov = create_provider({"endpoint": "http://127.0.0.1:1"})
        assert await prov.is_available() is False
        models = await prov.list_models()
        assert models and all(not m.downloaded for m in models)

    @pytest.mark.asyncio
    async def test_runtime_absent_generate_names_the_endpoint(self):
        prov = create_provider({"endpoint": "http://127.0.0.1:1"})
        with pytest.raises(ImageGenError, match="not reachable"):
            await prov.generate("a bicycle", model="flux.1-schnell")

    @pytest.mark.asyncio
    async def test_a_disqualified_model_is_still_listed_but_flagged(self, comfy):
        """Listing is not the recommendation gate — C9 binds what we RECOMMEND.

        A user who has already pulled SDXL can still bind it; the description says
        why we would not have suggested it.
        """
        prov = create_provider({"endpoint": comfy.endpoint})
        rows = {m.name: m for m in await prov.list_models()}
        assert "sdxl-base-1.0" in rows
        assert "NOT RECOMMENDED" in rows["sdxl-base-1.0"].description
        assert "NOT RECOMMENDED" not in rows[RECOMMENDED_MODEL].description


# ══ Egress posture (ARCC SSRF guidance) ══════════════════════════════════════


class TestEgressPosture:
    def test_the_policy_is_loopback_only_and_cannot_redirect(self):
        """Pinned so a later widening is a build failure, not a silent change.

        Each flag maps to one control the ARCC SSRF guidance names: host
        allowlisting (``loopback_only``), DNS-rebinding defeat
        (``pin_resolved_ip``), redirect control (``max_redirects``), and
        fail-closed handling (``on_violation``).
        """
        pol = app._LOCAL_ONLY
        assert pol.loopback_only is True
        assert pol.allow_private is True
        assert pol.max_redirects == 0
        assert pol.pin_resolved_ip is True
        assert pol.on_violation == "deny"
        assert set(pol.allow_schemes) <= {"http", "https"}

    @pytest.mark.asyncio
    async def test_a_public_endpoint_is_refused(self):
        """A "local" backend pointed at the internet must not reach it."""
        prov = create_provider({"endpoint": "http://example.com"})
        assert await prov.is_available() is False
        with pytest.raises(ImageGenError) as ei:
            await prov.generate("a bicycle", model="flux.1-schnell")
        assert "not reachable" in str(ei.value) or "Refused" in str(ei.value)

    def test_every_outbound_call_goes_through_the_one_guarded_helper(self):
        """No path may call ``fetch`` directly and skip the policy."""
        src = code_only((_APP_DIR / "provider.py").read_text(encoding="utf-8"))
        # `code_only` normalises to space-separated tokens, so the call reads `fetch (`
        calls = re.findall(r"(?<![\w_])fetch\s*\(", src)
        assert len(calls) == 1, f"expected one fetch() call site, found {len(calls)}"
        # and that one site passes the narrow policy rather than defaulting
        assert "policy = _LOCAL_ONLY" in src


# ══ Graph + size plumbing ════════════════════════════════════════════════════


class TestGraphAndSizes:
    def test_size_parsing_rounds_to_the_samplers_multiple_of_eight(self):
        assert app._parse_size("1024x768") == (1024, 768)
        assert app._parse_size("1023x769") == (1016, 768)
        assert app._parse_size("") == (1024, 1024)
        assert app._parse_size("garbage") == (1024, 1024)

    def test_the_graph_wires_prompt_checkpoint_and_size(self):
        g = app.build_graph(
            checkpoint="c.safetensors", prompt="p", width=512, height=640, steps=4, seed=7
        )
        assert g["1"]["inputs"]["ckpt_name"] == "c.safetensors"
        assert g["2"]["inputs"]["text"] == "p"
        assert g["4"]["inputs"] == {"width": 512, "height": 640, "batch_size": 1}
        assert g["5"]["inputs"]["steps"] == 4 and g["5"]["inputs"]["seed"] == 7
        assert g["7"]["class_type"] == "SaveImage"

    @pytest.mark.asyncio
    async def test_a_rejected_job_reports_the_runtimes_reason(self):
        with _FakeComfy(checkpoints=[_INSTALLED], fail_queue=True) as srv:
            prov = create_provider({"endpoint": srv.endpoint})
            with pytest.raises(ImageGenError, match="rejected the job"):
                await prov.generate("x", model="flux.1-schnell")

    @pytest.mark.asyncio
    async def test_edit_is_declared_unsupported_rather_than_approximated(self, comfy):
        prov = create_provider({"endpoint": comfy.endpoint})
        with pytest.raises(ImageGenError, match="does not support editing"):
            await prov.edit("x", source_image="/tmp/nope.png")
        assert all(not m.supports_edit for m in await prov.list_models())

    @pytest.mark.asyncio
    async def test_an_unknown_installed_checkpoint_is_still_bindable(self):
        with _FakeComfy(checkpoints=["someone_elses_model.safetensors"]) as srv:
            prov = create_provider({"endpoint": srv.endpoint})
            rows = {m.name: m for m in await prov.list_models()}
            assert rows["someone_elses_model.safetensors"].downloaded is True
            assert "licence unknown" in rows["someone_elses_model.safetensors"].description

    def test_provider_identity_is_stable(self):
        prov = LocalComfyImageProvider(endpoint="http://127.0.0.1:8188")
        assert prov.name == "local-image"
        assert prov.display_name == "Local (ComfyUI)"
        assert prov.info()["local"] is True
        assert prov.info()["endpoint"] == "http://127.0.0.1:8188"
