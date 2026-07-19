"""pyannote speaker-diarization provider (serves the ``diarization`` use-case).

The higher-ceiling, HF-gated diarization backend: the pyannote.audio pretrained pipeline.
Requires a HuggingFace token + license acceptance (pyannote/speaker-diarization-3.1). A
SECOND, independent provider for the ``diarization`` capability alongside the ONNX one —
uniform with how multiple apps can serve stt. Heavy torch deps install only with this app.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from personalclaw.sdk.diarization import (
    DiarizationModel,
    DiarizationProvider,
    LocalModelProvider,
    SpeakerTurn,
    ensure_ffmpeg_in_path,
)

logger = logging.getLogger(__name__)

_MODEL = "pyannote/speaker-diarization-3.1"


def create_provider(config: dict[str, Any] | None = None) -> "PyannoteDiarizationProvider":
    return PyannoteDiarizationProvider(config or {})


def availability() -> tuple[bool, str]:
    """Whether pyannote diarization can run here (needs pyannote.audio + torch)."""
    try:
        import pyannote.audio  # noqa: F401
        return True, ""
    except ImportError:
        return False, ("pyannote diarization needs personalclaw[diarization-pyannote] "
                       "(pyannote.audio + torch) — a large install; server/container build.")


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
        return str(self._config.get("hf_token") or os.environ.get("HF_TOKEN") or "").strip()

    async def is_available(self) -> bool:
        ok, _ = availability()
        return ok

    async def list_models(self) -> list[DiarizationModel]:
        has_token = bool(self._hf_token())
        return [DiarizationModel(
            name=_MODEL, size_mb=30, gated=True,
            description=("pyannote 3.1 — higher accuracy; needs a HuggingFace token + license "
                         "acceptance." + ("" if has_token else " (token not set in app settings)")),
            downloaded=self._cached() if has_token else False,
        )]

    def _cached(self) -> bool:
        try:
            from huggingface_hub import try_to_load_from_cache
            hit = try_to_load_from_cache(_MODEL, "config.yaml")
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
                snapshot_download(_MODEL, token=token)
                return True
            except Exception:
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)

    async def delete_model(self, model_name: str) -> bool:
        return False  # HF cache managed by huggingface_hub; no app-owned dir to prune

    async def diarize(self, audio_path: str, *, model: str = "", num_speakers: int | None = None,
                      min_speakers: int | None = None, max_speakers: int | None = None):
        ensure_ffmpeg_in_path()
        token = self._hf_token()
        if not token:
            return None

        def _run():
            try:
                from pyannote.audio import Pipeline
                # pyannote.audio ≥3.1 renamed the auth kwarg ``use_auth_token`` → ``token``;
                # try the current name, fall back for older installs. (Passing the wrong
                # kwarg raises TypeError → the whole diarize silently returned None → 0
                # turns; that was invisible until we logged the exception below.)
                try:
                    pipeline = Pipeline.from_pretrained(_MODEL, token=token)
                except TypeError:
                    pipeline = Pipeline.from_pretrained(_MODEL, use_auth_token=token)
                if pipeline is None:
                    logger.warning("pyannote from_pretrained returned None — accept the "
                                   "%s license on HuggingFace with this token.", _MODEL)
                    return None
                kwargs: dict = {}
                if num_speakers:
                    kwargs["num_speakers"] = int(num_speakers)
                if min_speakers:
                    kwargs["min_speakers"] = int(min_speakers)
                if max_speakers:
                    kwargs["max_speakers"] = int(max_speakers)
                diarization = pipeline(audio_path, **kwargs)
                # pyannote.audio 4.x wraps the result in a ``DiarizeOutput`` whose
                # ``.speaker_diarization`` is the ``Annotation`` (with ``itertracks``); 3.x
                # returned that Annotation directly. Unwrap the 4.x shape, fall back to the
                # object itself for the older direct-Annotation return.
                annotation = getattr(diarization, "speaker_diarization", diarization)
                return [SpeakerTurn(start=float(t.start), end=float(t.end), speaker=str(spk))
                        for t, _, spk in annotation.itertracks(yield_label=True)]
            except Exception as exc:
                # A GATED-repo error is actionable + distinct from a real failure: pyannote.audio
                # 4.x pulls a nested embedding model (speaker-diarization-community-1) that needs
                # its OWN license-acceptance click on HuggingFace — the token alone isn't enough.
                # Surface the exact repo + URL so the operator knows to accept conditions, rather
                # than burying it in a generic stack trace (the diarize just returned None before).
                msg = str(exc)
                if "gated" in msg.lower() or "403" in msg or "accept" in msg.lower():
                    import re as _re
                    m = _re.search(r"(pyannote/[\w.-]+)", msg)
                    repo = m.group(1) if m else "the pyannote model"
                    logger.warning(
                        "pyannote diarize blocked: the HF token can't download %s — accept its "
                        "user conditions at https://hf.co/%s (a one-time click on the HF website), "
                        "then retry. pyannote.audio 4.x needs the embedding sub-model's license too.",
                        repo, repo)
                else:
                    logger.warning("pyannote diarize failed", exc_info=True)
                return None

        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _run), timeout=900)
        except asyncio.TimeoutError:
            return None
