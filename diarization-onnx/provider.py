"""ONNX speaker-diarization provider (serves the ``diarization`` use-case).

The non-gated, install-and-go diarization backend: a sherpa-onnx segmentation +
speaker-embedding + clustering pipeline. No HuggingFace token — downloads freely, matching
PClaw's OSS/local-first ethos. One of (potentially) several providers for the ``diarization``
capability, exactly like faster-whisper is one STT provider (mirrors its app shape).
"""

from __future__ import annotations

import asyncio
import logging
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
from personalclaw.sdk.model import ProviderResolutionError, require_model
from personalclaw.sdk.util import config_dir

logger = logging.getLogger(__name__)

# The catalog model id (the binding ref is ``diarization-onnx:<this>``). The real weights are
# a pyannote-converted ONNX segmentation model + a 3D-Speaker embedding model — the documented
# sherpa-onnx diarization pairing. Both plain ONNX; no HF token.
_MODEL = "sherpa-onnx-pyannote-segmentation-3.0"
_SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
            "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
_EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
            "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")

#: The two files the pipeline needs, RELATIVE to whichever root holds them — segmentation
#: alone cannot diarize, so the pair is the unit of "downloaded".
_SEG_REL = Path("sherpa-onnx-pyannote-segmentation-3-0") / "model.onnx"
_EMB_REL = Path("embed.onnx")


def _models_dir() -> Path:
    """Where the pair is downloaded to and read from: the PersonalClaw home
    (``config_dir()``), so an isolated home is actually isolated.

    Earlier releases wrote it to ``$XDG_CACHE_HOME/personalclaw/diarization-onnx`` and read
    through that folder. It is outside the home, so it is neither read nor deleted any more:
    an install that has the pair only there downloads it once (47 MB) into the home."""
    return config_dir() / "models" / "diarization-onnx"


def _has_weights(root: Path) -> bool:
    """True only when BOTH halves of the pair sit under *root* — a half-populated root is
    what an interrupted download leaves behind, and accepting it would resolve to nothing."""
    return (root / _SEG_REL).is_file() and (root / _EMB_REL).is_file()


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
    """Whether a usable pair is in the home."""
    return _has_weights(_models_dir())


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
        """Where downloaded weights land — the core download UI reads this for byte
        progress, and core's delete sweeps it."""
        return str(_models_dir())

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
                root = _models_dir()
                seg, emb = root / _SEG_REL, root / _EMB_REL
                root.mkdir(parents=True, exist_ok=True)
                if not seg.is_file():
                    tarball = root / "seg.tar.bz2"
                    urllib.request.urlretrieve(_SEG_URL, tarball)
                    with tarfile.open(tarball, "r:bz2") as tf:
                        # filter="data" is 3.14's default and the safe one: it refuses
                        # members that would escape *root*. Passed explicitly because the
                        # extract path is now covered by a test, which surfaced the
                        # DeprecationWarning the implicit default emits on 3.12/3.13.
                        tf.extractall(root, filter="data")
                    tarball.unlink(missing_ok=True)
                if not emb.is_file():
                    urllib.request.urlretrieve(_EMB_URL, emb)
                return _has_weights(root)
            except Exception:
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)

    async def delete_model(self, model_name: str) -> bool:
        """Remove the pair from the home. Nothing outside the home is deleted."""
        import shutil
        root = _models_dir()
        if not root.is_dir():
            return False
        shutil.rmtree(root, ignore_errors=True)
        return True

    async def diarize(self, audio_path: str, *, model: str = "", num_speakers: int | None = None,
                      min_speakers: int | None = None, max_speakers: int | None = None):
        # Like every media call, a diarization names its model (the diarization binding in
        # Settings → Models), and one that names none is refused before the model loads. It
        # used to run with this app's one model in its place.
        try:
            require_model(model)
        except ProviderResolutionError as exc:
            logger.warning("diarization-onnx refused: %s", exc)
            return None
        ensure_ffmpeg_in_path()
        maxs = max_speakers or num_speakers or (self._config.get("max_speakers") or None)

        def _run():
            try:
                import sherpa_onnx
                import soundfile as sf
                if not _downloaded():
                    return None
                root = _models_dir()
                seg_path, emb_path = root / _SEG_REL, root / _EMB_REL
                clustering = (sherpa_onnx.FastClusteringConfig(num_clusters=int(maxs))
                              if maxs else sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.5))
                cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg_path))),
                    embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb_path)),
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
