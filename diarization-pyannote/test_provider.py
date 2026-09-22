"""Tests for the pyannote diarization provider — catalog + HF-token gating."""

from __future__ import annotations

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
    assert P.create_provider({"hf_token": "hf_explicit"})._hf_token() == "hf_explicit"


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
