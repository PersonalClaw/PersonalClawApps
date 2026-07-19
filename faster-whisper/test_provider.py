"""Unit tests for the faster-whisper (local STT) app.

The faster_whisper/CTranslate2 import is lazy (inside download/transcribe), so these
tests exercise the catalog + provider surface without it. The app registers the STT
provider that core's stt registry resolves the ``stt`` use-case to."""

from __future__ import annotations

import asyncio

import provider as prov

from personalclaw.sdk.stt import SttModel, SttProvider


def _run(coro):
    return asyncio.run(coro)


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
