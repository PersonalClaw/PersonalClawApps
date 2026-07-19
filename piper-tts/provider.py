"""Piper TTS provider (app) — download/manage ONNX voices + local synthesis.

Piper is fully offline: a single static binary plus a per-voice ``.onnx`` model
(https://github.com/rhasspy/piper). This app owns everything piper-specific — the
voice catalog, the HuggingFace voice downloads, binary resolution, and the sandboxed
synthesis subprocess (which used to live in core ``voice_reply.py``). Core keeps only
the provider-agnostic streaming voice-reply orchestration, which drives this (and any
TTS) provider through ``TtsProvider.synthesize``.

Implements the ``TtsProvider`` ABC from ``personalclaw.sdk.tts``; the loader registers
the returned provider into core's tts registry (the ``tts``-capability seam) so the
Settings → Models ``tts`` binding resolves to it. The synthesis subprocess is wrapped
in the host sandbox via ``personalclaw.sdk.util.sandbox_wrap_argv``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from personalclaw.sdk.tts import LocalTtsProvider, TtsVoice
from personalclaw.sdk.util import sandbox_wrap_argv

logger = logging.getLogger(__name__)

# The piper voice catalog (moved out of core tts/registry.py — it's piper-specific).
PIPER_VOICES = [
    {"name": "en_US-lessac-medium", "size_mb": 75, "description": "English US, Lessac voice (medium quality)"},
    {"name": "en_US-amy-medium", "size_mb": 75, "description": "English US, Amy voice (medium quality)"},
    {"name": "en_US-ryan-medium", "size_mb": 75, "description": "English US, Ryan voice (medium quality)"},
    {"name": "en_US-lessac-high", "size_mb": 150, "description": "English US, Lessac voice (high quality)"},
    {"name": "en_GB-alba-medium", "size_mb": 75, "description": "English GB, Alba voice (medium quality)"},
    {"name": "de_DE-thorsten-medium", "size_mb": 75, "description": "German, Thorsten voice (medium quality)"},
    {"name": "fr_FR-siwis-medium", "size_mb": 75, "description": "French, Siwis voice (medium quality)"},
    {"name": "es_ES-davefx-medium", "size_mb": 75, "description": "Spanish, Davefx voice (medium quality)"},
]


def _voices_dir() -> Path:
    home = os.environ.get("PERSONALCLAW_HOME", str(Path.home() / ".personalclaw"))
    d = Path(home) / "models" / "tts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_voice_downloaded(voice_name: str) -> bool:
    voice_dir = _voices_dir() / voice_name
    if not voice_dir.is_dir():
        return False
    return any(voice_dir.rglob("*.onnx"))


def voice_model_path(voice_name: str) -> str:
    """Absolute path to a downloaded voice's ``.onnx``, or "" if absent."""
    voice_dir = _voices_dir() / voice_name
    if not voice_dir.is_dir():
        return ""
    for onnx in voice_dir.rglob("*.onnx"):
        return str(onnx)
    return ""


def _resolve_piper_binary(configured: str = "") -> str | None:
    """Return the piper binary path or None. Resolution order: explicit path →
    ``piper`` on PATH → the console script next to the interpreter (the piper-tts
    pip extra installs it into the venv bin/) → ``~/piper-venv/bin``."""
    if configured:
        p = os.path.expanduser(configured)
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    found = shutil.which("piper")
    if found:
        return found
    sibling = os.path.join(os.path.dirname(sys.executable), "piper")
    for c in (sibling, os.path.expanduser("~/piper-venv/bin/piper")):
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


async def _synthesize_piper_chunk(
    text: str,
    piper_model: str = "",
    length_scale: float = 1.0,
    output_path: str = "",
) -> str | None:
    """Run the local piper binary to synthesize *text* → WAV. Returns the path or None.

    Piper takes plain text on stdin. ``length_scale`` controls speed (<1 faster). The
    subprocess is wrapped in the host sandbox so a compromised model/binary can't reach
    private filesystem areas."""
    bin_path = _resolve_piper_binary()
    if not bin_path:
        logger.error("piper binary not found")
        return None
    model = os.path.expanduser(piper_model) if piper_model else ""
    if not model or not os.path.isfile(model):
        logger.error("piper model not found: %r", piper_model)
        return None

    if output_path:
        path = output_path
    else:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
    sandbox_cleanup: str | None = None
    try:
        cmd: list[str] = [bin_path, "-m", model, "-f", path]
        if length_scale != 1.0:
            cmd += ["--length-scale", str(length_scale)]
        cmd, sandbox_cleanup = sandbox_wrap_argv(cmd, mode="standard")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(text.encode("utf-8")), timeout=60,
            )
        except asyncio.TimeoutError:
            logger.error("piper timed out after 60s; killing subprocess")
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                logger.debug("piper kill/wait failed", exc_info=True)
            _safe_unlink(path)
            return None
        if proc.returncode != 0:
            logger.error("piper failed (rc=%s): %s", proc.returncode, stderr.decode(errors="replace")[:500])
            _safe_unlink(path)
            return None
        if os.path.getsize(path) < 100:
            logger.error("piper output too small")
            _safe_unlink(path)
            return None
        return path
    except Exception:
        logger.exception("piper synthesis error")
        _safe_unlink(path)
        return None
    finally:
        if sandbox_cleanup:
            _safe_unlink(sandbox_cleanup)


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class PiperTtsProvider(LocalTtsProvider):
    @property
    def name(self) -> str:
        return "piper"

    @property
    def display_name(self) -> str:
        return "Piper TTS"

    async def is_available(self) -> bool:
        # A downloaded voice is useless without the piper runtime; report available
        # only when the binary resolves.
        return _resolve_piper_binary() is not None

    def cache_dir(self) -> str:
        """Where downloaded voices land — lets the core download UI track progress."""
        return str(_voices_dir())

    async def list_voices(self) -> list[TtsVoice]:
        return [
            TtsVoice(
                name=v["name"],
                language=v["name"].split("-")[0] if "-" in v["name"] else "",
                size_mb=v.get("size_mb", 75),
                description=v.get("description", ""),
                downloaded=_is_voice_downloaded(v["name"]),
            )
            for v in PIPER_VOICES
        ]

    # download_model / delete_model / list_models are provided by the TtsProvider base
    # (they bridge to the voice methods below), so TTS speaks the uniform local-model
    # contract without per-app aliasing.
    async def download_voice(self, voice_name: str) -> bool:
        if voice_name not in {v["name"] for v in PIPER_VOICES}:
            return False

        def _download():
            try:
                from huggingface_hub import hf_hub_download
                voice_dir = _voices_dir() / voice_name
                voice_dir.mkdir(parents=True, exist_ok=True)
                parts = voice_name.split("-")
                locale = parts[0]
                voice = parts[1] if len(parts) > 1 else ""
                quality = parts[2] if len(parts) > 2 else "medium"
                lang_short = locale.split("_")[0]
                repo_id = "rhasspy/piper-voices"
                subdir = f"{lang_short}/{locale}/{voice}/{quality}"
                try:
                    hf_hub_download(repo_id=repo_id, filename=f"{subdir}/{voice_name}.onnx",
                                    local_dir=str(voice_dir), local_dir_use_symlinks=False)
                    hf_hub_download(repo_id=repo_id, filename=f"{subdir}/{voice_name}.onnx.json",
                                    local_dir=str(voice_dir), local_dir_use_symlinks=False)
                    return True
                except Exception as e:
                    logger.warning("Failed to download piper voice %s: %s", voice_name, e)
                    return False
            except ImportError:
                logger.error("huggingface_hub not installed — cannot download piper voices")
                return False

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=300)
        except asyncio.TimeoutError:
            return False

    async def delete_voice(self, voice_name: str) -> bool:
        voice_dir = _voices_dir() / voice_name
        if voice_dir.is_dir():
            shutil.rmtree(voice_dir)
            return True
        return False

    async def synthesize(
        self,
        text: str,
        voice: str = "",
        output_path: str = "",
        *,
        speed: float = 1.0,
        **opts: Any,
    ) -> str | None:
        """Synthesize *text* to a WAV via the local piper binary. ``voice`` is the
        voice name (its ``.onnx`` is located on disk); ``speed`` → ``--length-scale``."""
        model_path = voice_model_path(voice) if voice else ""
        if not model_path:
            return None
        return await _synthesize_piper_chunk(
            text, piper_model=model_path, length_scale=speed, output_path=output_path,
        )

    async def can_synthesize(self, voice: str = "") -> bool:
        if _resolve_piper_binary() is None:
            return False
        return bool(voice_model_path(voice)) if voice else True


def create_provider(config: dict[str, Any] | None = None) -> PiperTtsProvider:
    return PiperTtsProvider()


def availability() -> tuple[bool, str]:
    """Whether piper voices can be downloaded here (needs huggingface_hub)."""
    try:
        import huggingface_hub  # noqa: F401
        return True, ""
    except ImportError:
        return False, "Piper voice downloads need the huggingface_hub package (server/container build)."
