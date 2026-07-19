"""Tests for the pyannote diarization provider — catalog + HF-token gating."""

from __future__ import annotations

import pytest

import provider as P


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
async def test_diarize_none_without_token(tmp_path):
    f = tmp_path / "a.wav"; f.write_bytes(b"\x00" * 32)
    assert await P.create_provider({}).diarize(str(f)) is None  # no token


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
    turns = await P.create_provider({"hf_token": "hf_test"}).diarize(str(f))
    assert turns is not None and len(turns) == 2
    assert {t.speaker for t in turns} == {"SPEAKER_00", "SPEAKER_01"}
    assert turns[0].start == 0.0 and turns[1].end == 11.0
