"""MI-6 remainder: real zero-shot inference, resumable weights, typed crash reason.

What the atom pins, test by test:

- the sidecar **worker** drives the REAL engine API (constructor + synthesis method
  probed in documented order, kwargs filtered to the callee's true signature) and an
  engine that matches none of it fails TYPED and ALIVE (``engine_api_mismatch``), never
  as a process death;
- the **provider** routes synthesis through the sidecar runner with the exact
  ``{"method", "payload"}`` nesting the child refuses to mis-parse, records a sidecar
  death's ``typed_reason`` while returning ``None`` (gateway up), and clears it on the
  next clean call;
- **downloads resume**: an interrupted fetch keeps its partial files and writes NO
  completion receipt; the follow-up fetch completes and only then marks done;
- the spike verdict is FINAL in the shipped surface: one catalog card (OmniVoice), one
  candidate module, no "pending spike" copy.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import provider as prov
import worker
from provider import VoiceCloneTtsProvider, create_provider

_BUNDLE = Path(__file__).resolve().parent


# ── the worker drives the real engine API ──────────────────────────────────────


class _FakePipeline:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def clone(self, *, text, ref_audio="", ref_text="", seed=0):
        self.calls.append({"text": text, "ref_audio": ref_audio, "ref_text": ref_text, "seed": seed})
        return b"RIFF-fake-wav-bytes"


def _install_fake_omnivoice(monkeypatch, pipeline):
    mod = types.ModuleType("omnivoice")

    class OmniVoice:
        seen: dict = {}

        @staticmethod
        def from_pretrained(pretrained_model_name_or_path="", device_map="cpu"):
            OmniVoice.seen = {"path": pretrained_model_name_or_path, "device": device_map}
            return pipeline

    mod.OmniVoice = OmniVoice
    monkeypatch.setitem(sys.modules, "omnivoice", mod)
    return mod


@pytest.fixture(autouse=True)
def _fresh_worker_state():
    worker.unload()
    yield
    worker.unload()


class TestWorkerRealInference:
    def test_load_constructs_via_from_pretrained_with_device(self, monkeypatch):
        pipeline = _FakePipeline()
        mod = _install_fake_omnivoice(monkeypatch, pipeline)
        out = worker.load(device="mps", weights_dir="/w/omnivoice-zeroshot")
        assert out["loaded"] is True and out["reused"] is False
        assert mod.OmniVoice.seen == {"path": "/w/omnivoice-zeroshot", "device": "mps"}
        # Idempotent: same key reuses the live pipeline instead of re-importing torch.
        assert worker.load(device="mps", weights_dir="/w/omnivoice-zeroshot")["reused"] is True

    def test_synthesize_calls_the_engine_and_writes_audio(self, monkeypatch, tmp_path):
        pipeline = _FakePipeline()
        _install_fake_omnivoice(monkeypatch, pipeline)
        worker.load(device="cpu", weights_dir="")
        out_path = str(tmp_path / "clip.wav")
        result = worker.call(
            "synthesize",
            {
                "text": "hello there",
                "output_path": out_path,
                "ref_audio": "/clips/me.wav",
                "ref_text": "reference",
                "seed": 7,
            },
        )
        assert result == {"output_path": out_path, "method": "clone"}
        # The REAL call happened: engine method invoked with the clone conditioning,
        # kwargs filtered to its true signature (speed/instruct dropped, not passed).
        assert pipeline.calls == [
            {"text": "hello there", "ref_audio": "/clips/me.wav", "ref_text": "reference", "seed": 7}
        ]
        assert Path(out_path).read_bytes() == b"RIFF-fake-wav-bytes"

    def test_a_path_returning_engine_is_copied_to_output(self, monkeypatch, tmp_path):
        produced = tmp_path / "engine-out.wav"
        produced.write_bytes(b"engine-bytes")

        class _PathPipeline:
            def synthesize(self, **kw):
                return str(produced)

        mod = types.ModuleType("omnivoice")
        mod.load_model = lambda **kw: _PathPipeline()
        monkeypatch.setitem(sys.modules, "omnivoice", mod)
        worker.load(device="cpu", weights_dir="")
        out_path = str(tmp_path / "final.wav")
        result = worker.call("synthesize", {"text": "x", "output_path": out_path})
        assert result["output_path"] == out_path and result["method"] == "synthesize"
        assert Path(out_path).read_bytes() == b"engine-bytes"

    def test_a_batch_list_return_is_unwrapped(self, monkeypatch, tmp_path):
        # The real engine is batch-in/batch-out: one text returns a ONE-ELEMENT list.
        class _BatchPipeline:
            def generate(self, **kw):
                return [b"clip-bytes"]

        mod = types.ModuleType("omnivoice")
        mod.load_model = lambda **kw: _BatchPipeline()
        monkeypatch.setitem(sys.modules, "omnivoice", mod)
        worker.load(device="cpu", weights_dir="")
        out_path = str(tmp_path / "clip.wav")
        result = worker.call("synthesize", {"text": "x", "output_path": out_path})
        assert result["method"] == "generate"
        assert Path(out_path).read_bytes() == b"clip-bytes"

    def test_a_multi_clip_batch_is_refused_not_guessed(self, monkeypatch, tmp_path):
        class _BatchPipeline:
            def generate(self, **kw):
                return [b"a", b"b"]

        mod = types.ModuleType("omnivoice")
        mod.load_model = lambda **kw: _BatchPipeline()
        monkeypatch.setitem(sys.modules, "omnivoice", mod)
        worker.load(device="cpu", weights_dir="")
        with pytest.raises(RuntimeError, match="single-clip batch"):
            worker.call("synthesize", {"text": "x", "output_path": str(tmp_path / "o.wav")})

    def test_a_bare_array_without_a_sampling_rate_is_refused(self, monkeypatch, tmp_path):
        # An array-shaped return needs the pipeline's own rate; inventing one would
        # write audio at the wrong speed and call it success.
        class _Arrayish:
            dtype = "float32"

            def __array__(self):  # pragma: no cover — presence is what's probed
                return self

        class _ArrayPipeline:
            def generate(self, **kw):
                return _Arrayish()

        mod = types.ModuleType("omnivoice")
        mod.load_model = lambda **kw: _ArrayPipeline()
        monkeypatch.setitem(sys.modules, "omnivoice", mod)
        worker.load(device="cpu", weights_dir="")
        with pytest.raises(RuntimeError, match="sampling rate"):
            worker.call("synthesize", {"text": "x", "output_path": str(tmp_path / "o.wav")})

    def test_an_unrecognized_engine_api_fails_typed_and_alive(self, monkeypatch):
        mod = types.ModuleType("omnivoice")  # exposes NOTHING the adapter knows
        monkeypatch.setitem(sys.modules, "omnivoice", mod)
        with pytest.raises(RuntimeError, match="engine_api_mismatch"):
            worker.load(device="cpu", weights_dir="")

    def test_call_before_load_is_refused(self):
        with pytest.raises(RuntimeError, match="not loaded"):
            worker.call("synthesize", {"text": "x"})

    def test_unknown_method_is_refused(self, monkeypatch):
        _install_fake_omnivoice(monkeypatch, _FakePipeline())
        worker.load(device="cpu", weights_dir="")
        with pytest.raises(ValueError, match="unknown method"):
            worker.call("transcribe", {})


# ── the provider routes through the sidecar and types the crash ────────────────

sdk_sidecar = pytest.importorskip(
    "personalclaw.sdk.sidecar",
    reason="core without the SDK sidecar facade — provider degrades; runner paths untestable",
)


class _FakeRunner:
    def __init__(self, *, crash: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._crash = crash

    async def acall(self, verb, payload=None, *, timeout=None):
        self.calls.append((verb, dict(payload or {})))
        if verb == "call" and self._crash is not None:
            raise self._crash
        if verb == "call":
            return {"output_path": payload["payload"]["output_path"]}
        return {"loaded": True}


class TestProviderSidecarPath:
    @pytest.mark.asyncio
    async def test_synthesize_routes_load_then_call_with_nested_payload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
        ref = tmp_path / "me.wav"
        ref.write_bytes(b"ref")
        p = create_provider({"device": "mps"})
        runner = _FakeRunner()
        p._runner = runner
        with patch("provider._detect_engine", return_value="omnivoice"):
            out = await p.synthesize(
                "hello", voice="omnivoice-zeroshot", ref_audio=str(ref), ref_text="r", seed=3
            )
        assert out and out.endswith(".wav")
        verbs = [v for v, _ in runner.calls]
        assert verbs == ["load", "call"]
        load_payload = runner.calls[0][1]
        assert load_payload["device"] == "mps"
        call_payload = runner.calls[1][1]
        # The child refuses flattened args — the provider must nest exactly this way.
        assert set(call_payload) == {"method", "payload"}
        assert call_payload["method"] == "synthesize"
        assert call_payload["payload"]["ref_audio"] == str(ref)
        assert call_payload["payload"]["seed"] == 3
        assert p.last_crash_reason == ""

    @pytest.mark.asyncio
    async def test_a_sidecar_death_is_typed_and_leaves_the_caller_standing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
        p = create_provider({})
        p._runner = _FakeRunner(crash=sdk_sidecar.SidecarCrashed("signal_9", generation=2))
        with patch("provider._detect_engine", return_value="omnivoice"):
            out = await p.synthesize("hello")
        assert out is None  # degraded, not raised — the gateway stays up
        assert p.last_crash_reason == "sidecar_crashed:signal_9"

    @pytest.mark.asyncio
    async def test_a_worker_refusal_degrades_without_burning_the_crash_channel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
        p = create_provider({})
        p._runner = _FakeRunner(crash=sdk_sidecar.SidecarWorkerError("engine_api_mismatch: no clone"))
        with patch("provider._detect_engine", return_value="omnivoice"):
            assert await p.synthesize("hello") is None
        assert p.last_crash_reason == ""  # alive-but-refused is NOT a crash


# ── resumable download: interrupted fetch survives ─────────────────────────────


class TestResumableDownload:
    @pytest.mark.asyncio
    async def test_an_interrupted_fetch_keeps_partials_and_resumes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
        attempts: list[str] = []

        def fake_snapshot_download(*, repo_id, local_dir):
            target = Path(local_dir)
            target.mkdir(parents=True, exist_ok=True)
            attempts.append(repo_id)
            if len(attempts) == 1:
                (target / "weights-part-00.bin").write_bytes(b"partial")
                raise ConnectionError("network died mid-fetch")
            (target / "weights-part-01.bin").write_bytes(b"rest")

        hub = types.ModuleType("huggingface_hub")
        hub.snapshot_download = fake_snapshot_download
        monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

        p = create_provider({})
        voice_dir = tmp_path / "models" / "tts-clone" / "omnivoice-zeroshot"

        assert await p.download_voice("omnivoice-zeroshot") is False
        assert (voice_dir / "weights-part-00.bin").exists(), "partials must survive the interrupt"
        assert p.downloaded_voice("omnivoice-zeroshot") is False, "no receipt for a half fetch"

        assert await p.download_voice("omnivoice-zeroshot") is True
        assert (voice_dir / "weights-part-00.bin").exists(), "resume built on the partials"
        assert p.downloaded_voice("omnivoice-zeroshot") is True
        receipt = json.loads((voice_dir / prov._RECEIPT_NAME).read_text())
        assert receipt["complete"] is True and receipt["source"] == "k2-fsa/OmniVoice"


# ── the spike verdict is final in the shipped surface ──────────────────────────


class TestVerdictIsFinal:
    def test_exactly_one_engine_card_and_no_pending_copy(self):
        cards = json.loads((_BUNDLE / "catalog.json").read_text())
        assert [c["name"] for c in cards] == ["omnivoice-zeroshot"]
        assert "pending" not in cards[0]["label"].lower()

    def test_the_candidate_tuple_is_pinned_to_the_winner(self):
        assert prov._CANDIDATE_ENGINE_MODULES == ("omnivoice",)

    def test_the_worker_ships_beside_the_provider(self):
        assert (_BUNDLE / "worker.py").is_file()
        assert prov._worker_path().name == "worker.py"
