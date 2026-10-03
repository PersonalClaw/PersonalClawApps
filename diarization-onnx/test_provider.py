"""Tests for the ONNX diarization provider — catalog + gating + (when deps present) real diarize."""

from __future__ import annotations

import io
import json
import tarfile
import urllib.request
from pathlib import Path

import pytest
from personalclaw.sdk.net import EgressBlocked

import provider as P


def _seed_pair(root: Path) -> None:
    """Write the ONNX pair a real download leaves behind under *root*, so presence
    detection is exercised against a REAL tree rather than a mock."""
    seg = root / P._SEG_REL
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_bytes(b"\x00" * 16)
    (root / P._EMB_REL).write_bytes(b"\x00" * 16)


class _Served(io.BytesIO):
    """What opening a source returns: its bytes, and the length it announced (by default, the
    true one)."""

    def __init__(self, body: bytes, announced: int | None = None) -> None:
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body) if announced is None else announced)}


def _source_bytes(url: str) -> bytes:
    """The segmentation URL yields a REAL ``.tar.bz2`` whose one member is the model, so
    ``download_model`` runs its actual extract path into whatever root it chose."""
    if url != P._SEG_URL:
        return b"\x00" * 16
    payload = b"\x00" * 16
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tf:
        info = tarfile.TarInfo(str(P._SEG_REL))
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _serve(monkeypatch, announced: dict[str, int] | None = None) -> list[str]:
    """Stand in for the network at the one place the app opens a source, and return the URLs
    opened: the real download path (the partial file, the length check, the extract, the move
    into place) runs on what it hands out — the point of the cache_dir() assertions below."""
    opened: list[str] = []

    def open_url(url: str, *, timeout_s: float) -> _Served:
        assert timeout_s > 0, "a download opened with no bound on a stalled read"
        opened.append(url)
        return _Served(_source_bytes(url), (announced or {}).get(url))

    monkeypatch.setattr(P, "open_url", open_url)
    return opened


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
        "itself. Reinstall Diarization (ONNX) from the Store. The desktop app cannot install it: "
        "use the server or container build there.",
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

    monkeypatch.setattr(P, "open_url", _boom)
    assert P._downloaded() is False
    models = await P.create_provider({}).list_models()
    assert models[0].downloaded is False


# ── the engine runs in a process of its own ──────────────────────────────────────────────────

#: A stand-in for sherpa-onnx, written to disk so the provider's child process imports it too.
#: It notes what it was handed (model paths, the samples, its pid) in a JSON file, and its
#: ``process()`` holds the interpreter lock for ``_HOLD`` seconds, as the real one does for the
#: whole of a diarization: one C call that never lets the lock go.
_FAKE_ENGINE_BODY = '''
import ctypes
import json
import os


def _note(**seen):
    try:
        with open(_RECORD) as f:
            noted = json.load(f)
    except (OSError, ValueError):
        noted = {}
    noted.update(seen)
    with open(_RECORD, "w") as f:
        json.dump(noted, f)


class FastClusteringConfig:
    def __init__(self, **kw):
        _note(clustering=kw)


class OfflineSpeakerSegmentationPyannoteModelConfig:
    def __init__(self, model=""):
        _note(seg=model)


class OfflineSpeakerSegmentationModelConfig:
    def __init__(self, **kw):
        pass


class SpeakerEmbeddingExtractorConfig:
    def __init__(self, model=""):
        _note(emb=model)


class OfflineSpeakerDiarizationConfig:
    def __init__(self, **kw):
        pass


class _Segment:
    def __init__(self, start, end, speaker):
        self.start, self.end, self.speaker = start, end, speaker


class _Result:
    def sort_by_start_time(self):
        return [_Segment(*turn) for turn in _TURNS]


class OfflineSpeakerDiarization:
    sample_rate = 16000

    def __init__(self, config):
        pass

    def process(self, samples):
        _note(samples=[float(s) for s in samples[:64]], count=len(samples),
              loudest=float(abs(samples).max()) if len(samples) else 0.0, pid=os.getpid())
        if _HOLD:
            ctypes.PyDLL(None).usleep(int(_HOLD * 1_000_000))
        if _FAIL:
            raise RuntimeError(_FAIL)
        return _Result()
'''


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    """Install the stand-in engine where both this process and the provider's child import it
    (``PYTHONPATH`` reaches the child and precedes the installed sherpa-onnx there). Returns the
    file it notes what it saw in."""
    import os
    import sys

    def install(*, turns=(), fail=None, hold=0.0) -> Path:
        site = tmp_path / "fake-site"
        package = site / "sherpa_onnx"
        package.mkdir(parents=True, exist_ok=True)
        record = tmp_path / "engine-saw.json"
        record.unlink(missing_ok=True)
        (package / "__init__.py").write_text(
            f"_RECORD = {str(record)!r}\n_TURNS = {[list(t) for t in turns]!r}\n"
            f"_FAIL = {fail!r}\n_HOLD = {hold!r}\n" + _FAKE_ENGINE_BODY,
            encoding="utf-8",
        )
        inherited = os.environ.get("PYTHONPATH", "")
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(site), inherited) if p))
        monkeypatch.syspath_prepend(str(site))
        monkeypatch.delitem(sys.modules, "sherpa_onnx", raising=False)
        return record

    return install


def _engine_saw(record: Path) -> dict:
    import json

    return json.loads(record.read_text(encoding="utf-8"))


def _fake_ffmpeg(tmp_path: Path, pcm: bytes) -> str:
    """An ffmpeg stand-in under the test's own folder (``bin/ffmpeg``): it notes how it was run
    (its own path, then its arguments) and writes *pcm* as the decoded audio."""
    import sys

    script = tmp_path / "bin" / "ffmpeg"
    script.parent.mkdir(exist_ok=True)
    (tmp_path / "pcm").write_bytes(pcm)
    script.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(tmp_path / 'ffmpeg-ran.json')!r}, 'w').write(json.dumps(sys.argv))\n"
        f"sys.stdout.buffer.write(open({str(tmp_path / 'pcm')!r}, 'rb').read())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


async def _worst_gap_while(awaitable):
    """Await *awaitable* while the event loop ticks every 10 ms: its result, and the longest gap."""
    import asyncio
    import time

    gaps: list[float] = []
    done = asyncio.Event()

    async def _tick():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.ensure_future(_tick())
    await asyncio.sleep(0.05)
    try:
        result = await awaitable
    finally:
        done.set()
        await ticker
    return result, max(gaps)


def _ffmpeg_ran(tmp_path: Path) -> list[str]:
    """How the stand-in ffmpeg was run: its own path, then its arguments."""
    import json

    return json.loads((tmp_path / "ffmpeg-ran.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_diarize_reads_the_pair_from_the_home(monkeypatch, tmp_path, fake_engine):
    """The PIPELINE is handed the home's paths."""
    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    record = fake_engine()
    monkeypatch.setattr(P, "find_ffmpeg", lambda: _fake_ffmpeg(tmp_path, bytes(32)))

    f = tmp_path / "a.wav"
    f.write_bytes(b"\x00" * 32)
    assert await P.create_provider({}).diarize(str(f), model=P._MODEL) == []
    saw = _engine_saw(record)
    assert saw["seg"] == str(P._models_dir() / P._SEG_REL)
    assert saw["emb"] == str(P._models_dir() / P._EMB_REL)


@pytest.mark.asyncio
async def test_the_diarizations_process_runs_only_a_program_named_ffmpeg(
    monkeypatch, tmp_path, fake_engine
):
    """The child is handed the path of the ffmpeg core found, and looks for ffmpeg in that folder
    only: a path that names another program runs nothing, and the diarization says why."""
    from personalclaw.sdk.diarization import DiarizationError

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    fake_engine()
    stand_in = Path(_fake_ffmpeg(tmp_path, bytes(32)))
    other = stand_in.rename(stand_in.with_name("recorder"))
    monkeypatch.setattr(P, "find_ffmpeg", lambda: str(other))
    memo = tmp_path / "standup.m4a"
    memo.write_bytes(b"\x00" * 32)

    with pytest.raises(DiarizationError) as raised:
        await P.create_provider({}).diarize(str(memo), model=P._MODEL)

    assert f"there is no ffmpeg to run at {other}" in str(raised.value)
    assert not (tmp_path / "ffmpeg-ran.json").exists(), "a program not named ffmpeg ran"


@pytest.mark.asyncio
async def test_the_model_hears_the_recording_decoded_at_its_own_rate(
    monkeypatch, tmp_path, fake_engine
):
    """Every recording goes through ffmpeg, mono, at the model's sample rate, as float32: the
    samples the pipeline is handed are exactly what ffmpeg wrote. Turns come back labelled."""
    import numpy as np

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    record = fake_engine(turns=[(0.0, 4.2, 0), (4.4, 9.1, 1)])
    pcm = np.linspace(-0.5, 0.5, 64, dtype=np.float32)
    ffmpeg = _fake_ffmpeg(tmp_path, pcm.tobytes())
    monkeypatch.setattr(P, "find_ffmpeg", lambda: ffmpeg)
    memo = tmp_path / "team call.m4a"
    memo.write_bytes(b"\x00" * 32)

    turns = await P.create_provider({}).diarize(str(memo), model=P._MODEL)

    assert [(t.start, t.end, t.speaker) for t in turns] == [
        (0.0, 4.2, "SPEAKER_00"), (4.4, 9.1, "SPEAKER_01"),
    ]
    ran, *argv = _ffmpeg_ran(tmp_path)
    assert ran == ffmpeg
    assert argv[argv.index("-i") + 1] == str(memo)
    assert argv[argv.index("-ar") + 1] == "16000" and argv[argv.index("-ac") + 1] == "1"
    assert argv[argv.index("-f") + 1] == "f32le"
    assert _engine_saw(record)["samples"] == pytest.approx(pcm.tolist())


def _ffmpeg_or_skip() -> str:
    import shutil

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("needs ffmpeg to build an AAC recording")
    return ffmpeg


@pytest.mark.asyncio
async def test_an_aac_voice_memo_is_read_where_soundfile_could_not(
    monkeypatch, tmp_path, fake_engine
):
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
    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    record = fake_engine()
    monkeypatch.setattr(P, "find_ffmpeg", lambda: ffmpeg)

    assert await P.create_provider({}).diarize(str(memo), model=P._MODEL) == []

    saw = _engine_saw(record)
    assert 15_500 <= saw["count"] <= 16_600, saw["count"]  # one second at 16 kHz, not 22,050
    assert saw["loudest"] > 0.1  # the tone, not silence


@pytest.mark.asyncio
async def test_a_file_ffmpeg_cannot_read_says_why(monkeypatch, tmp_path, fake_engine):
    from personalclaw.sdk.diarization import DiarizationError

    ffmpeg = _ffmpeg_or_skip()
    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    fake_engine()
    monkeypatch.setattr(P, "find_ffmpeg", lambda: ffmpeg)
    junk = tmp_path / "not audio.m4a"
    junk.write_text("this is a text file")
    with pytest.raises(DiarizationError) as raised:
        await P.create_provider({}).diarize(str(junk), model=P._MODEL)
    said = str(raised.value)
    assert said.startswith("ONNX diarization could not read this recording. Details: ")
    assert "[in#" not in said and str(tmp_path) not in said and "\n" not in said, said


@pytest.mark.asyncio
async def test_a_recording_cut_off_after_its_header_is_not_a_recording_with_no_speakers(
    monkeypatch, tmp_path, fake_engine
):
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
    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    record = fake_engine()
    monkeypatch.setattr(P, "find_ffmpeg", lambda: ffmpeg)
    provider = P.create_provider({})

    assert await provider.diarize(str(memo), model=P._MODEL) == []
    assert _engine_saw(record)["count"] > 0  # the control: the whole memo is read
    with pytest.raises(DiarizationError) as raised:
        await provider.diarize(str(cut), model=P._MODEL)
    assert str(raised.value) == P._NO_AUDIO


def test_ffmpegs_closing_line_is_the_detail():
    import worker as W

    stderr = (b"[in#0 @ 0x76e5020000] moov atom not found\n"
              b"[in#0 @ 0x76e4c14000] Error opening input: Invalid data found when processing input\n"
              b"Error opening input file /srv/knowledge/files/0b1c.m4a.\n"
              b"Error opening input files: Invalid data found when processing input\n")
    assert W.ffmpeg_said(stderr, 1) == "Error opening input files: Invalid data found when processing input"
    assert W.ffmpeg_said(b"[aac @ 0x1] Too many bits\n", 1) == "Too many bits"
    assert W.ffmpeg_said(b"", 183) == "ffmpeg exited with status 183"


@pytest.mark.asyncio
async def test_a_pipeline_that_fails_says_why(monkeypatch, tmp_path, fake_engine):
    """🔴 Red before: swallowed into ``None``, with no log line."""
    from personalclaw.sdk.diarization import DiarizationError

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    fake_engine(fail="Expected samples rate: 16000, given: 22050")
    monkeypatch.setattr(P, "find_ffmpeg", lambda: _fake_ffmpeg(tmp_path, bytes(32)))
    with pytest.raises(DiarizationError) as raised:
        await P.create_provider({}).diarize(str(tmp_path / "a.wav"), model=P._MODEL)
    assert str(raised.value) == (
        "ONNX diarization could not tell the speakers apart in this recording. Details: "
        "Expected samples rate: 16000, given: 22050"
    )


@pytest.mark.asyncio
async def test_a_long_recording_is_not_cut_off_by_a_clock_of_its_own(
    monkeypatch, tmp_path, fake_engine
):
    """🔴 Red before: a fixed ten-minute ``wait_for`` answered ``None`` when it fired, whatever
    the caller's budget. Here every ``wait_for`` gives up at once: the provider must not use one."""
    import asyncio

    async def _out_of_time(awaitable, timeout):
        if hasattr(awaitable, "cancel"):
            awaitable.cancel()
        raise asyncio.TimeoutError

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    fake_engine(turns=[(0.0, 1.0, 0)])
    monkeypatch.setattr(P, "find_ffmpeg", lambda: _fake_ffmpeg(tmp_path, bytes(32)))
    monkeypatch.setattr(asyncio, "wait_for", _out_of_time)
    turns = await P.create_provider({}).diarize(str(tmp_path / "a.wav"), model=P._MODEL)
    assert [t.speaker for t in turns] == ["SPEAKER_00"]


@pytest.mark.asyncio
async def test_a_process_that_ends_before_it_answers_says_so(monkeypatch, tmp_path, fake_engine):
    """The diarization's process can be ended under it (out of memory on a long recording): the
    step says it stopped and how, rather than that no one spoke."""
    from personalclaw.sdk.diarization import DiarizationError

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    fake_engine(fail="unused")
    engine = tmp_path / "fake-site" / "sherpa_onnx" / "__init__.py"
    engine.write_text(
        engine.read_text(encoding="utf-8").replace(
            "        if _FAIL:\n",
            "        os.kill(os.getpid(), 9)\n        if _FAIL:\n",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(P, "find_ffmpeg", lambda: _fake_ffmpeg(tmp_path, bytes(32)))
    with pytest.raises(DiarizationError) as raised:
        await P.create_provider({}).diarize(str(tmp_path / "a.wav"), model=P._MODEL)
    assert str(raised.value) == (
        "ONNX diarization stopped before it finished: the process it ran in ended (signal_9)."
    )


@pytest.mark.asyncio
async def test_cache_dir_is_the_dir_a_fresh_download_fills(monkeypatch, tmp_path):
    """cache_dir() is what core's download UI reads for byte progress, so it must be the
    directory that ACTUALLY fills. Asserted by running the real download path against a
    stubbed network and then checking the REPORTED dir now holds the weights."""
    _home(monkeypatch, tmp_path)
    opened = _serve(monkeypatch)

    p = P.create_provider({})
    reported = Path(p.cache_dir())
    assert await p.download_model(P._MODEL) is True
    assert P._has_weights(reported), f"cache_dir() {reported} did not fill"
    assert opened == [P._SEG_URL, P._EMB_URL]
    assert not [f for f in _files(reported) if f.endswith((".partial", ".tar.bz2"))]


@pytest.mark.asyncio
async def test_a_download_cut_off_before_its_end_is_not_kept(monkeypatch, tmp_path):
    """A source that stops sending before the length it announced leaves nothing at the
    model's path: a short file there would read as a downloaded model every run after."""
    _home(monkeypatch, tmp_path)
    _serve(monkeypatch, announced={P._EMB_URL: 64})

    assert await P.create_provider({}).download_model(P._MODEL) is False
    assert not (P._models_dir() / P._EMB_REL).exists()
    assert P._downloaded() is False
    assert not [f for f in _files(P._models_dir()) if f.endswith(".partial")]


@pytest.mark.asyncio
async def test_a_source_on_denied_hosts_is_refused_and_the_download_says_why(
    monkeypatch, tmp_path
):
    """The download is held to the owner's Network egress settings, through core's own guard:
    a source on Denied hosts is never connected to, and the download fails with the guard's
    sentence, which names the setting, rather than a bare failure."""
    home = _home(monkeypatch, tmp_path)
    (home / "config.json").write_text(
        json.dumps({"security": {"egress": {"deny_hosts": ["github.com"]}}}), encoding="utf-8"
    )
    connected: list[str] = []

    def connect(_handler, request):
        connected.append(request.full_url)
        raise OSError("the guard let this through")

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", connect)

    with pytest.raises(EgressBlocked) as refused:
        await P.create_provider({}).download_model(P._MODEL)

    assert connected == [], "a refused source was connected to"
    assert f"{P._SEG_URL} was not reached" in str(refused.value)
    assert "Denied hosts" in str(refused.value)
    assert _files(P._models_dir()) == []


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
    monkeypatch, tmp_path, fake_engine
):
    """🔴 Red before: a diarization first put the folder holding an ffmpeg in front of the
    gateway's own PATH, which every program the gateway starts afterwards inherits, and then ran
    whichever ffmpeg came first on it. Now the ffmpeg on the PATH the gateway started with runs,
    by its absolute path, and the PATH is as it was."""
    import os

    import numpy as np

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    record = fake_engine(turns=[(0.0, 1.0, 0)])
    pcm = np.linspace(-0.5, 0.5, 16, dtype=np.float32)
    stand_in = _fake_ffmpeg(tmp_path, pcm.tobytes())
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin")
    before = os.environ["PATH"]
    memo = tmp_path / "memo.m4a"
    memo.write_bytes(b"\x00" * 32)

    turns = await P.create_provider({}).diarize(str(memo), model=P._MODEL)

    assert [t.speaker for t in turns] == ["SPEAKER_00"]
    assert _ffmpeg_ran(tmp_path)[0] == stand_in
    assert _engine_saw(record)["samples"] == pytest.approx(pcm.tolist())
    assert os.environ["PATH"] == before


@pytest.mark.asyncio
async def test_a_long_diarization_leaves_the_gateway_answering(monkeypatch, tmp_path, fake_engine):
    """🔴 Red before: sherpa-onnx holds the interpreter lock for the whole of ``process()``, and
    this app ran it in a thread of the gateway, so every request the gateway was serving waited
    for the diarization to end (two minutes, for a six-minute video). The stand-in holds the lock
    for 1.5 s; the event loop must go on ticking, because the engine runs in a child process."""
    import os

    _home(monkeypatch, tmp_path)
    _seed_pair(P._models_dir())
    record = fake_engine(turns=[(0.0, 1.0, 0), (1.2, 2.0, 1)], hold=1.5)
    monkeypatch.setattr(P, "find_ffmpeg", lambda: _fake_ffmpeg(tmp_path, bytes(64)))
    memo = tmp_path / "standup.m4a"
    memo.write_bytes(b"\x00" * 32)

    turns, worst = await _worst_gap_while(
        P.create_provider({}).diarize(str(memo), model=P._MODEL)
    )

    assert [(t.start, t.end, t.speaker) for t in turns] == [
        (0.0, 1.0, "SPEAKER_00"), (1.2, 2.0, "SPEAKER_01"),
    ]
    assert worst < 0.75, f"the event loop stopped for {worst:.2f}s while the engine held its lock"
    assert _engine_saw(record)["pid"] != os.getpid(), "the engine ran in the gateway's process"
