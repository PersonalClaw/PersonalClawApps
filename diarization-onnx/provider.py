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
    """Where downloaded weights are WRITTEN — rooted at ``PERSONALCLAW_HOME`` so an
    isolated home is actually isolated. Same one-line idiom as the sibling model bundles
    (sentence-transformers, piper-tts, voice-clone-tts); this used to root at
    ``XDG_CACHE_HOME``/``~/.cache``, which no PersonalClaw home can reach."""
    home = os.environ.get("PERSONALCLAW_HOME", str(Path.home() / ".personalclaw"))
    return Path(home) / "models" / "diarization-onnx"


def _legacy_dir() -> Path:
    """Where the pair landed BEFORE it was rooted under ``PERSONALCLAW_HOME``: the
    machine-wide XDG cache. Read-through only (see :func:`_weights_root`) — an existing
    install keeps working with zero downloads, and nothing is ever copied out of here. A
    migration copy at startup is its own outage, and looks like a hang rather than an
    error."""
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "personalclaw" / "diarization-onnx"


def _has_weights(root: Path) -> bool:
    """True only when BOTH halves of the pair sit under *root* — a half-populated root is
    what an interrupted download leaves behind, and accepting it is how a legacy fallback
    quietly resolves to nothing."""
    return (root / _SEG_REL).is_file() and (root / _EMB_REL).is_file()


def _weights_root() -> Path:
    """The root the pipeline READS the pair from: the new ``PERSONALCLAW_HOME`` one, unless
    the pair lives only in the legacy cache — then read straight through it so an upgrade
    fetches nothing."""
    new = _models_dir()
    if _has_weights(new):
        return new
    return _legacy_dir() if _has_weights(_legacy_dir()) else new


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
    """Whether a usable pair exists in EITHER root. Accepting the legacy cache is what
    keeps an upgrade free: without it an already-downloaded pair reads as missing and the
    Settings UI invites the user to re-fetch it."""
    return _has_weights(_models_dir()) or _has_weights(_legacy_dir())


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
        progress. ALWAYS the new root, never the legacy one, because every download writes
        here (see :meth:`download_model`) even when the legacy cache already holds a copy.
        Returning the legacy dir whenever it happened to hold weights would aim the
        progress bar at a tree that never grows."""
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
                # A deliberate download always fills the NEW root — never the legacy one —
                # so cache_dir()'s byte progress tracks the tree that is actually growing.
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
                # Deliberately _has_weights(root), not _downloaded(): a legacy copy must
                # not let a failed write here report success.
                return _has_weights(root)
            except Exception:
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)

    async def delete_model(self, model_name: str) -> bool:
        import shutil
        # BOTH roots, for the same reason sentence-transformers removes both of its
        # layouts: _downloaded() reads through to the legacy cache, so leaving that copy
        # behind would keep the model reporting as "downloaded" right after a delete.
        removed = False
        for root in (_models_dir(), _legacy_dir()):
            if root.is_dir():
                shutil.rmtree(root, ignore_errors=True)
                removed = True
        return removed

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
                # Read-through: resolves to the legacy root when the pair lives ONLY there,
                # so an upgraded install uses what it already has instead of re-fetching.
                root = _weights_root()
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
