"""pyannote speaker-diarization provider (serves the ``diarization`` use-case).

The higher-ceiling, HF-gated diarization backend: the pyannote.audio pretrained pipeline.
Requires a HuggingFace token + license acceptance (pyannote/speaker-diarization-3.1). A
SECOND, independent provider for the ``diarization`` capability alongside the ONNX one —
uniform with how multiple apps can serve stt. Heavy torch deps install only with this app.

The pipeline itself runs in a child process (``worker.py``, through the SDK's ``run_once``): its
clustering holds the interpreter lock while it runs, and in a thread of the gateway that stopped
the gateway answering anything for as long.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from personalclaw.sdk.availability import missing_modules
from personalclaw.sdk.credentials import resolve_token
from personalclaw.sdk.diarization import (
    DiarizationError,
    DiarizationModel,
    DiarizationProvider,
    LocalModelProvider,
    SpeakerTurn,
)
from personalclaw.sdk.model import ProviderResolutionError, require_model
from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.sidecar import SidecarCrashed, SidecarWorkerError, run_once
from personalclaw.sdk.util import config_dir

logger = logging.getLogger(__name__)

#: pyannote.audio (from 4.0) reports each model and pipeline it loads, and each file it diarizes
#: (how long it is, how many speakers), to its makers unless this says otherwise. It reads the
#: setting at every report, and turns it on as it loads when nothing has set it, so it is set here,
#: before the library can load, and over an inherited value: PersonalClaw sends no telemetry.
os.environ["PYANNOTE_METRICS_ENABLED"] = "false"

_MODEL = "pyannote/speaker-diarization-3.1"

#: The diarization's own process: what ``run_once`` runs, beside this file.
_WORKER = Path(__file__).with_name("worker.py")

_COULD_NOT_TELL = "Diarization (pyannote) could not tell the speakers apart in this recording."


def _models_dir() -> Path:
    """Where the pipeline, and the models it pulls in, are downloaded to and read from: the
    PersonalClaw home (``config_dir()``), so an isolated home is actually isolated.

    Earlier releases used the Hugging Face folder other tools share. It is outside the home, so
    it is neither read nor written any more: an install that has the pipeline only there
    downloads it once more, into the home."""
    return config_dir() / "models" / "diarization-pyannote"


def create_provider(config: dict[str, Any] | None = None) -> "PyannoteDiarizationProvider":
    return PyannoteDiarizationProvider(config or {})


#: What the pipeline runs on: each module, by the package this app installs for it.
_RUNTIME = {"pyannote.audio": "pyannote.audio", "torch": "torch"}


def availability() -> tuple[bool, str]:
    """Whether pyannote diarization can run here, + a UI reason if not.

    Found without importing either package, as the SDK's availability contract asks: importing
    pyannote.audio imports torch, the heaviest import there is, to answer one question on every
    Models page (``pyannote`` itself is a namespace package, which runs nothing as it is found).
    """
    missing = [_RUNTIME[module] for module in missing_modules(*_RUNTIME)]
    if not missing:
        return True, ""
    ships, it = ("ships", "it") if len(missing) == 1 else ("ship", "them")
    return False, (
        f"pyannote diarization needs {' and '.join(missing)}, which {ships} with this app, not "
        "with PersonalClaw itself. Reinstall Diarization (pyannote) from the Store. The desktop "
        f"app cannot install {it}: use the server or container build there."
    )


def _turns(answer: Any) -> list[SpeakerTurn]:
    """The speaker turns the diarization's process answered with, or the sentence for what
    stopped them (``worker.diarize`` says which). What it sends back is only ever data, and it is
    read as such: anything that is not a list of turns is a failure, not a guess."""
    if not isinstance(answer, dict):
        raise DiarizationError(_COULD_NOT_TELL)
    if answer.get("refused"):
        raise DiarizationError(
            f"Hugging Face would not give this token {_MODEL}. Accept its user conditions at "
            f"https://hf.co/{_MODEL} with the same account, then try again."
        )
    if "gated" in answer:
        repo = str(answer["gated"] or "") or "the pyannote model"
        logger.warning("pyannote diarize blocked: the HF token can't download %s", repo)
        raise DiarizationError(
            f"This Hugging Face token can't download {repo}. Accept its user conditions at "
            f"https://hf.co/{repo} (a one-time click on the Hugging Face website), then try again: "
            "pyannote.audio 4.x needs the embedding model's conditions accepted too."
        )
    if "failed" in answer:
        logger.warning("pyannote diarize failed: %s", answer["failed"])
        raise DiarizationError(sentence_with_detail(_COULD_NOT_TELL, str(answer["failed"])))
    try:
        return [
            SpeakerTurn(start=float(start), end=float(end), speaker=str(speaker))
            for start, end, speaker in answer.get("turns") or []
        ]
    except (TypeError, ValueError) as exc:
        raise DiarizationError(_COULD_NOT_TELL) from exc


class PyannoteDiarizationProvider(DiarizationProvider, LocalModelProvider):
    def __init__(self, config: dict[str, Any]):
        self._config = config

    @property
    def name(self) -> str:
        return "diarization-pyannote"

    @property
    def display_name(self) -> str:
        return "Diarization (pyannote)"

    def _hf_token(self) -> str:
        """This app's own setting, else the SHARED SDK cascade (LOCAL-MODEL-MANAGER-V2 §5).

        The second term used to be a private ``os.environ["HF_TOKEN"]`` read, which saw
        neither the managed credential store nor a ``huggingface-cli login``. It now
        delegates to :func:`personalclaw.sdk.credentials.resolve_token` — the one shared
        resolver (credential store → ``HF_TOKEN``/``HUGGING_FACE_HUB_TOKEN`` → the
        ``huggingface-cli`` token file, preferring a whoami-valid source) — so this provider
        resolves the token identically to every other HF-touching surface instead of
        re-deriving a narrower answer.

        The app's declared ``hf_token`` setting stays FIRST because it is the one source
        nothing else can see: it is persisted to this bundle's ``data/config.json`` and
        mirrored nowhere — not into the credential store, not into ``os.environ`` — so the
        cascade is structurally blind to it. Dropping this term would leave the manifest's
        documented, ``sensitive``-flagged HuggingFace Token field as a dead control that
        silently ignored whatever the operator pasted into it.
        """
        return str(self._config.get("hf_token") or "").strip() or resolve_token()

    async def is_available(self) -> bool:
        ok, _ = availability()
        return ok

    def cache_dir(self) -> str:
        """Where downloaded weights land — the core download UI reads this for byte
        progress, and core's delete sweeps it."""
        return str(_models_dir())

    async def list_models(self) -> list[DiarizationModel]:
        has_token = bool(self._hf_token())
        return [DiarizationModel(
            name=_MODEL, size_mb=30, gated=True,
            # "in app settings" would now be a lie: the token may equally come from the
            # credential store, the environment, or a `huggingface-cli login`.
            description=("pyannote 3.1 — higher accuracy; needs a HuggingFace token + license "
                         "acceptance." + ("" if has_token else " (no HuggingFace token found)")),
            downloaded=self._cached() if has_token else False,
        )]

    def _cached(self) -> bool:
        try:
            from huggingface_hub import try_to_load_from_cache
            hit = try_to_load_from_cache(_MODEL, "config.yaml", cache_dir=str(_models_dir()))
            return isinstance(hit, str)
        except Exception:
            return False

    async def download_model(self, model_name: str) -> bool:
        token = self._hf_token()
        if not token:
            return False  # gated: the UI greys this out until a token is set

        def _run() -> bool:
            try:
                from huggingface_hub import snapshot_download
                snapshot_download(_MODEL, token=token, cache_dir=str(_models_dir()))
                return True
            except Exception:
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)

    async def delete_model(self, model_name: str) -> bool:
        """Remove the pipeline and the models it pulled in from the home. Nothing outside the
        home is deleted."""
        import shutil

        root = _models_dir()
        if not root.is_dir():
            return False
        shutil.rmtree(root, ignore_errors=True)
        return True

    async def diarize(self, audio_path: str, *, model: str = "", num_speakers: int | None = None,
                      min_speakers: int | None = None, max_speakers: int | None = None):
        # Like every media call, a diarization names its model (the diarization binding in
        # Settings → Models), and one that names none is refused before the pipeline is fetched.
        # It used to fetch and run this app's one pipeline in its place. A model this app does
        # not have is refused the same way, before the token is asked for: running its own
        # pipeline instead answered for a model nobody chose, and a missing token is not the
        # reason such a call cannot run.
        try:
            require_model(model)
        except ProviderResolutionError as exc:
            logger.warning("diarization-pyannote refused: %s", exc)
            return None
        if model != _MODEL:
            logger.warning(
                "diarization-pyannote refused: it has one model, %s, and this call named %r. "
                "Choose %s for Diarization in Settings → Models.", _MODEL, model, _MODEL,
            )
            return None
        token = self._hf_token()
        if not token:
            # Said, not answered with ``None``: that read as a recording with no speakers.
            raise DiarizationError(
                "Diarization (pyannote) needs a Hugging Face token, and none was found. Set its "
                "HuggingFace Token in the app's settings, then try again."
            )

        # In a process of its own (``worker.py``, through the SDK's ``run_once``): the pipeline's
        # clustering holds the interpreter lock while it runs, and in a thread of the gateway that
        # stopped every request the gateway had. No clock of its own: the caller's budget grows
        # with the recording's length, and a cancelled call stops the process.
        try:
            answer = await run_once(
                self.name,
                _WORKER,
                "diarize",
                {
                    "audio": audio_path,
                    "model": _MODEL,
                    "token": token,
                    "cache": str(_models_dir()),
                    "num_speakers": int(num_speakers or 0),
                    "min_speakers": int(min_speakers or 0),
                    "max_speakers": int(max_speakers or 0),
                },
            )
        except SidecarWorkerError as exc:
            logger.warning("diarization-pyannote could not diarize %s: %s", audio_path, exc)
            raise DiarizationError(sentence_with_detail(_COULD_NOT_TELL, exc)) from exc
        except SidecarCrashed as exc:
            logger.warning("diarization-pyannote's process ended diarizing %s: %s", audio_path, exc)
            raise DiarizationError(
                "Diarization (pyannote) stopped before it finished: the process it ran in ended "
                f"({exc.reason})."
            ) from exc
        return _turns(answer)
