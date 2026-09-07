"""Unit tests for the faster-whisper (local STT) app.

The faster_whisper/CTranslate2 import is lazy (inside download/transcribe), so these
tests exercise the catalog + provider surface without it. The app registers the STT
provider that core's stt registry resolves the ``stt`` use-case to."""

from __future__ import annotations

import asyncio
from pathlib import Path

import provider as prov

from personalclaw.sdk.stt import SttModel, SttProvider


def _run(coro):
    return asyncio.run(coro)


def _seed_snapshot(root: Path, model_name: str) -> Path:
    """Write the minimum a real ``huggingface_hub`` snapshot leaves behind for *model_name*
    under *root*, so presence detection is exercised against a REAL tree rather than a mock."""
    d = prov._repo_dir(root, model_name) / "snapshots" / "00000000"
    d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"\x00" * 16)
    (d / "config.json").write_text("{}")
    return d


def test_create_provider_is_stt_provider():
    p = prov.create_provider({})
    assert isinstance(p, SttProvider)
    assert p.name == "faster_whisper"
    assert p.supports_streaming is True


def test_lists_catalog_models():
    models = _run(prov.create_provider({}).list_models())
    names = {m.name for m in models}
    assert "turbo" in names and "tiny" in names
    for m in models:
        assert isinstance(m, SttModel)
        # app doesn't mark active (core's Settings layer does)
        assert m.active is False


def test_cache_dir_exposed():
    assert prov.create_provider({}).cache_dir()  # non-empty path


# ── issue #93: weights must be rooted at PERSONALCLAW_HOME, without re-downloading ──


def test_write_root_is_under_personalclaw_home(monkeypatch, tmp_path):
    """The WRITE target is PERSONALCLAW_HOME-rooted, so an isolated home is isolated.
    Asserted on the RESOLVED path (not a mock call): setting PERSONALCLAW_HOME must move
    the tree, and no part of it may sit in the machine-wide HuggingFace cache."""
    home = tmp_path / "pclaw-home"
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    root = prov._models_dir()
    assert home in root.parents, f"{root} is not under PERSONALCLAW_HOME {home}"
    assert prov._legacy_dir() not in root.parents and root != prov._legacy_dir()
    assert ".cache" not in root.parts
    assert Path(prov.create_provider({}).cache_dir()) == root


def test_write_root_defaults_to_dot_personalclaw_not_dot_cache(monkeypatch):
    """Unset, the root defaults exactly the way the three sibling bundles spell it —
    ``Path.home()/".personalclaw"`` — NOT the host's ``~/.cache``, which is what this app
    used to fall back to and is why an isolated home leaked."""
    monkeypatch.delenv("PERSONALCLAW_HOME", raising=False)
    root = prov._models_dir()
    assert Path.home() / ".personalclaw" in root.parents
    assert Path.home() / ".cache" not in root.parents


def test_legacy_weights_report_downloaded_and_fetch_nothing(monkeypatch, tmp_path):
    """Upgrade path: weights already in the legacy machine-wide cache report as downloaded
    even though the new PERSONALCLAW_HOME root is EMPTY — so the UI never invites a
    multi-GB re-fetch. Presence checking must not construct a WhisperModel (that is what
    would download), so the stub explodes if it is touched."""
    home = tmp_path / "pclaw-home"
    home.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _seed_snapshot(prov._legacy_dir(), "small")
    assert not prov._models_dir().exists()  # nothing at the new root

    import faster_whisper

    def _boom(*a, **k):
        raise AssertionError("presence check constructed a WhisperModel — that downloads")

    monkeypatch.setattr(faster_whisper, "WhisperModel", _boom, raising=False)

    assert prov._model_downloaded("small") is True
    models = _run(prov.create_provider({}).list_models())
    assert next(m for m in models if m.name == "small").downloaded is True
    # still nothing written to the new root: read-through, never a migration copy
    assert not prov._models_dir().exists()


def test_loader_reads_through_legacy_root_without_copying(monkeypatch, tmp_path):
    """The read-through is the mechanism that makes the upgrade free: with weights only in
    the legacy cache, the LOADER is handed the legacy root (so huggingface_hub resolves the
    snapshot locally and fetches nothing) and the new root stays untouched."""
    home = tmp_path / "pclaw-home"
    home.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _seed_snapshot(prov._legacy_dir(), "small")

    captured = {}

    class _StubWord:
        start = 0.0; end = 0.5; word = "hi"; probability = 0.9

    class _StubSeg:
        start = 0.0; end = 0.5; text = "hi"; words = [_StubWord()]

    class _StubModel:
        def __init__(self, *a, **k):
            captured["download_root"] = k.get("download_root")

        def transcribe(self, path, **kwargs):
            class _Info: language = "en"; duration = 0.5
            return iter([_StubSeg()]), _Info()

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)

    r = _run(prov.create_provider({}).transcribe_detailed("/tmp/x.wav", model="small"))
    assert r is not None and r.text
    assert prov._weights_root("small") == prov._legacy_dir()
    assert captured["download_root"] == str(prov._legacy_dir())
    assert not prov._repo_dir(prov._models_dir(), "small").exists()  # no copy


def test_cache_dir_is_the_dir_a_fresh_download_fills(monkeypatch, tmp_path):
    """cache_dir() is what core's download UI reads for byte progress, so it must be the
    directory that ACTUALLY fills. Fresh case: captured from the root the loader is handed."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "pclaw-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    captured = {}

    class _StubModel:
        def __init__(self, *a, **k):
            captured["download_root"] = k.get("download_root")

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)

    p = prov.create_provider({})
    reported = p.cache_dir()
    assert _run(p.download_model("small")) is True
    assert captured["download_root"] == reported
    assert Path(reported).is_dir()  # the download really created the tree it reports


def test_cache_dir_tracks_new_root_even_when_legacy_holds_weights(monkeypatch, tmp_path):
    """Presence-reporting and progress-reporting must not disagree. Legacy weights make
    presence TRUE, but a deliberate download still fills the NEW root — so cache_dir() must
    keep pointing there, not at the legacy tree that will never grow."""
    home = tmp_path / "pclaw-home"
    home.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _seed_snapshot(prov._legacy_dir(), "small")
    assert prov._model_downloaded("small") is True  # presence: yes, from legacy

    captured = {}

    class _StubModel:
        def __init__(self, *a, **k):
            captured["download_root"] = k.get("download_root")

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)

    p = prov.create_provider({})
    assert _run(p.download_model("small")) is True
    assert captured["download_root"] == p.cache_dir() == str(prov._models_dir())
    assert captured["download_root"] != str(prov._legacy_dir())


def test_partial_snapshot_is_not_weights(monkeypatch, tmp_path):
    """A bare ``models--…`` shell is what an interrupted download leaves behind. Counting it
    as present is how the legacy fallback silently resolves to an unusable tree."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "pclaw-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    prov._repo_dir(prov._legacy_dir(), "small").mkdir(parents=True)
    assert prov._model_downloaded("small") is False


def test_download_unknown_model_false():
    assert _run(prov.create_provider({}).download_model("no-such-model")) is False


def test_bias_prompt_capped_and_single_lever(monkeypatch):
    """Regression: a large Lexicon (many bias terms) must NOT overflow Whisper's
    224-token prompt window. The bias string is capped ~200 chars AND passed through
    exactly ONE lever (hotwords OR initial_prompt, never both) — else the decoder raises
    'No position encodings ... >= 448' and the whole transcription silently returns None
    (empty transcript on audio ingestion). Captures the transcribe() kwargs via a stub."""
    captured = {}

    class _StubWord:
        start = 0.0; end = 0.5; word = "hi"; probability = 0.9

    class _StubSeg:
        start = 0.0; end = 0.5; text = "hi"; words = [_StubWord()]

    class _StubModel:
        def __init__(self, *a, **k): pass
        def transcribe(self, path, **kwargs):
            captured.update(kwargs)
            class _Info: language = "en"; duration = 0.5
            return iter([_StubSeg()]), _Info()

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)

    huge_bias = [f"Term Number {i} With Some Length" for i in range(80)]  # ~2000 chars raw
    r = _run(prov.create_provider({}).transcribe_detailed("/tmp/x.wav", bias_terms=huge_bias))
    assert r is not None and r.text  # did NOT silently fail
    # exactly one bias lever, and it's short (well under the 224-token window)
    levers = [k for k in ("hotwords", "initial_prompt") if k in captured]
    assert len(levers) == 1, f"expected ONE bias lever, got {levers}"
    assert len(captured[levers[0]]) <= 200


def test_availability_reason_without_faster_whisper(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _no_fw(name, *a, **k):
        if name == "faster_whisper":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_fw)
    ok, reason = prov.availability()
    assert ok is False and "stt" in reason.lower()
