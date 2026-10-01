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

    runtimes = ("onnxruntime", "sherpa_onnx", "numpy", "soundfile")
    for name in runtimes:
        monkeypatch.delitem(sys.modules, name, raising=False)

    P.availability()

    assert [name for name in runtimes if name in sys.modules] == [], "the check loaded a library"


def test_a_missing_package_is_named_with_the_way_to_get_it(monkeypatch):
    """Found missing without importing anything (a module ``None`` in ``sys.modules`` is one the
    import system reports absent). The packages ship with this app, so the fix is its reinstall."""
    import sys

    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)

    assert P.availability() == (
        False,
        "ONNX diarization needs sherpa-onnx, which ships with this app, not with PersonalClaw "
        "itself. Reinstall Diarization (ONNX) from the Store.",
    )

    monkeypatch.setitem(sys.modules, "numpy", None)
    assert P.availability()[1].startswith("ONNX diarization needs sherpa-onnx and numpy, which ship")


def test_without_ffmpeg_it_says_it_cannot_read_recordings(monkeypatch):
    """ffmpeg decodes every recording now, so a machine without it cannot diarize anything, and
    the Models page says so before a single recording fails: where PersonalClaw looked for it,
    and what to do."""
    monkeypatch.setattr(P, "missing_modules", lambda *modules: [])  # both packages installed
    monkeypatch.setattr(P, "find_ffmpeg", lambda: None)
    ok, reason = P.availability()
    assert (ok, reason) == (False, P._needs_ffmpeg())
    assert reason.startswith(
        "ONNX diarization needs ffmpeg to read recordings. ffmpeg isn't installed where "
        "PersonalClaw looks for it: the folders on the PATH the gateway started with"
    )


def test_it_declares_no_runtime_it_does_not_run():
    """The install's consent lists what the app installs: onnxruntime is not among what it runs,
    and neither is soundfile, since ffmpeg reads the recordings."""
    import json

    manifest = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))

    declared = [d.split(">")[0].split("=")[0] for d in manifest["dependencies"]["pythonDependencies"]]
    assert "onnxruntime" not in declared and "soundfile" not in declared, declared
    assert {"sherpa-onnx", "numpy"} <= set(declared), declared


@pytest.mark.asyncio
async def test_catalog_single_nongated_model():
    models = await P.create_provider({}).list_models()
    assert len(models) == 1
    assert models[0].name == P._MODEL and models[0].gated is False


@pytest.mark.asyncio
async def test_a_model_that_is_not_downloaded_says_so(monkeypatch, tmp_path):
    """🔴 Red before: ``None``, read as a recording with no speakers."""
    from personalclaw.sdk.diarization import DiarizationError

    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    monkeypatch.setattr(P, "_downloaded", lambda: False)
    with pytest.raises(DiarizationError, match="isn't downloaded. Download it under Speaker"):
        await P.create_provider({}).diarize(str(f), model=P._MODEL)


@pytest.mark.asyncio
async def test_a_model_this_app_does_not_have_is_refused_before_anything_runs(
    monkeypatch, tmp_path, caplog
):
    """A call naming another model is refused, saying which model this app has, and nothing
    runs: the app used to run its one model in place of the one the call named."""
    import logging

    ran: list[str] = []
    monkeypatch.setattr(P, "find_ffmpeg", lambda: ran.append("ffmpeg"))
    monkeypatch.setattr(P, "_downloaded", lambda: ran.append("weights") or False)
    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    with caplog.at_level(logging.WARNING):
        result = await P.create_provider({}).diarize(str(f), model="fake-other-diarizer")
    assert result is None
    assert ran == []
    assert "fake-other-diarizer" in caplog.text and P._MODEL in caplog.text
    # The control: its own model gets past the same point (and then says it isn't downloaded).
    from personalclaw.sdk.diarization import DiarizationError

    with pytest.raises(DiarizationError):
        await P.create_provider({}).diarize(str(f), model=P._MODEL)
    assert ran == ["weights"]


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


class _Segment:
    def __init__(self, start, end, speaker):
        self.start, self.end, self.speaker = start, end, speaker


def _fake_sherpa(monkeypatch, *, turns=(), fail: Exception | None = None, seen=None):
    """sherpa-onnx stubbed into sys.modules (the repo's vendor-SDK pattern): records the model
    paths it is given and the samples it is handed, and answers *turns* (or raises *fail*)."""
    import sys
    import types

    seen = {} if seen is None else seen

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
        sample_rate = 16000

        def __init__(self, cfg):
            pass

        def process(self, samples):
            seen["samples"] = samples
            if fail is not None:
                raise fail

            class _R:
                def sort_by_start_time(self):
                    return list(turns)
            return _R()

    fake.OfflineSpeakerDiarization = _Sd
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    return seen


@pytest.mark.asyncio
async def test_diarize_reads_the_pair_from_the_home(monkeypatch, tmp_path):
    """The PIPELINE is handed the home's paths."""
    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    seen = _fake_sherpa(monkeypatch)
    monkeypatch.setattr(P, "_decode", lambda path, rate: [0.0] * 8)

    f = tmp_path / "a.wav"
    f.write_bytes(b"\x00" * 32)
    assert await P.create_provider({}).diarize(str(f), model=P._MODEL) == []
    assert seen["seg"] == str(P._models_dir() / P._SEG_REL)
    assert seen["emb"] == str(P._models_dir() / P._EMB_REL)


@pytest.mark.asyncio
async def test_the_model_hears_the_recording_decoded_at_its_own_rate(monkeypatch, tmp_path):
    """Every recording goes through ffmpeg, mono, at the model's sample rate, as float32: the
    samples the pipeline is handed are exactly what ffmpeg wrote. Turns come back labelled."""
    import numpy as np

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    seen = _fake_sherpa(monkeypatch, turns=[_Segment(0.0, 4.2, 0), _Segment(4.4, 9.1, 1)])
    pcm = np.linspace(-0.5, 0.5, 64, dtype=np.float32)
    argv: list = []

    def _run(cmd, **kwargs):
        argv.extend(cmd)
        return P.subprocess.CompletedProcess(cmd, 0, stdout=pcm.tobytes(), stderr=b"")

    monkeypatch.setattr(P, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(P.subprocess, "run", _run)
    memo = tmp_path / "team call.m4a"
    memo.write_bytes(b"\x00" * 32)

    turns = await P.create_provider({}).diarize(str(memo), model=P._MODEL)

    assert [(t.start, t.end, t.speaker) for t in turns] == [
        (0.0, 4.2, "SPEAKER_00"), (4.4, 9.1, "SPEAKER_01"),
    ]
    assert argv[0] == "/usr/bin/ffmpeg"
    assert argv[argv.index("-i") + 1] == str(memo)
    assert argv[argv.index("-ar") + 1] == "16000" and argv[argv.index("-ac") + 1] == "1"
    assert argv[argv.index("-f") + 1] == "f32le"
    assert np.array_equal(seen["samples"], pcm)


def _ffmpeg_or_skip() -> str:
    import shutil

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("needs ffmpeg to build an AAC recording")
    return ffmpeg


def test_an_aac_voice_memo_is_read_where_soundfile_could_not(tmp_path):
    """🔴 A two-voice voice memo was an ``.m4a`` (AAC at 22.05 kHz). soundfile's libsndfile cannot
    open AAC, so the app failed before diarizing and answered "no speakers". Built here with
    ffmpeg; soundfile's refusal of the same file is the control that it is the failing kind."""
    import subprocess

    import soundfile

    ffmpeg = _ffmpeg_or_skip()
    memo = tmp_path / "snippet.m4a"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=22050:duration=1",
         "-c:a", "aac", "-map_metadata", "-1", str(memo)],
        check=True,
    )
    with pytest.raises(Exception, match="Format not recognised"):
        soundfile.read(str(memo))

    samples = P._decode(str(memo), 16000)

    assert samples.dtype.name == "float32"
    assert 15_500 <= len(samples) <= 16_600, len(samples)  # one second at 16 kHz, not 22,050
    assert float(abs(samples).max()) > 0.1  # the tone, not silence


def test_a_file_ffmpeg_cannot_read_says_why(tmp_path):
    from personalclaw.sdk.diarization import DiarizationError

    _ffmpeg_or_skip()
    junk = tmp_path / "not audio.m4a"
    junk.write_text("this is a text file")
    with pytest.raises(DiarizationError) as raised:
        P._decode(str(junk), 16000)
    said = str(raised.value)
    assert said.startswith("ONNX diarization could not read this recording. Details: ")
    assert "[in#" not in said and str(tmp_path) not in said and "\n" not in said, said


def test_a_recording_cut_off_after_its_header_is_not_a_recording_with_no_speakers(tmp_path):
    """🔴 Red before: a voice memo whose upload stopped after the file's header (it declares 24
    seconds of audio and holds none) decoded to nothing, ffmpeg exited 0 with a "partial file"
    warning, and diarizing nothing reported "done" with no speakers."""
    import subprocess

    from personalclaw.sdk.diarization import DiarizationError

    ffmpeg = _ffmpeg_or_skip()
    memo = tmp_path / "memo.m4a"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=22050:duration=3",
         "-c:a", "aac", "-map_metadata", "-1", "-movflags", "+faststart", str(memo)],
        check=True,
    )
    whole = memo.read_bytes()
    cut = tmp_path / "cut off memo.m4a"
    cut.write_bytes(whole[: whole.index(b"mdat") + 4])  # the header, and none of the audio

    assert len(P._decode(str(memo), 16000)) > 0  # the control: the whole memo is read
    with pytest.raises(DiarizationError) as raised:
        P._decode(str(cut), 16000)
    assert str(raised.value) == P._NO_AUDIO


def test_ffmpegs_closing_line_is_the_detail():
    stderr = (b"[in#0 @ 0x76e5020000] moov atom not found\n"
              b"[in#0 @ 0x76e4c14000] Error opening input: Invalid data found when processing input\n"
              b"Error opening input file /srv/knowledge/files/0b1c.m4a.\n"
              b"Error opening input files: Invalid data found when processing input\n")
    assert P._ffmpeg_said(stderr, 1) == "Error opening input files: Invalid data found when processing input"
    assert P._ffmpeg_said(b"[aac @ 0x1] Too many bits\n", 1) == "Too many bits"
    assert P._ffmpeg_said(b"", 183) == "ffmpeg exited with status 183"


@pytest.mark.asyncio
async def test_a_pipeline_that_fails_says_why(monkeypatch, tmp_path):
    """🔴 Red before: swallowed into ``None``, with no log line."""
    from personalclaw.sdk.diarization import DiarizationError

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    _fake_sherpa(monkeypatch, fail=RuntimeError("Expected samples rate: 16000, given: 22050"))
    monkeypatch.setattr(P, "_decode", lambda path, rate: [0.0] * 8)
    with pytest.raises(DiarizationError) as raised:
        await P.create_provider({}).diarize(str(tmp_path / "a.wav"), model=P._MODEL)
    assert str(raised.value) == (
        "ONNX diarization could not tell the speakers apart in this recording. Details: "
        "Expected samples rate: 16000, given: 22050"
    )


@pytest.mark.asyncio
async def test_a_long_recording_is_not_cut_off_by_a_clock_of_its_own(monkeypatch, tmp_path):
    """🔴 Red before: a fixed ten-minute ``wait_for`` answered ``None`` when it fired, whatever
    the caller's budget. Here every ``wait_for`` gives up at once: the provider must not use one."""
    import asyncio

    async def _out_of_time(awaitable, timeout):
        if hasattr(awaitable, "cancel"):
            awaitable.cancel()
        raise asyncio.TimeoutError

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    _fake_sherpa(monkeypatch, turns=[_Segment(0.0, 1.0, 0)])
    monkeypatch.setattr(P, "_decode", lambda path, rate: [0.0] * 8)
    monkeypatch.setattr(asyncio, "wait_for", _out_of_time)
    turns = await P.create_provider({}).diarize(str(tmp_path / "a.wav"), model=P._MODEL)
    assert [t.speaker for t in turns] == ["SPEAKER_00"]


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


@pytest.mark.asyncio
async def test_a_diarization_runs_the_ffmpeg_on_its_path_and_leaves_the_path_alone(
    monkeypatch, tmp_path
):
    """🔴 Red before: a diarization first put the folder holding an ffmpeg in front of the
    gateway's own PATH, which every program the gateway starts afterwards inherits, and then ran
    whichever ffmpeg came first on it. Now the ffmpeg on the PATH the gateway started with runs,
    by its absolute path, and the PATH is as it was."""
    import os

    import numpy as np

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    seen = _fake_sherpa(monkeypatch, turns=[_Segment(0.0, 1.0, 0)])
    pcm = np.linspace(-0.5, 0.5, 16, dtype=np.float32)
    (tmp_path / "pcm").write_bytes(pcm.tobytes())
    folder = tmp_path / "bin"
    folder.mkdir()
    stand_in = folder / "ffmpeg"
    stand_in.write_text(f"#!/bin/sh\ncat '{tmp_path / 'pcm'}'\n", encoding="utf-8")
    stand_in.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}/usr/bin{os.pathsep}/bin")
    before = os.environ["PATH"]
    ran: list = []
    real_run = P.subprocess.run

    def _run(cmd, **kwargs):
        ran.append(cmd[0])
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(P.subprocess, "run", _run)
    memo = tmp_path / "memo.m4a"
    memo.write_bytes(b"\x00" * 32)

    turns = await P.create_provider({}).diarize(str(memo), model=P._MODEL)

    assert [t.speaker for t in turns] == ["SPEAKER_00"]
    assert ran == [str(stand_in)]
    assert np.array_equal(seen["samples"], pcm)
    assert os.environ["PATH"] == before
