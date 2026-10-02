"""ONNX speaker-diarization provider (serves the ``diarization`` use-case).

The non-gated, install-and-go diarization backend: a sherpa-onnx segmentation +
speaker-embedding + clustering pipeline. No HuggingFace token — downloads freely, matching
PClaw's OSS/local-first ethos. One of (potentially) several providers for the ``diarization``
capability, exactly like faster-whisper is one STT provider (mirrors its app shape).

The diarization itself runs in a child process (``worker.py``, through the SDK's ``run_once``):
sherpa-onnx holds the interpreter lock for the whole of it, and in a thread of the gateway that
stopped the gateway answering anything until the recording was done.
"""

from __future__ import annotations

import asyncio
import logging
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
    ffmpeg_not_found,
    find_ffmpeg,
)
from personalclaw.sdk.model import ProviderResolutionError, require_model
from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.sidecar import SidecarCrashed, SidecarWorkerError, run_once
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

#: What the pipeline runs on: each module, by the package that installs it. sherpa-onnx carries
#: an ONNX Runtime build of its own, so the ``onnxruntime`` package is neither declared nor
#: loaded: loading it started its maker's telemetry, a device identifier kept in the user's home.
_RUNTIME = {"sherpa_onnx": "sherpa-onnx", "numpy": "numpy"}

_NO_AUDIO = (
    "ONNX diarization found no audio it could read in this recording. The file may be cut off "
    "or damaged."
)
_UNREADABLE = "ONNX diarization could not read this recording."
_COULD_NOT_TELL = "ONNX diarization could not tell the speakers apart in this recording."

#: The diarization's own process: what ``run_once`` runs, beside this file.
_WORKER = Path(__file__).with_name("worker.py")


def _models_dir() -> Path:
    """Where the pair is downloaded to and read from: the PersonalClaw home
    (``config_dir()``), so an isolated home is actually isolated.

    Earlier releases wrote it to ``$XDG_CACHE_HOME/personalclaw/diarization-onnx`` and read
    through that folder. It is outside the home, so it is neither read nor deleted any more:
    an install that has the pair only there downloads it once (47 MB) into the home."""
    return config_dir() / "models" / "diarization-onnx"


def _needs_ffmpeg() -> str:
    """Why nothing can be diarized without ffmpeg, which decodes every recording (below): where
    PersonalClaw looked for it, and what to do, in the words core uses for every ffmpeg."""
    return f"ONNX diarization needs ffmpeg to read recordings. {ffmpeg_not_found()}"


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
    if find_ffmpeg() is None:
        return False, _needs_ffmpeg()
    return True, ""


def _downloaded() -> bool:
    """Whether a usable pair is in the home."""
    return _has_weights(_models_dir())


def _turns(answer: Any) -> list[SpeakerTurn]:
    """The speaker turns the diarization's process answered with, or the sentence for what
    stopped them (``worker.diarize`` says which). What it sends back is only ever data, and it is
    read as such: anything that is not a list of numeric turns is a failure, not a guess."""
    if not isinstance(answer, dict):
        raise DiarizationError(_COULD_NOT_TELL)
    if "unreadable" in answer:
        raise DiarizationError(sentence_with_detail(_UNREADABLE, str(answer["unreadable"])))
    if answer.get("no_audio"):
        raise DiarizationError(_NO_AUDIO)
    if "failed" in answer:
        raise DiarizationError(sentence_with_detail(_COULD_NOT_TELL, str(answer["failed"])))
    try:
        return [
            SpeakerTurn(start=float(start), end=float(end), speaker=f"SPEAKER_{int(speaker):02d}")
            for start, end, speaker in answer.get("turns") or []
        ]
    except (TypeError, ValueError) as exc:
        raise DiarizationError(_COULD_NOT_TELL) from exc


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
        if not _downloaded():
            raise DiarizationError(
                f"The diarization model {_MODEL} isn't downloaded. Download it under Speaker "
                "diarization in Settings → Models, then try again."
            )
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise DiarizationError(_needs_ffmpeg())
        maxs = max_speakers or num_speakers or (self._config.get("max_speakers") or None)
        root = _models_dir()
        try:
            # In a process of its own: sherpa-onnx holds the interpreter lock for the whole
            # diarization, which in a thread here stopped the gateway answering anything until the
            # recording was done. No clock of its own: the caller's budget grows with the
            # recording's length, and a caller that gives up (cancels) stops the child with it.
            answer = await run_once(
                self.name,
                _WORKER,
                "diarize",
                {
                    "audio": audio_path,
                    "ffmpeg": ffmpeg,
                    "segmentation": str(root / _SEG_REL),
                    "embedding": str(root / _EMB_REL),
                    "max_speakers": int(maxs or 0),
                },
            )
        except SidecarWorkerError as exc:
            logger.warning("diarization-onnx could not diarize %s: %s", audio_path, exc)
            raise DiarizationError(sentence_with_detail(_COULD_NOT_TELL, exc)) from exc
        except SidecarCrashed as exc:
            logger.warning("diarization-onnx's process ended diarizing %s: %s", audio_path, exc)
            raise DiarizationError(
                f"ONNX diarization stopped before it finished: the process it ran in ended "
                f"({exc.reason})."
            ) from exc
        return _turns(answer)

