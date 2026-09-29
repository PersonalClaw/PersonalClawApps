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


def _pipeline_that(behaviour, monkeypatch):
    import sys
    import types

    class _PipelineFactory:
        @staticmethod
        def from_pretrained(model, **kwargs):
            return behaviour(model)

    fake_mod = types.ModuleType("pyannote.audio")
    fake_mod.Pipeline = _PipelineFactory
    monkeypatch.setitem(sys.modules, "pyannote.audio", fake_mod)
    monkeypatch.setattr(P, "ensure_ffmpeg_in_path", lambda: None)


@pytest.mark.asyncio
async def test_a_licence_not_accepted_says_where_to_accept_it(monkeypatch, tmp_path):
    from personalclaw.sdk.diarization import DiarizationError

    def _gated(model):
        raise RuntimeError(
            "403 Client Error. Cannot access gated repo pyannote/speaker-diarization-community-1"
        )

    _pipeline_that(_gated, monkeypatch)
    with pytest.raises(DiarizationError) as raised:
        await P.create_provider({"hf_token": "fake-hf-token"}).diarize(
            str(tmp_path / "a.wav"), model=P._MODEL
        )
    assert "https://hf.co/pyannote/speaker-diarization-community-1" in str(raised.value)


@pytest.mark.asyncio
async def test_a_pipeline_that_fails_says_why(monkeypatch, tmp_path):
    from personalclaw.sdk.diarization import DiarizationError

    class _Pipeline:
        def __call__(self, audio_path, **kwargs):
            raise RuntimeError("could not decode audio")

    _pipeline_that(lambda model: _Pipeline(), monkeypatch)
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
    monkeypatch, tmp_path, caplog
):
    """A call naming another model is refused, saying which model this app has, before the
    token is read or a pipeline fetched: the app used to fetch and run its own one instead."""
    import logging
    import sys
    import types

    fetched: list[str] = []

    class _PipelineFactory:
        @staticmethod
        def from_pretrained(model, **kwargs):
            fetched.append(model)
            return None

    fake_mod = types.ModuleType("pyannote.audio")
    fake_mod.Pipeline = _PipelineFactory
    monkeypatch.setitem(sys.modules, "pyannote.audio", fake_mod)
    monkeypatch.setattr(P, "ensure_ffmpeg_in_path", lambda: None)

    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    provider = P.create_provider({"hf_token": "fake-hf-token"})
    with caplog.at_level(logging.WARNING):
        assert await provider.diarize(str(f), model="fake/other-diarizer") is None
    assert fetched == []
    assert "fake/other-diarizer" in caplog.text and P._MODEL in caplog.text
    # The control: its own model is fetched (and, refused by Hugging Face here, says so).
    from personalclaw.sdk.diarization import DiarizationError

    with pytest.raises(DiarizationError, match="Accept its user conditions"):
        await provider.diarize(str(f), model=P._MODEL)
    assert fetched == [P._MODEL]


@pytest.mark.asyncio
async def test_diarize_unwraps_pyannote_4x_output(tmp_path, monkeypatch):
    """pyannote.audio 4.x returns a DiarizeOutput whose .speaker_diarization is the
    Annotation (with itertracks); 3.x returned that Annotation directly. The provider
    must unwrap the 4.x shape — before this it called .itertracks on DiarizeOutput and
    got AttributeError → EVERY diarization silently returned None on 4.x."""
    from types import SimpleNamespace

    class _Seg:
        def __init__(self, s, e): self.start, self.end = s, e

    class _Annotation:  # mimics pyannote's Annotation.itertracks(yield_label=True)
        def itertracks(self, yield_label=False):
            yield _Seg(0.0, 5.5), "t0", "SPEAKER_00"
            yield _Seg(5.6, 11.0), "t1", "SPEAKER_01"

    # 4.x shape: pipeline(audio) -> DiarizeOutput(.speaker_diarization=Annotation)
    diarize_output = SimpleNamespace(speaker_diarization=_Annotation())

    class _FakePipeline:
        def __call__(self, audio_path, **kwargs):
            return diarize_output

    class _PipelineFactory:
        @staticmethod
        def from_pretrained(model, **kwargs):
            return _FakePipeline()

    import sys, types
    fake_mod = types.ModuleType("pyannote.audio")
    fake_mod.Pipeline = _PipelineFactory
    monkeypatch.setitem(sys.modules, "pyannote.audio", fake_mod)

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
async def test_the_pipeline_and_the_models_it_pulls_in_load_from_the_home(tmp_path, monkeypatch):
    """pyannote.audio fetches the models the pipeline names when it loads, into the folder the
    pipeline is loaded with. That folder is the home's."""
    import sys
    import types
    from types import SimpleNamespace

    loads: list[dict] = []

    class _PipelineFactory:
        @staticmethod
        def from_pretrained(model, **kwargs):
            loads.append({"model": model, **kwargs})
            return lambda audio_path, **kw: SimpleNamespace(itertracks=lambda **_: iter(()))

    fake_mod = types.ModuleType("pyannote.audio")
    fake_mod.Pipeline = _PipelineFactory
    monkeypatch.setitem(sys.modules, "pyannote.audio", fake_mod)

    f = tmp_path / "a.wav"
    f.write_bytes(b"\x00" * 32)
    p = P.create_provider({"hf_token": "hf_test"})
    assert await p.diarize(str(f), model=P._MODEL) == []
    assert loads == [{"model": P._MODEL, "token": "hf_test", "cache_dir": p.cache_dir()}]


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
