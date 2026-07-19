"""Faster-Whisper STT provider — CTranslate2-backed in-process Whisper."""

import asyncio
import os
from pathlib import Path
from typing import Any

from personalclaw.sdk.local_model import LocalModelProvider
from personalclaw.sdk.stt import (
    SttModel,
    SttProvider,
    TranscriptResult,
    TranscriptSegment,
    TranscriptWord,
    ensure_ffmpeg_in_path,
)


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

_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface" / "hub"


def _model_downloaded(model_name: str) -> bool:
    """Check if a faster-whisper model has been downloaded (from HuggingFace cache)."""
    repo_id = f"Systran/faster-whisper-{model_name}"
    safe_name = repo_id.replace("/", "--")
    model_dir = _CACHE_DIR / f"models--{safe_name}"
    return model_dir.is_dir()


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
        """Where downloaded weights land (HuggingFace hub cache) — lets the core
        download UI track byte progress without knowing this backend's layout."""
        return str(_CACHE_DIR)

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
            result.append(SttModel(
                name=m.name,
                size_mb=m.size_mb,
                description=m.description,
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
                WhisperModel(model_name, device="cpu", compute_type="int8")
                return True
            except Exception:
                return False

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=600)
        except asyncio.TimeoutError:
            return False

    async def delete_model(self, model_name: str) -> bool:
        import shutil
        repo_id = f"Systran/faster-whisper-{model_name}"
        safe_name = repo_id.replace("/", "--")
        model_dir = _CACHE_DIR / f"models--{safe_name}"
        if model_dir.is_dir():
            shutil.rmtree(model_dir)
            return True
        return False

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
        hotwords). Maps native faster-whisper Segment/Word objects → TranscriptResult."""
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return None

        ensure_ffmpeg_in_path()
        model_name = model or "turbo"
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
                m = WhisperModel(model_name, device="cpu", compute_type="int8")
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
