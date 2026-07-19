"""ONNX speaker-diarization provider (serves the ``diarization`` use-case).

The non-gated, install-and-go diarization backend: a sherpa-onnx segmentation +
speaker-embedding + clustering pipeline. No HuggingFace token — downloads freely, matching
PClaw's OSS/local-first ethos. One of (potentially) several providers for the ``diarization``
capability, exactly like faster-whisper is one STT provider (mirrors its app shape).
"""

from __future__ import annotations

import asyncio
import os
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

from personalclaw.sdk.diarization import (
    DiarizationModel,
    DiarizationProvider,
    LocalModelProvider,
    SpeakerTurn,
    ensure_ffmpeg_in_path,
)

_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "personalclaw" / "diarization-onnx"

# The catalog model id (the binding ref is ``diarization-onnx:<this>``). The real weights are
# a pyannote-converted ONNX segmentation model + a 3D-Speaker embedding model — the documented
# sherpa-onnx diarization pairing. Both plain ONNX; no HF token.
_MODEL = "sherpa-onnx-pyannote-segmentation-3.0"
_SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
            "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
_EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
            "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
_SEG_PATH = _CACHE_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
_EMB_PATH = _CACHE_DIR / "embed.onnx"


def create_provider(config: dict[str, Any] | None = None) -> "OnnxDiarizationProvider":
    return OnnxDiarizationProvider(config or {})


def availability() -> tuple[bool, str]:
    """Whether ONNX diarization can run here (needs onnxruntime + sherpa-onnx + soundfile)."""
    try:
        import onnxruntime  # noqa: F401
        import sherpa_onnx  # noqa: F401
        import soundfile  # noqa: F401
        return True, ""
    except ImportError:
        return False, ("ONNX diarization needs personalclaw[diarization-onnx] "
                       "(onnxruntime + sherpa-onnx + soundfile) — server/container build.")


def _downloaded() -> bool:
    return _SEG_PATH.is_file() and _EMB_PATH.is_file()


class OnnxDiarizationProvider(DiarizationProvider, LocalModelProvider):
    def __init__(self, config: dict[str, Any]):
        self._config = config

    @property
    def name(self) -> str:
        return "diarization-onnx"

    @property
    def display_name(self) -> str:
        return "Diarization (ONNX)"

    def cache_dir(self) -> str:
        return str(_CACHE_DIR)

    async def is_available(self) -> bool:
        ok, _ = availability()
        return ok

    async def list_models(self) -> list[DiarizationModel]:
        return [DiarizationModel(
            name=_MODEL, size_mb=47,
            description="ONNX segmentation + 3D-Speaker embedding — no token, install-and-go.",
            downloaded=_downloaded(), gated=False,
        )]

    async def download_model(self, model_name: str) -> bool:
        ensure_ffmpeg_in_path()

        def _run() -> bool:
            try:
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                if not _SEG_PATH.is_file():
                    tarball = _CACHE_DIR / "seg.tar.bz2"
                    urllib.request.urlretrieve(_SEG_URL, tarball)
                    with tarfile.open(tarball, "r:bz2") as tf:
                        tf.extractall(_CACHE_DIR)
                    tarball.unlink(missing_ok=True)
                if not _EMB_PATH.is_file():
                    urllib.request.urlretrieve(_EMB_URL, _EMB_PATH)
                return _downloaded()
            except Exception:
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)

    async def delete_model(self, model_name: str) -> bool:
        import shutil
        if _CACHE_DIR.is_dir():
            shutil.rmtree(_CACHE_DIR)
            return True
        return False

    async def diarize(self, audio_path: str, *, model: str = "", num_speakers: int | None = None,
                      min_speakers: int | None = None, max_speakers: int | None = None):
        ensure_ffmpeg_in_path()
        maxs = max_speakers or num_speakers or (self._config.get("max_speakers") or None)

        def _run():
            try:
                import sherpa_onnx
                import soundfile as sf
                if not _downloaded():
                    return None
                clustering = (sherpa_onnx.FastClusteringConfig(num_clusters=int(maxs))
                              if maxs else sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.5))
                cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(_SEG_PATH))),
                    embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(_EMB_PATH)),
                    clustering=clustering, min_duration_on=0.3,
                )
                sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
                samples, _sr = sf.read(audio_path, dtype="float32", always_2d=False)
                if getattr(samples, "ndim", 1) > 1:
                    samples = samples[:, 0]
                res = sd.process(samples).sort_by_start_time()
                return [SpeakerTurn(start=float(r.start), end=float(r.end),
                                    speaker=f"SPEAKER_{r.speaker:02d}") for r in res]
            except Exception:
                return None

        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _run), timeout=600)
        except asyncio.TimeoutError:
            return None
