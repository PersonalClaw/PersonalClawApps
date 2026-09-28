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
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from personalclaw.sdk.credentials import resolve_token
from personalclaw.sdk.model import ProviderResolutionError, require_model
from personalclaw.sdk.tts import LocalTtsProvider, TtsVoice
from personalclaw.sdk.util import app_packages_env, child_process_env, config_dir, sandbox_wrap_argv

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
    """Where voices are downloaded to: the PersonalClaw home (``config_dir()``), so an isolated
    home is actually isolated."""
    d = config_dir() / "models" / "tts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hub_token() -> str | bool:
    """How a voice download authenticates: the token PersonalClaw resolves, or none at all.

    ``False`` rather than ``None``: with ``None`` huggingface_hub looks for a token itself — the
    environment, then ``huggingface-cli login``'s token file in the Hugging Face folder other
    tools share. That file is outside the home, and PersonalClaw reads it only when the owner
    allows that folder (Settings → Security → Outside PersonalClaw's home); the library's own
    lookup reads it without asking.
    """
    return resolve_token() or False


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


def _declared_piper_command() -> list[str] | None:
    """Run the ``piper-tts`` package this app declares as ``python -m piper``, or ``None``.

    The gateway installs an app's declared packages into ``<home>/app-python`` and loads them
    only into its own process: a plain Python subprocess does not see them, and pip's ``piper``
    console script lands in that directory's ``bin/`` rather than beside the interpreter. So the
    child is the gateway's own interpreter running the module, with the app packages on its
    ``PYTHONPATH`` (core's ``app_packages_env``, which :func:`_synthesize_piper_chunk` passes for
    it). Those directories come before site-packages in the child, which is harmless here: the
    child runs nothing but piper.
    """
    spec = importlib.util.find_spec("piper")
    locations = list(spec.submodule_search_locations or []) if spec is not None else []
    if not locations or not (Path(locations[0]) / "__main__.py").is_file():
        return None
    return [sys.executable, "-m", "piper"]


def _piper_command(configured: str = "") -> tuple[list[str], bool] | None:
    """``(argv prefix, whether it runs the declared package)`` that runs piper, or ``None``.

    Resolution order: an explicit path → ``piper`` on PATH → the ``piper-tts`` package this app
    declares (see :func:`_declared_piper_command`) → ``~/piper-venv/bin/piper``.
    """
    if configured:
        p = os.path.expanduser(configured)
        return ([p], False) if os.path.isfile(p) and os.access(p, os.X_OK) else None
    found = shutil.which("piper")
    if found:
        return [found], False
    declared = _declared_piper_command()
    if declared is not None:
        return declared, True
    standalone = os.path.expanduser("~/piper-venv/bin/piper")
    if os.path.isfile(standalone) and os.access(standalone, os.X_OK):
        return [standalone], False
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
    command = _piper_command()
    if command is None:
        logger.error("piper not found: no piper on PATH and the piper-tts package is not importable")
        return None
    prefix, runs_declared_package = command
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
        cmd: list[str] = [*prefix, "-m", model, "-f", path]
        if length_scale != 1.0:
            cmd += ["--length-scale", str(length_scale)]
        cmd, sandbox_cleanup = sandbox_wrap_argv(cmd, mode="standard")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Piper is a program someone else wrote, reading a voice someone else trained, and this
            # provider runs inside the gateway, whose environment holds every secret saved in
            # PersonalClaw. So it gets the child allowlist. The declared package needs the app
            # packages on its PYTHONPATH as well; a piper of its own install must not have them put
            # in front of its own.
            env=app_packages_env() if runs_declared_package else child_process_env(),
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
        # only when piper resolves.
        return _piper_command() is not None

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
                token = _hub_token()
                try:
                    hf_hub_download(repo_id=repo_id, filename=f"{subdir}/{voice_name}.onnx",
                                    local_dir=str(voice_dir), local_dir_use_symlinks=False,
                                    token=token)
                    hf_hub_download(repo_id=repo_id, filename=f"{subdir}/{voice_name}.onnx.json",
                                    local_dir=str(voice_dir), local_dir_use_symlinks=False,
                                    token=token)
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
        voice name (its ``.onnx`` is located on disk); ``speed`` → ``--length-scale``.

        Like every media call, it names its voice (the text-to-speech binding's model), and one
        that names none is refused, saying so. It used to return nothing and say nothing."""
        try:
            voice = require_model(voice)
        except ProviderResolutionError as exc:
            logger.warning("piper-tts refused: %s", exc)
            return None
        model_path = voice_model_path(voice)
        if not model_path:
            return None
        return await _synthesize_piper_chunk(
            text, piper_model=model_path, length_scale=speed, output_path=output_path,
        )

    async def can_synthesize(self, voice: str = "") -> bool:
        """Whether a call naming *voice* would speak now: the piper runtime is here and the
        voice is on disk. A call that names no voice is refused, so it cannot."""
        if not voice or _piper_command() is None:
            return False
        return bool(voice_model_path(voice))


def create_provider(config: dict[str, Any] | None = None) -> PiperTtsProvider:
    return PiperTtsProvider()


def availability() -> tuple[bool, str]:
    """Whether piper voices can be downloaded here (needs huggingface_hub)."""
    try:
        import huggingface_hub  # noqa: F401
        return True, ""
    except ImportError:
        return False, "Piper voice downloads need the huggingface_hub package (server/container build)."
