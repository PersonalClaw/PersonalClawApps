"""Faster-Whisper STT provider — CTranslate2-backed in-process Whisper."""

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from personalclaw.sdk.credentials import resolve_token
from personalclaw.sdk.local_model import LocalModelProvider
from personalclaw.sdk.model import ProviderResolutionError, require_model
from personalclaw.sdk.stt import (
    SttModel,
    SttProvider,
    TranscriptResult,
    TranscriptSegment,
    TranscriptWord,
    ensure_ffmpeg_in_path,
)
from personalclaw.sdk.util import config_dir, outside_home_path

logger = logging.getLogger(__name__)

#: The place core lets the owner turn on in Settings → Security → Outside PersonalClaw's home:
#: the Hugging Face folder other tools share. Off, this app reads nothing outside the home.
_SHARED_HF = "huggingface-cache"


def create_provider(config: dict[str, Any] | None = None) -> "FasterWhisperProvider":
    return FasterWhisperProvider()


def availability() -> tuple[bool, str]:
    """Whether in-process Whisper STT can run here, + a UI reason if not.

    Backed by ``faster-whisper`` (CTranslate2). Builds without it — e.g. the
    desktop PyInstaller bundle — surface this so the Settings card greys out and
    blocks model downloads instead of offering buttons that only ever 500.
    """
    try:
        import faster_whisper  # noqa: F401
        return True, ""
    except ImportError:
        return False, "In-process STT needs the personalclaw[stt] package (not bundled with the desktop app — use the server or container build)."

_MODELS = [
    SttModel(name="tiny", size_mb=75, description="Fastest, lowest accuracy"),
    SttModel(name="base", size_mb=142, description="Fast with reasonable accuracy"),
    SttModel(name="small", size_mb=466, description="Good balance of speed and accuracy"),
    SttModel(name="medium", size_mb=1500, description="High accuracy, slower"),
    SttModel(name="large-v3", size_mb=2900, description="Highest accuracy (v3)"),
    SttModel(name="turbo", size_mb=1600, description="Optimized large model (recommended)"),
]


def _models_dir() -> Path:
    """Where downloaded weights are WRITTEN: the PersonalClaw home (``config_dir()``), so an
    isolated home is actually isolated. This used to root at ``XDG_CACHE_HOME``/``~/.cache``,
    which no PersonalClaw home can reach."""
    return config_dir() / "models" / "stt"


def _shared_hub() -> Path | None:
    """The model cache of the Hugging Face folder other tools share, when the owner allowed
    PersonalClaw to read it, else ``None``. Earlier releases downloaded there, so an install
    that allows it keeps using those weights without a download. Only ever read: a model is
    loaded from its snapshot folder in place (:func:`_load_target`), and nothing is written,
    copied or deleted there."""
    shared = outside_home_path(_SHARED_HF)
    return shared / "hub" if shared is not None else None


def _repo_id(model_name: str) -> str:
    """Resolve the repository exactly as the installed faster-whisper release does."""
    fallback = f"Systran/faster-whisper-{model_name}"
    try:
        from faster_whisper.utils import _MODELS as upstream_models
    except (ImportError, AttributeError) as exc:
        logger.warning(
            "faster_whisper.utils._MODELS is unavailable for %r (%s); "
            "using fallback repository %r",
            model_name,
            exc,
            fallback,
        )
        return fallback

    if not isinstance(upstream_models, Mapping):
        logger.warning(
            "faster_whisper.utils._MODELS has unexpected type %s for %r; "
            "using fallback repository %r",
            type(upstream_models).__name__,
            model_name,
            fallback,
        )
        return fallback

    repo_id = upstream_models.get(model_name)
    if not isinstance(repo_id, str) or "/" not in repo_id:
        logger.warning(
            "faster_whisper.utils._MODELS has no usable repository for %r; "
            "using fallback repository %r",
            model_name,
            fallback,
        )
        return fallback
    return repo_id


def _repo_dir(root: Path, model_name: str) -> Path:
    """HuggingFace's on-disk cache layout for a repo id — ``models--{org}--{repo}`` —
    rebuilt UNDERNEATH *root*. ctranslate2 resolves a snapshot through ``huggingface_hub``,
    so the layout is the contract but the root is ours to pick. The installed library's
    model map owns the repo id because public names do not all follow one template."""
    repo_id = _repo_id(model_name)
    return root / ("models--" + repo_id.replace("/", "--"))


def _has_weights(d: Path) -> bool:
    """True only if a snapshot dir holds real weights. A bare ``models--…`` shell is what
    an interrupted download leaves behind, and accepting it is how a legacy fallback
    quietly resolves to nothing."""
    if not d.is_dir():
        return False
    return any(d.rglob("model.bin")) or any(d.rglob("*.safetensors"))


def _shared_snapshot(model_name: str) -> Path | None:
    """The snapshot folder holding *model_name*'s weights in the allowed shared cache, or
    ``None``. The revision ``refs/main`` names comes first, as ``huggingface_hub`` would pick."""
    hub = _shared_hub()
    if hub is None:
        return None
    repo = _repo_dir(hub, model_name)
    try:
        snapshots = sorted(p for p in (repo / "snapshots").iterdir() if p.is_dir())
    except OSError:
        return None
    try:
        pinned = (repo / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        pinned = ""
    for snapshot in sorted(snapshots, key=lambda p: p.name != pinned):
        if _has_weights(snapshot):
            return snapshot
    return None


def _in_home(model_name: str) -> bool:
    return _has_weights(_repo_dir(_models_dir(), model_name))


def _load_target(model_name: str) -> tuple[str, dict[str, Any]]:
    """What ``WhisperModel`` is handed to LOAD *model_name*: the name, with the home as the
    root a missing model downloads into, unless the weights are only in the shared folder the
    owner allowed. Then it is that snapshot's own folder, which faster-whisper loads in place
    without asking ``huggingface_hub`` anything, so nothing is fetched or written there."""
    if not _in_home(model_name):
        shared = _shared_snapshot(model_name)
        if shared is not None:
            return str(shared), {}
    return model_name, _home_fetch_kwargs()


def _home_fetch_kwargs() -> dict[str, Any]:
    """How a fetch into the home authenticates: the token PersonalClaw resolves, or none.
    ``False`` keeps ``huggingface_hub`` from reading ``huggingface-cli login``'s token file,
    which is outside the home and read only when the owner allows its folder."""
    root = _models_dir()
    root.mkdir(parents=True, exist_ok=True)
    return {"download_root": str(root), "use_auth_token": resolve_token() or False}


def _model_downloaded(model_name: str) -> bool:
    """Whether usable weights exist in the home, or in the shared folder the owner allowed.
    Counting the allowed shared copy is what keeps an upgrade free: without it every model an
    earlier release downloaded there reads as missing and Settings invites a re-fetch."""
    return _in_home(model_name) or _shared_snapshot(model_name) is not None


class FasterWhisperProvider(SttProvider, LocalModelProvider):
    @property
    def name(self) -> str:
        return "faster_whisper"

    @property
    def display_name(self) -> str:
        return "Faster Whisper"

    @property
    def supports_streaming(self) -> bool:
        return True

    def cache_dir(self) -> str:
        """Where downloaded weights land — lets the core download UI track byte progress
        without knowing this backend's layout.

        ALWAYS the home, never the shared folder, because every download writes here (see
        :meth:`download_model`) even when the shared folder already holds a copy. It is also
        the root core's delete sweeps, which must never be a folder outside the home."""
        return str(_models_dir())

    async def is_available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    async def list_models(self) -> list[SttModel]:
        # No active-binding lookup here: core's Settings/discovery layer marks which
        # model is active (from active_models.json). The app only reports the catalog
        # + local download state.
        result = []
        for m in _MODELS:
            shared_only = not _in_home(m.name) and _shared_snapshot(m.name) is not None
            result.append(SttModel(
                name=m.name,
                size_mb=m.size_mb,
                # Said on the row, because Delete cannot remove this copy: it is outside the home.
                description=(m.description + ". Read from the Hugging Face folder other tools "
                             "share") if shared_only else m.description,
                downloaded=_model_downloaded(m.name),
                active=False,
            ))
        return result

    async def download_model(self, model_name: str) -> bool:
        if model_name not in {m.name for m in _MODELS}:
            return False

        def _download():
            try:
                from faster_whisper import WhisperModel
                # ``download_root`` becomes huggingface_hub's ``cache_dir``, which rebuilds
                # the models--<org>--<repo> layout underneath it. A deliberate download
                # always fills the home, never the shared folder, so cache_dir()'s byte
                # progress tracks the tree that is actually growing.
                WhisperModel(model_name, device="cpu", compute_type="int8",
                             **_home_fetch_kwargs())
                return True
            except Exception:
                return False

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=600)
        except asyncio.TimeoutError:
            return False

    async def delete_model(self, model_name: str) -> bool:
        """Remove the model from the home. A copy in the Hugging Face folder other tools share
        is never deleted: it is outside the home and other tools use it. The model then still
        reads as downloaded, and its row says where it is read from."""
        import shutil
        model_dir = _repo_dir(_models_dir(), model_name)
        if not model_dir.is_dir():
            return False
        shutil.rmtree(model_dir, ignore_errors=True)
        return True

    async def transcribe(self, audio_path: str, model: str = "", language: str = "") -> str | None:
        # Flat path: run the detailed transcription and return just its text, so there is
        # ONE decode implementation. Detailed is a superset (segments + words + VAD).
        result = await self.transcribe_detailed(audio_path, model=model, language=language)
        return result.text if result is not None and result.text else None

    # faster-whisper emits segments, per-word timestamps, and accepts a bias prompt.
    @property
    def supports_segments(self) -> bool:
        return True

    @property
    def supports_word_timestamps(self) -> bool:
        return True

    @property
    def supports_bias_terms(self) -> bool:
        return True

    async def transcribe_detailed(
        self,
        audio_path: str,
        *,
        model: str = "",
        language: str = "",
        bias_terms: list[str] | None = None,
    ) -> "TranscriptResult | None":
        """Rich transcription with VAD (silence removal → fewer hallucinations, tighter
        timestamps), per-word timestamps, and optional Lexicon bias (initial_prompt +
        hotwords). Maps native faster-whisper Segment/Word objects → TranscriptResult.

        ``model`` is the speech-to-text binding's model. Like chat, a call that names none is
        refused before anything is loaded; this used to load ``turbo`` in its place.
        """
        try:
            model_name = require_model(model)
        except ProviderResolutionError as exc:
            logger.error("faster-whisper refused: %s", exc)
            return None
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return None

        ensure_ffmpeg_in_path()
        lang = language.split("-")[0] if language else None
        # Whisper's decoder caps the PROMPT window at max_length//2 = 224 tokens; a bias
        # string that (with the forced decoder tokens) pushes a position >= 448 raises
        # "No position encodings are defined for positions >= 448" and the whole decode
        # fails. So keep the bias SMALL and pass it through ONLY ONE lever — never both
        # ``initial_prompt`` AND ``hotwords`` (that doubled the budget and overflowed).
        # ~200 chars of comma-separated terms is well under budget while still biasing.
        bias_prompt = ", ".join(bias_terms)[:200].rstrip(", ") if bias_terms else None

        def _run() -> "TranscriptResult | None":
            try:
                # The home's copy, else the allowed shared folder's snapshot loaded in place
                # (nothing fetched, copied or written there), else a download into the home.
                target, fetch = _load_target(model_name)
                m = WhisperModel(target, device="cpu", compute_type="int8", **fetch)
                kwargs: dict = {"language": lang, "word_timestamps": True, "vad_filter": True}
                if bias_prompt:
                    # Prefer ``hotwords`` (the purpose-built biasing lever) when the
                    # installed faster-whisper accepts it; else fall back to
                    # ``initial_prompt``. Exactly ONE carries the bias — passing both
                    # double-counts against the 224-token prompt window and overflows.
                    used_hotwords = False
                    try:
                        import inspect
                        if "hotwords" in inspect.signature(m.transcribe).parameters:
                            kwargs["hotwords"] = bias_prompt
                            used_hotwords = True
                    except (ValueError, TypeError):
                        pass
                    if not used_hotwords:
                        kwargs["initial_prompt"] = bias_prompt
                seg_iter, info = m.transcribe(audio_path, **kwargs)
                segments: list[TranscriptSegment] = []
                text_parts: list[str] = []
                for seg in seg_iter:
                    words = [
                        TranscriptWord(
                            start=float(w.start), end=float(w.end),
                            word=w.word, prob=float(getattr(w, "probability", 1.0) or 1.0),
                        )
                        for w in (getattr(seg, "words", None) or [])
                    ]
                    segments.append(TranscriptSegment(
                        start=float(seg.start), end=float(seg.end),
                        text=seg.text.strip(), words=words,
                    ))
                    text_parts.append(seg.text.strip())
                flat = " ".join(t for t in text_parts if t).strip()
                if not flat:
                    return None
                return TranscriptResult(
                    text=flat,
                    language=getattr(info, "language", "") or (lang or ""),
                    duration=float(getattr(info, "duration", 0.0) or 0.0),
                    segments=segments,
                )
            except Exception:
                return None

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=300)
        except asyncio.TimeoutError:
            return None
