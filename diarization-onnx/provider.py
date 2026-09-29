"""ONNX speaker-diarization provider (serves the ``diarization`` use-case).

The non-gated, install-and-go diarization backend: a sherpa-onnx segmentation +
speaker-embedding + clustering pipeline. No HuggingFace token — downloads freely, matching
PClaw's OSS/local-first ethos. One of (potentially) several providers for the ``diarization``
capability, exactly like faster-whisper is one STT provider (mirrors its app shape).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

from personalclaw.sdk.availability import missing_modules
from personalclaw.sdk.diarization import (
    DiarizationError,
    DiarizationModel,
    DiarizationProvider,
    LocalModelProvider,
    SpeakerTurn,
    ensure_ffmpeg_in_path,
)
from personalclaw.sdk.model import ProviderResolutionError, require_model
from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.util import child_process_env, config_dir

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

#: What the pipeline runs on: each module, by the package that installs it. sherpa-onnx carries
#: an ONNX Runtime build of its own, so the ``onnxruntime`` package is neither declared nor
#: loaded: loading it started its maker's telemetry, a device identifier kept in the user's home.
_RUNTIME = {"sherpa_onnx": "sherpa-onnx", "numpy": "numpy"}

#: Why nothing can be diarized without ffmpeg, which decodes every recording (below).
_NEEDS_FFMPEG = (
    "ONNX diarization needs ffmpeg to read recordings, and it isn't installed here. Install "
    "ffmpeg (for example with your system's package manager), then try again."
)
_NO_AUDIO = (
    "ONNX diarization found no audio it could read in this recording. The file may be cut off "
    "or damaged."
)


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
    """Whether ONNX diarization can run here: sherpa-onnx and numpy are installed, and ffmpeg
    is there to read recordings. Found without importing either package, as the SDK's
    availability contract asks: an import runs the library, and this is asked on every Models
    page."""
    missing = [_RUNTIME[module] for module in missing_modules(*_RUNTIME)]
    if missing:
        ships = "ships" if len(missing) == 1 else "ship"
        return False, (
            f"ONNX diarization needs {' and '.join(missing)}, which {ships} with this app, not "
            "with PersonalClaw itself. Reinstall Diarization (ONNX) from the Store."
        )
    ensure_ffmpeg_in_path()
    if shutil.which("ffmpeg") is None:
        return False, _NEEDS_FFMPEG
    return True, ""


def _decode(audio_path: str, sample_rate: int):
    """The recording as mono float32 samples at *sample_rate*, decoded by ffmpeg.

    A recording arrives in whatever format was uploaded (``.m4a``, ``.mp3``, ``.webm``, a
    video's extracted ``.wav``). This app used to read it with soundfile, whose libsndfile
    cannot open AAC, so every ``.m4a`` voice memo failed before diarization began, and the
    failure was swallowed into "no speakers". It also never checked the rate: the model hears
    only its own (``sample_rate``), and a 22.05 kHz file handed over as-is is heard slowed and
    lower. ffmpeg reads every format the product accepts and resamples on the way."""
    import numpy as np

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise DiarizationError(_NEEDS_FFMPEG)
    decoded = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", audio_path, "-vn", "-ac", "1",
         "-ar", str(int(sample_rate)), "-f", "f32le", "-"],
        capture_output=True,
        check=False,
        env=child_process_env(),
    )
    if decoded.returncode != 0:
        raise DiarizationError(sentence_with_detail(
            "ONNX diarization could not read this recording.",
            _ffmpeg_said(decoded.stderr, decoded.returncode),
        ))
    if not decoded.stdout:
        # ffmpeg succeeds on a file cut off after its header (it warns "partial file") and
        # hands back no audio at all. Diarizing nothing found no speakers and read "done".
        raise DiarizationError(_NO_AUDIO)
    return np.frombuffer(decoded.stdout, dtype=np.float32)


def _ffmpeg_said(stderr: bytes, status: int) -> str:
    """ffmpeg's last word on a failed decode, for the item's status line: its closing summary
    line, without the ``[in#0 @ 0x…]`` tags it starts lines with. The raw text ran several lines,
    named the recording's storage path, and pushed the other steps' reasons past the line's end."""
    lines = [re.sub(r"^(\[[^\]]*\]\s*)+", "", line).strip()
             for line in stderr.decode("utf-8", "replace").splitlines()]
    lines = [line for line in lines if line]
    return lines[-1] if lines else f"ffmpeg exited with status {status}"


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
        # used to run with this app's one model in its place. A model this app does not have is
        # refused the same way, and said first: running its own one model instead answered for
        # a model nobody chose.
        try:
            require_model(model)
        except ProviderResolutionError as exc:
            logger.warning("diarization-onnx refused: %s", exc)
            return None
        if model != _MODEL:
            logger.warning(
                "diarization-onnx refused: it has one model, %s, and this call named %r. "
                "Choose %s for Diarization in Settings → Models.", _MODEL, model, _MODEL,
            )
            return None
        ensure_ffmpeg_in_path()
        maxs = max_speakers or num_speakers or (self._config.get("max_speakers") or None)

        def _run():
            if not _downloaded():
                raise DiarizationError(
                    f"The diarization model {_MODEL} isn't downloaded. Download it under Speaker "
                    "diarization in Settings → Models, then try again."
                )
            try:
                import sherpa_onnx
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
                samples = _decode(audio_path, sd.sample_rate)
                res = sd.process(samples).sort_by_start_time()
                return [SpeakerTurn(start=float(r.start), end=float(r.end),
                                    speaker=f"SPEAKER_{r.speaker:02d}") for r in res]
            except DiarizationError:
                raise
            except Exception as exc:
                logger.warning("diarization-onnx could not diarize %s", audio_path, exc_info=True)
                raise DiarizationError(sentence_with_detail(
                    "ONNX diarization could not tell the speakers apart in this recording.", exc,
                )) from exc

        # No clock of its own: the caller's budget grows with the recording's length, and a
        # timer here could only throw a finished result away (a thread cannot be cancelled).
        return await asyncio.get_running_loop().run_in_executor(None, _run)
