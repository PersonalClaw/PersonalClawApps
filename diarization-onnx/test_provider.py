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


def test_asking_whether_it_can_run_loads_no_library(monkeypatch):
    """Binding the diarization model asks this, and asking used to import onnxruntime, sherpa-onnx
    and soundfile. onnxruntime runs nothing here (sherpa-onnx carries an ONNX Runtime of its own),
    and loading it started its maker's telemetry: a device identifier left in the user's home."""
    import sys

    runtimes = ("onnxruntime", "sherpa_onnx", "soundfile")
    for name in runtimes:
        monkeypatch.delitem(sys.modules, name, raising=False)

    P.availability()

    assert [name for name in runtimes if name in sys.modules] == [], "the check loaded a library"


def test_a_missing_package_is_named_with_the_way_to_get_it(monkeypatch):
    """Found missing without importing anything (a module ``None`` in ``sys.modules`` is one the
    import system reports absent). The packages ship with this app, so the fix is its reinstall."""
    import importlib.machinery
    import sys
    import types

    present = types.ModuleType("soundfile")
    present.__spec__ = importlib.machinery.ModuleSpec("soundfile", None)
    monkeypatch.setitem(sys.modules, "soundfile", present)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)

    assert P.availability() == (
        False,
        "ONNX diarization needs sherpa-onnx, which ships with this app, not with PersonalClaw "
        "itself. Reinstall Diarization (ONNX) from the Store.",
    )

    monkeypatch.setitem(sys.modules, "soundfile", None)
    assert P.availability()[1].startswith("ONNX diarization needs sherpa-onnx and soundfile, which ship")


def test_it_declares_no_runtime_it_does_not_run():
    """The install's consent lists what the app installs: onnxruntime is not among what it runs."""
    import json

    manifest = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))

    declared = [d.split(">")[0].split("=")[0] for d in manifest["dependencies"]["pythonDependencies"]]
    assert "onnxruntime" not in declared, declared
    assert {"sherpa-onnx", "soundfile"} <= set(declared), declared


@pytest.mark.asyncio
async def test_catalog_single_nongated_model():
    models = await P.create_provider({}).list_models()
    assert len(models) == 1
    assert models[0].name == P._MODEL and models[0].gated is False


@pytest.mark.asyncio
async def test_diarize_none_without_model(monkeypatch, tmp_path):
    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    monkeypatch.setattr(P, "_downloaded", lambda: False)
    assert await P.create_provider({}).diarize(str(f), model=P._MODEL) is None


@pytest.mark.asyncio
async def test_a_model_this_app_does_not_have_is_refused_before_anything_runs(
    monkeypatch, tmp_path, caplog
):
    """A call naming another model is refused, saying which model this app has, and nothing
    runs: the app used to run its one model in place of the one the call named."""
    import logging

    ran: list[str] = []
    monkeypatch.setattr(P, "ensure_ffmpeg_in_path", lambda: ran.append("ffmpeg"))
    monkeypatch.setattr(P, "_downloaded", lambda: ran.append("weights") or False)
    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    with caplog.at_level(logging.WARNING):
        result = await P.create_provider({}).diarize(str(f), model="fake-other-diarizer")
    assert result is None
    assert ran == []
    assert "fake-other-diarizer" in caplog.text and P._MODEL in caplog.text
    # The control: its own model gets past the same point.
    await P.create_provider({}).diarize(str(f), model=P._MODEL)
    assert ran[0] == "ffmpeg"


def test_cache_dir_exposed():
    assert P.create_provider({}).cache_dir()  # for download byte-progress


# ── the pair lives in the PersonalClaw home; the old cache outside it is left alone ──


def _home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "pclaw-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    return home


def _old_cache(tmp_path) -> Path:
    """Where earlier releases put the pair: outside the home, in the machine-wide XDG cache."""
    return tmp_path / "xdg" / "personalclaw" / "diarization-onnx"


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) if root.exists() else []


def test_write_root_is_under_personalclaw_home(monkeypatch, tmp_path):
    """The pair is written to and read from the PersonalClaw home, so an isolated home is
    isolated. Asserted on the RESOLVED path (not a mock call)."""
    home = _home(monkeypatch, tmp_path)
    root = P._models_dir()
    assert home in root.parents, f"{root} is not under PERSONALCLAW_HOME {home}"
    assert ".cache" not in root.parts and (tmp_path / "xdg") not in root.parents
    assert Path(P.create_provider({}).cache_dir()) == root


def test_write_root_defaults_to_dot_personalclaw_not_dot_cache(monkeypatch, tmp_path):
    """Unset, the root is the default home, ``~/.personalclaw``, NOT the host's ``~/.cache``,
    which is what this app used to fall back to and is why an isolated home leaked. The ``~`` is
    this test's own: core makes the home as it resolves it, and this made the real one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PERSONALCLAW_HOME", raising=False)
    root = P._models_dir()
    assert Path.home() == tmp_path, "control: the default home is resolved under this HOME"
    assert Path.home() / ".personalclaw" in root.parents
    assert Path.home() / ".cache" not in root.parents


@pytest.mark.asyncio
async def test_a_pair_left_in_the_old_cache_outside_the_home_is_not_read(monkeypatch, tmp_path):
    """The old cache is outside the home, so a pair there does not count: the model reads as
    not downloaded (one 47 MB download puts it in the home), and presence checking reaches
    neither that folder's files nor the network."""
    _home(monkeypatch, tmp_path)
    _seed_pair(_old_cache(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("presence check hit the network")

    monkeypatch.setattr(P.urllib.request, "urlretrieve", _boom)
    assert P._downloaded() is False
    models = await P.create_provider({}).list_models()
    assert models[0].downloaded is False


@pytest.mark.asyncio
async def test_diarize_reads_the_pair_from_the_home(monkeypatch, tmp_path):
    """The PIPELINE is handed the home's paths. sherpa-onnx is stubbed into sys.modules (the
    repo's vendor-SDK pattern)."""
    import sys
    import types

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())

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
    assert await P.create_provider({}).diarize(str(f), model=P._MODEL) == []
    assert seen["seg"] == str(P._models_dir() / P._SEG_REL)
    assert seen["emb"] == str(P._models_dir() / P._EMB_REL)


@pytest.mark.asyncio
async def test_cache_dir_is_the_dir_a_fresh_download_fills(monkeypatch, tmp_path):
    """cache_dir() is what core's download UI reads for byte progress, so it must be the
    directory that ACTUALLY fills. Asserted by running the real download path against a
    stubbed network and then checking the REPORTED dir now holds the weights."""
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(P.urllib.request, "urlretrieve", _fake_urlretrieve)

    p = P.create_provider({})
    reported = Path(p.cache_dir())
    assert await p.download_model(P._MODEL) is True
    assert P._has_weights(reported), f"cache_dir() {reported} did not fill"


@pytest.mark.asyncio
async def test_delete_removes_the_home_pair_and_never_the_old_cache(monkeypatch, tmp_path):
    """Delete removes what is in the home. The old cache outside it is left exactly as it
    was: it is not PersonalClaw's to delete any more."""
    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    _seed_pair(_old_cache(tmp_path))
    before = _files(tmp_path / "xdg")

    p = P.create_provider({})
    assert await p.delete_model(P._MODEL) is True
    assert not P._models_dir().exists()
    assert _files(tmp_path / "xdg") == before
    assert await p.delete_model(P._MODEL) is False  # nothing left in the home
    assert _files(tmp_path / "xdg") == before


def test_half_a_pair_is_not_downloaded(monkeypatch, tmp_path):
    """Segmentation without the embedding model cannot diarize. Counting a half-populated
    root as present would resolve to an unusable tree."""
    _home(monkeypatch, tmp_path)
    seg = P._models_dir() / P._SEG_REL
    seg.parent.mkdir(parents=True)
    seg.write_bytes(b"\x00" * 16)
    assert P._downloaded() is False


def test_a_media_call_that_names_no_model_is_refused_and_sends_nothing():
    """A diarization names its model (the diarization binding in Settings → Models). One that
    names none is refused with the SDK's sentence before the model loads: it used to run with
    this app's one model in its place."""
    import asyncio

    from apps_testkit.model_wire import (
        media_adapters,
        media_refusal_expected,
        media_refusal_report,
    )

    adapters = media_adapters(Path(__file__).parent, P.create_provider)
    report = asyncio.run(media_refusal_report(adapters))
    assert report == media_refusal_expected(adapters)
