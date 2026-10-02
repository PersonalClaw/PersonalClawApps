"""Tests for the pyannote diarization provider — catalog + HF-token gating."""

from __future__ import annotations

from pathlib import Path

import pytest

import provider as P


@pytest.fixture(autouse=True)
def _no_ambient_hf_token(monkeypatch):
    """Make "without a token" mean exactly that, on any host.

    ``_hf_token()`` delegates its fallback to the shared SDK cascade, which reads the real
    credential store, ``HF_TOKEN``/``HUGGING_FACE_HUB_TOKEN`` and ``~/.cache/huggingface/token``.
    Left unpatched, a contributor who has ever run ``huggingface-cli login`` would hand the
    two "refused without a token" cases below a LIVE token — flipping them from an assertion
    about a guard into a real gated download of a multi-gigabyte model. Neutralized per-test
    (not globally) so the delegation tests can still substitute their own resolver.
    """
    monkeypatch.setattr(P, "resolve_token", lambda: "")


def test_loading_the_app_turns_pyannotes_usage_reports_off(tmp_path):
    """pyannote.audio (from 4.0) reports each pipeline it loads and each file it diarizes to its
    makers unless ``PYANNOTE_METRICS_ENABLED`` says otherwise; it counts ``true`` and ``1`` as on,
    and reads the setting at every report. Loading the app sets it off, in a fresh process, over
    an inherited on."""
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "PYANNOTE_METRICS_ENABLED": "true",
        "PERSONALCLAW_HOME": str(tmp_path / "home"),
    }
    done = subprocess.run(
        [sys.executable, "-c", "import os, provider; print(os.environ['PYANNOTE_METRICS_ENABLED'])"],
        cwd=Path(__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    assert done.stdout.strip().splitlines()[-1] == "false"


def test_hf_token_delegates_to_the_shared_cascade(monkeypatch):
    """With no app-level setting, the token comes from the shared SDK cascade.

    The point of the change: the provider no longer hand-rolls a single ``os.environ``
    read, so a token held in the credential store or by ``huggingface-cli`` — neither of
    which the old two-term lookup could see — now reaches this provider.
    """
    monkeypatch.setattr(P, "resolve_token", lambda: "hf_from_cascade")
    assert P.create_provider({})._hf_token() == "hf_from_cascade"


def test_app_setting_wins_over_the_cascade(monkeypatch):
    """The manifest's ``hf_token`` field stays authoritative when it is set.

    It is persisted only to this bundle's ``data/config.json`` and mirrored into neither the
    credential store nor the environment, so the cascade cannot see it. If the cascade won
    here, the declared ``sensitive`` setting would be a dead control.
    """
    monkeypatch.setattr(P, "resolve_token", lambda: "hf_from_cascade")
    assert P.create_provider({"hf_token": "fake-hf-token-explicit"})._hf_token() == "fake-hf-token-explicit"


def test_create_provider():
    p = P.create_provider({})
    assert p.name == "diarization-pyannote" and p.display_name


@pytest.mark.asyncio
async def test_catalog_gated_model():
    models = await P.create_provider({}).list_models()
    assert len(models) == 1 and models[0].gated is True


@pytest.mark.asyncio
async def test_download_refused_without_token():
    assert await P.create_provider({}).download_model(P._MODEL) is False


@pytest.mark.asyncio
async def test_diarize_without_a_token_says_so(tmp_path):
    """🔴 Red before: ``None``, read as a recording with no speakers."""
    from personalclaw.sdk.diarization import DiarizationError

    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    with pytest.raises(DiarizationError, match="needs a Hugging Face token"):
        await P.create_provider({}).diarize(str(f), model=P._MODEL)


#: A stand-in for pyannote.audio, written to disk so the provider's child process imports it: the
#: pipeline runs there, not in the gateway. It notes what it was handed (how the pipeline was
#: loaded, the call, its pid, the usage-report setting it saw) in a JSON file, and the pipeline
#: holds the interpreter lock for ``_HOLD`` seconds, as scipy's clustering does inside the real one.
_FAKE_PIPELINE_BODY = '''
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


class _Segment:
    def __init__(self, start, end):
        self.start, self.end = start, end


class _Annotation:
    def itertracks(self, yield_label=False):
        for start, end, speaker in _TURNS:
            yield _Segment(start, end), None, speaker


class _DiarizeOutput:
    def __init__(self):
        self.speaker_diarization = _Annotation()


class _Pipeline:
    def __call__(self, audio, **kwargs):
        _note(audio=audio, kwargs=kwargs, pid=os.getpid())
        if _HOLD:
            ctypes.PyDLL(None).usleep(int(_HOLD * 1_000_000))
        if _DIE:
            os._exit(9)
        if _FAIL:
            raise RuntimeError(_FAIL)
        return _DiarizeOutput() if _WRAPPED else _Annotation()


class Pipeline:
    @staticmethod
    def from_pretrained(model, **kwargs):
        _note(loaded={"model": model, **kwargs},
              metrics=os.environ.get("PYANNOTE_METRICS_ENABLED"))
        if _LOAD_FAILS:
            raise RuntimeError(_LOAD_FAILS)
        if _REFUSED:
            return None
        return _Pipeline()
'''


@pytest.fixture
def fake_pyannote(tmp_path, monkeypatch):
    """Install the stand-in where both this process and the provider's child import it
    (``PYTHONPATH`` reaches the child). Returns the file it notes what it saw in, which stays
    absent until it is loaded."""
    import os
    import sys

    def install(*, turns=(), fail="", load_fails="", refused=False, wrapped=False, hold=0.0,
                die=False) -> Path:
        site = tmp_path / "fake-site"
        package = site / "pyannote"
        package.mkdir(parents=True, exist_ok=True)
        record = tmp_path / "pipeline-saw.json"
        record.unlink(missing_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        settings = {
            "_RECORD": str(record), "_TURNS": [list(t) for t in turns], "_FAIL": fail,
            "_LOAD_FAILS": load_fails, "_REFUSED": refused, "_WRAPPED": wrapped, "_HOLD": hold,
            "_DIE": die,
        }
        (package / "audio.py").write_text(
            "".join(f"{name} = {value!r}\n" for name, value in settings.items())
            + _FAKE_PIPELINE_BODY,
            encoding="utf-8",
        )
        inherited = os.environ.get("PYTHONPATH", "")
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(site), inherited) if p))
        monkeypatch.syspath_prepend(str(site))
        for name in ("pyannote.audio", "pyannote"):
            monkeypatch.delitem(sys.modules, name, raising=False)
        return record

    return install


def _pipeline_saw(record: Path) -> dict:
    import json

    return json.loads(record.read_text(encoding="utf-8")) if record.exists() else {}


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


@pytest.mark.asyncio
async def test_a_licence_not_accepted_says_where_to_accept_it(fake_pyannote, tmp_path):
    from personalclaw.sdk.diarization import DiarizationError

    fake_pyannote(
        load_fails="403 Client Error. Cannot access gated repo "
        "pyannote/speaker-diarization-community-1"
    )
    with pytest.raises(DiarizationError) as raised:
        await P.create_provider({"hf_token": "fake-hf-token"}).diarize(
            str(tmp_path / "a.wav"), model=P._MODEL
        )
    assert "https://hf.co/pyannote/speaker-diarization-community-1" in str(raised.value)


@pytest.mark.asyncio
async def test_a_pipeline_that_fails_says_why(fake_pyannote, tmp_path):
    from personalclaw.sdk.diarization import DiarizationError

    fake_pyannote(fail="could not decode audio")
    with pytest.raises(DiarizationError) as raised:
        await P.create_provider({"hf_token": "fake-hf-token"}).diarize(
            str(tmp_path / "a.wav"), model=P._MODEL
        )
    assert str(raised.value) == (
        "Diarization (pyannote) could not tell the speakers apart in this recording. Details: "
        "could not decode audio"
    )


@pytest.mark.asyncio
async def test_a_model_this_app_does_not_have_is_refused_before_the_pipeline(
    fake_pyannote, tmp_path, caplog
):
    """A call naming another model is refused, saying which model this app has, before the
    token is read or a pipeline fetched: the app used to fetch and run its own one instead."""
    import logging

    record = fake_pyannote(refused=True)

    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    provider = P.create_provider({"hf_token": "fake-hf-token"})
    with caplog.at_level(logging.WARNING):
        assert await provider.diarize(str(f), model="fake/other-diarizer") is None
    assert _pipeline_saw(record) == {}, "no pipeline was fetched"
    assert "fake/other-diarizer" in caplog.text and P._MODEL in caplog.text
    # The control: its own model is fetched (and, refused by Hugging Face here, says so).
    from personalclaw.sdk.diarization import DiarizationError

    with pytest.raises(DiarizationError, match="Accept its user conditions"):
        await provider.diarize(str(f), model=P._MODEL)
    assert _pipeline_saw(record)["loaded"]["model"] == P._MODEL


@pytest.mark.asyncio
async def test_diarize_unwraps_pyannote_4x_output(tmp_path, fake_pyannote):
    """pyannote.audio 4.x returns a DiarizeOutput whose .speaker_diarization is the
    Annotation (with itertracks); 3.x returned that Annotation directly. The provider
    must unwrap the 4.x shape — before this it called .itertracks on DiarizeOutput and
    got AttributeError → EVERY diarization silently returned None on 4.x."""
    fake_pyannote(turns=[(0.0, 5.5, "SPEAKER_00"), (5.6, 11.0, "SPEAKER_01")], wrapped=True)

    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    turns = await P.create_provider({"hf_token": "hf_test"}).diarize(str(f), model=P._MODEL)
    assert turns is not None and len(turns) == 2
    assert {t.speaker for t in turns} == {"SPEAKER_00", "SPEAKER_01"}
    assert turns[0].start == 0.0 and turns[1].end == 11.0


# ── the pipeline lives in the home, not in the Hugging Face folder other tools share ──────


def _shared_hub(tmp_path, monkeypatch) -> Path:
    """The machine-wide Hugging Face folder's model cache: where huggingface_hub looks when it is
    not told where to. The library reads that default once, at import, so it is set there too."""
    from huggingface_hub import constants

    shared = tmp_path / "hf" / "hub"
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(shared))
    return shared


def _seed_pipeline(cache: Path) -> None:
    """What a finished download of the pipeline leaves in a Hugging Face cache folder."""
    repo = cache / ("models--" + P._MODEL.replace("/", "--"))
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("0" * 40)
    snapshot = repo / "snapshots" / ("0" * 40)
    snapshot.mkdir(parents=True)
    (snapshot / "config.yaml").write_text("pipeline: {}\n")


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) if root.exists() else []


@pytest.mark.asyncio
async def test_a_pipeline_only_in_the_shared_folder_is_not_read(tmp_path, monkeypatch):
    """The folder other tools share is outside the home, and this app used to read it to say
    whether the pipeline was downloaded. It reads the home only now: a pipeline that is only
    there reads as not downloaded, and downloads once more, into the home."""
    shared = _shared_hub(tmp_path, monkeypatch)
    _seed_pipeline(shared)
    p = P.create_provider({"hf_token": "hf_test"})
    assert p._cached() is False
    assert (await p.list_models())[0].downloaded is False

    _seed_pipeline(P._models_dir())
    assert p._cached() is True, "the home's copy counts"
    assert (await p.list_models())[0].downloaded is True


@pytest.mark.asyncio
async def test_the_pipeline_downloads_into_the_home(tmp_path, monkeypatch):
    import sys
    import types

    from personalclaw.sdk.util import config_dir

    calls: list[dict] = []

    def snapshot_download(repo_id, **kwargs):
        calls.append({"repo_id": repo_id, **kwargs})

    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    p = P.create_provider({"hf_token": "hf_test"})
    assert await p.download_model(P._MODEL) is True
    assert calls == [{"repo_id": P._MODEL, "token": "hf_test", "cache_dir": p.cache_dir()}]
    assert Path(p.cache_dir()).resolve().is_relative_to(config_dir().resolve())


@pytest.mark.asyncio
async def test_the_pipeline_and_the_models_it_pulls_in_load_from_the_home(tmp_path, fake_pyannote):
    """pyannote.audio fetches the models the pipeline names when it loads, into the folder the
    pipeline is loaded with. That folder is the home's."""
    record = fake_pyannote()

    f = tmp_path / "a.wav"
    f.write_bytes(b"\x00" * 32)
    p = P.create_provider({"hf_token": "hf_test"})
    assert await p.diarize(str(f), model=P._MODEL) == []
    assert _pipeline_saw(record)["loaded"] == {
        "model": P._MODEL, "token": "hf_test", "cache_dir": p.cache_dir(),
    }


@pytest.mark.asyncio
async def test_delete_removes_the_home_copy_and_never_the_shared_one(tmp_path, monkeypatch):
    shared = _shared_hub(tmp_path, monkeypatch)
    _seed_pipeline(shared)
    before = _files(shared)
    p = P.create_provider({"hf_token": "hf_test"})
    assert await p.delete_model(P._MODEL) is False, "nothing in the home to delete"

    _seed_pipeline(P._models_dir())
    assert await p.delete_model(P._MODEL) is True
    assert not P._models_dir().exists()
    assert p._cached() is False
    assert _files(shared) == before


def test_a_media_call_that_names_no_model_is_refused_and_sends_nothing():
    """A diarization names its model (the diarization binding in Settings → Models). One that
    names none is refused with the SDK's sentence before the pipeline is fetched: it used to
    fetch and run this app's one pipeline in its place."""
    import asyncio
    from pathlib import Path

    from apps_testkit.model_wire import (
        media_adapters,
        media_refusal_expected,
        media_refusal_report,
    )

    adapters = media_adapters(Path(__file__).parent, P.create_provider)
    report = asyncio.run(media_refusal_report(adapters))
    assert report == media_refusal_expected(adapters)


@pytest.mark.asyncio
async def test_a_diarization_leaves_the_gateways_path_as_it_was(monkeypatch, tmp_path, fake_pyannote):
    """🔴 Red before: each diarization first put the folder holding an ffmpeg in front of the
    gateway's own PATH, which every program the gateway starts afterwards inherits. pyannote
    decodes recordings itself and runs no ffmpeg, so it now looks for none."""
    import os

    fake_pyannote(turns=[(0.0, 1.5, "SPEAKER_00")])
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    before = os.environ["PATH"]

    turns = await P.create_provider({"hf_token": "fake-hf-token"}).diarize(
        str(tmp_path / "a.wav"), model=P._MODEL
    )

    assert [(t.start, t.end, t.speaker) for t in turns] == [(0.0, 1.5, "SPEAKER_00")]
    assert os.environ["PATH"] == before


@pytest.mark.asyncio
async def test_a_long_diarization_leaves_the_gateway_answering(tmp_path, fake_pyannote):
    """🔴 Red before: the pipeline ran in a thread of the gateway, and its clustering holds the
    interpreter lock while it runs (1.16 s at a time for a two-hour recording's voices), so every
    request the gateway was serving waited. The stand-in holds the lock for 1.5 s; the event loop
    must go on ticking, because the pipeline runs in a child process."""
    import os

    record = fake_pyannote(turns=[(0.0, 1.0, "SPEAKER_00"), (1.2, 2.0, "SPEAKER_01")], hold=1.5)
    memo = tmp_path / "standup.wav"
    memo.write_bytes(b"\x00" * 32)

    turns, worst = await _worst_gap_while(
        P.create_provider({"hf_token": "hf_test"}).diarize(str(memo), model=P._MODEL, max_speakers=2)
    )

    assert [(t.start, t.end, t.speaker) for t in turns] == [
        (0.0, 1.0, "SPEAKER_00"), (1.2, 2.0, "SPEAKER_01"),
    ]
    assert worst < 0.75, f"the event loop stopped for {worst:.2f}s while the pipeline held its lock"
    saw = _pipeline_saw(record)
    assert saw["pid"] != os.getpid(), "the pipeline ran in the gateway's process"
    assert saw["audio"] == str(memo) and saw["kwargs"] == {"max_speakers": 2}


@pytest.mark.asyncio
async def test_the_pipelines_process_turns_usage_reports_off(tmp_path, fake_pyannote, monkeypatch):
    """The child does not inherit the gateway's setting, so the worker sets it before the library
    loads: pyannote.audio reads it at every report, and counts anything but off as on."""
    monkeypatch.setenv("PYANNOTE_METRICS_ENABLED", "true")
    record = fake_pyannote()
    f = tmp_path / "a.wav"
    f.write_bytes(b"\x00" * 32)

    assert await P.create_provider({"hf_token": "hf_test"}).diarize(str(f), model=P._MODEL) == []

    assert _pipeline_saw(record)["metrics"] == "false"


@pytest.mark.asyncio
async def test_a_process_that_ends_before_it_answers_says_so(tmp_path, fake_pyannote):
    from personalclaw.sdk.diarization import DiarizationError

    fake_pyannote(die=True)
    f = tmp_path / "a.wav"
    f.write_bytes(b"\x00" * 32)

    with pytest.raises(DiarizationError) as raised:
        await P.create_provider({"hf_token": "hf_test"}).diarize(str(f), model=P._MODEL)

    assert str(raised.value) == (
        "Diarization (pyannote) stopped before it finished: the process it ran in ended (exit_9)."
    )

