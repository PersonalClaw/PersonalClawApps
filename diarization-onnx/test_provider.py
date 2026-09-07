"""Tests for the ONNX diarization provider — catalog + gating + (when deps present) real diarize."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

import provider as P


def _seed_pair(root: Path) -> None:
    """Write the ONNX pair a real download leaves behind under *root*, so presence
    detection is exercised against a REAL tree rather than a mock."""
    seg = root / P._SEG_REL
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_bytes(b"\x00" * 16)
    (root / P._EMB_REL).write_bytes(b"\x00" * 16)


def _fake_urlretrieve(url, dest):
    """Stand in for the network. The segmentation URL yields a REAL ``.tar.bz2`` whose one
    member is the model, so ``download_model`` runs its actual extract path into whatever
    root it chose — the point of the cache_dir() assertions below."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if url == P._SEG_URL:
        payload = b"\x00" * 16
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:bz2") as tf:
            info = tarfile.TarInfo(str(P._SEG_REL))
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        dest.write_bytes(buf.getvalue())
    else:
        dest.write_bytes(b"\x00" * 16)


def test_create_provider():
    p = P.create_provider({})
    assert p.name == "diarization-onnx" and p.display_name


@pytest.mark.asyncio
async def test_catalog_single_nongated_model():
    models = await P.create_provider({}).list_models()
    assert len(models) == 1
    assert models[0].name == P._MODEL and models[0].gated is False


@pytest.mark.asyncio
async def test_diarize_none_without_model(monkeypatch, tmp_path):
    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    monkeypatch.setattr(P, "_downloaded", lambda: False)
    assert await P.create_provider({}).diarize(str(f)) is None


def test_cache_dir_exposed():
    assert P.create_provider({}).cache_dir()  # for download byte-progress


# ── issue #93: weights must be rooted at PERSONALCLAW_HOME, without re-downloading ──


def test_write_root_is_under_personalclaw_home(monkeypatch, tmp_path):
    """The WRITE target is PERSONALCLAW_HOME-rooted, so an isolated home is isolated.
    Asserted on the RESOLVED path (not a mock call): setting PERSONALCLAW_HOME must move
    the tree, and no part of it may sit in the machine-wide XDG cache."""
    home = tmp_path / "pclaw-home"
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    root = P._models_dir()
    assert home in root.parents, f"{root} is not under PERSONALCLAW_HOME {home}"
    assert P._legacy_dir() not in root.parents and root != P._legacy_dir()
    assert ".cache" not in root.parts
    assert Path(P.create_provider({}).cache_dir()) == root


def test_write_root_defaults_to_dot_personalclaw_not_dot_cache(monkeypatch):
    """Unset, the root defaults exactly the way the three sibling bundles spell it —
    ``Path.home()/".personalclaw"`` — NOT the host's ``~/.cache``, which is what this app
    used to fall back to and is why an isolated home leaked."""
    monkeypatch.delenv("PERSONALCLAW_HOME", raising=False)
    root = P._models_dir()
    assert Path.home() / ".personalclaw" in root.parents
    assert Path.home() / ".cache" not in root.parents


@pytest.mark.asyncio
async def test_legacy_weights_report_downloaded_and_fetch_nothing(monkeypatch, tmp_path):
    """Upgrade path: a pair already in the legacy machine-wide cache reports as downloaded
    even though the new PERSONALCLAW_HOME root is EMPTY — so the UI never invites a re-fetch.
    Presence checking must not hit the network, so urlretrieve explodes if it is touched."""
    home = tmp_path / "pclaw-home"
    home.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _seed_pair(P._legacy_dir())
    assert not P._models_dir().exists()  # nothing at the new root

    def _boom(*a, **k):
        raise AssertionError("presence check hit the network")

    monkeypatch.setattr(P.urllib.request, "urlretrieve", _boom)

    assert P._downloaded() is True
    models = await P.create_provider({}).list_models()
    assert models[0].downloaded is True
    # still nothing written to the new root: read-through, never a migration copy
    assert not P._models_dir().exists()


@pytest.mark.asyncio
async def test_diarize_reads_through_legacy_root_without_copying(monkeypatch, tmp_path):
    """The read-through is the mechanism that makes the upgrade free: with the pair only in
    the legacy cache, the PIPELINE is handed the legacy paths and the new root stays
    untouched. sherpa-onnx is stubbed into sys.modules (the repo's vendor-SDK pattern)."""
    import sys
    import types

    home = tmp_path / "pclaw-home"
    home.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _seed_pair(P._legacy_dir())

    seen = {}

    def _record(key):
        def _factory(**kwargs):
            seen[key] = kwargs.get("model")
            return object()
        return _factory

    fake = types.ModuleType("sherpa_onnx")
    fake.FastClusteringConfig = lambda **k: object()
    fake.OfflineSpeakerSegmentationPyannoteModelConfig = _record("seg")
    fake.OfflineSpeakerSegmentationModelConfig = lambda **k: object()
    fake.SpeakerEmbeddingExtractorConfig = _record("emb")
    fake.OfflineSpeakerDiarizationConfig = lambda **k: object()

    class _Sd:
        def __init__(self, cfg): pass
        def process(self, samples):
            class _R:
                def sort_by_start_time(self):
                    return []
            return _R()

    fake.OfflineSpeakerDiarization = _Sd
    fake_sf = types.ModuleType("soundfile")
    fake_sf.read = lambda p, dtype="float32", always_2d=False: ([0.0] * 8, 16000)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)

    f = tmp_path / "a.wav"
    f.write_bytes(b"\x00" * 32)
    assert await P.create_provider({}).diarize(str(f)) == []

    assert P._weights_root() == P._legacy_dir()
    assert seen["seg"] == str(P._legacy_dir() / P._SEG_REL)
    assert seen["emb"] == str(P._legacy_dir() / P._EMB_REL)
    assert not P._models_dir().exists()  # no copy


@pytest.mark.asyncio
async def test_cache_dir_is_the_dir_a_fresh_download_fills(monkeypatch, tmp_path):
    """cache_dir() is what core's download UI reads for byte progress, so it must be the
    directory that ACTUALLY fills. Fresh case, asserted by running the real download path
    against a stubbed network and then checking the REPORTED dir now holds the weights."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "pclaw-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(P.urllib.request, "urlretrieve", _fake_urlretrieve)

    p = P.create_provider({})
    reported = Path(p.cache_dir())
    assert await p.download_model(P._MODEL) is True
    assert P._has_weights(reported), f"cache_dir() {reported} did not fill"


@pytest.mark.asyncio
async def test_cache_dir_tracks_new_root_even_when_legacy_holds_weights(monkeypatch, tmp_path):
    """Presence-reporting and progress-reporting must not disagree. A legacy pair makes
    presence TRUE, but a deliberate download still fills the NEW root — so cache_dir() must
    keep pointing there, not at the legacy tree that will never grow."""
    home = tmp_path / "pclaw-home"
    home.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _seed_pair(P._legacy_dir())
    assert P._downloaded() is True  # presence: yes, from legacy
    monkeypatch.setattr(P.urllib.request, "urlretrieve", _fake_urlretrieve)

    p = P.create_provider({})
    reported = Path(p.cache_dir())
    assert reported == P._models_dir() and reported != P._legacy_dir()
    assert await p.download_model(P._MODEL) is True
    assert P._has_weights(reported), f"cache_dir() {reported} did not fill"


def test_half_a_pair_is_not_downloaded(monkeypatch, tmp_path):
    """Segmentation without the embedding model cannot diarize. Counting a half-populated
    root as present is how the legacy fallback silently resolves to an unusable tree."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "pclaw-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    seg = P._legacy_dir() / P._SEG_REL
    seg.parent.mkdir(parents=True)
    seg.write_bytes(b"\x00" * 16)
    assert P._downloaded() is False
