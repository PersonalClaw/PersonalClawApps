"""Voice Clone TTS provider (app) — cloning-capable synthesis beside piper.

A heavier, higher-fidelity local TTS engine that does **zero-shot voice cloning**:
condition synthesis on a short reference clip (the ``ref_audio``/``ref_text`` a
clone-kind voice profile resolves to) instead of a fixed voice bank. It runs as a
**sidecar** (``provider.execution: "sidecar"`` in ``app.json``) because the engine is
torch-heavy diffusion — exactly the crash class sidecars isolate, so a mid-synthesis
crash leaves the gateway up with a typed reason (LOCAL-MODEL-MANAGER-V2 §3 machinery,
consumed as-is).

Implements the ``LocalTtsProvider`` ABC from ``personalclaw.sdk.tts`` (never core
internals — the app boundary): its downloadable engine weights surface through the
uniform ``list_models``/``download_model``/``delete_model`` contract, and it declares
``supports_cloning = True`` so MI-2a's ``tts.registry.guard_synthesis_capability``
routes a clone-kind request HERE instead of refusing it with ``409
cloning_unsupported:<provider>``. The engine's model cards (``runtime: torch``,
``matrix.supports_cloning``) are declared in the bundled ``catalog.json``, the single
source of truth for what this app offers.

SCOPE (MI-2b): the heavy ML engine is an OPTIONAL, lazily detected dependency — it is
NOT pinned in ``app.json`` ``pythonDependencies`` and no model weights are vendored, so
the manifest/contract tests run everywhere. When no engine is installed the provider
degrades gracefully (``is_available`` → False, ``synthesize`` → None) rather than
raising. Selecting ONE engine from the OmniVoice-vs-CosyVoice spike and wiring its
real zero-shot inference + weight download is MI-2c (see README + catalog cards).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from personalclaw.sdk.tts import LocalTtsProvider, TtsVoice

logger = logging.getLogger(__name__)

#: Candidate cloning engines from the plan's OmniVoice-vs-CosyVoice evaluation. Detection
#: probes each import name; the MI-2c spike pins the winner (and prunes the loser's
#: catalog card + this tuple). Kept as data so finalizing the choice is a one-line edit.
_CANDIDATE_ENGINE_MODULES: tuple[str, ...] = ("omnivoice", "cosyvoice")


def _bundle_dir() -> Path:
    return Path(__file__).resolve().parent


def _catalog_path() -> Path:
    return _bundle_dir() / "catalog.json"


def _weights_dir() -> Path:
    """Where the cloning engine's downloaded weights live (per-user, outside the bundle)."""
    home = os.environ.get("PERSONALCLAW_HOME", str(Path.home() / ".personalclaw"))
    d = Path(home) / "models" / "tts-clone"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _detect_engine() -> str:
    """Return the import name of the first installed candidate cloning engine, else "".

    Uses :func:`importlib.util.find_spec` so detection never imports (and never loads)
    the multi-GB engine just to answer "is it here?". A namespace/partial-install edge
    that raises is treated as absent (fail-closed), consistent with the capability
    surface's fail-closed footing.
    """
    for mod in _CANDIDATE_ENGINE_MODULES:
        try:
            if importlib.util.find_spec(mod) is not None:
                return mod
        except (ImportError, ValueError):
            continue
    return ""


class VoiceCloneTtsProvider(LocalTtsProvider):
    """Cloning-capable local TTS provider. Declares ``supports_cloning`` so a clone-kind
    profile (one carrying a reference clip) resolves here instead of a 409 refusal."""

    #: MI-2a capability surface: this backend opts into voice CLONING. ``supports_voice_design``
    #: stays False until the MI-2c spike validates the engine's instruct/design mode — the
    #: catalog cards mirror that (cloning true, design false) so nothing over-claims.
    supports_cloning = True
    supports_voice_design = False

    @property
    def name(self) -> str:
        return "voice-clone-tts"

    @property
    def display_name(self) -> str:
        return "Voice Clone TTS"

    async def is_available(self) -> bool:
        # Available only when a cloning engine runtime is actually installed; the app
        # ships without one (optional heavy dep), so this is False until the user adds it.
        return bool(_detect_engine())

    def cache_dir(self) -> str:
        """Where downloaded engine weights land — lets the core download UI track progress."""
        return str(_weights_dir())

    # ── catalog.json is the source of truth for what this app offers ──────────────
    #
    # Override list_models (rather than let LocalTtsProvider bridge it from list_voices)
    # so the per-model CapabilityMatrix + runtime survive into the cards — that carriage
    # is exactly what the atom's "catalog.json cards (runtime torch, matrix flags)" names,
    # and TtsVoice has no place to hold a matrix.

    async def list_models(self) -> list[Any]:
        # `_models_from_catalog` (LocalModelProvider mixin) parses the declarative cards,
        # fail-soft: a missing/malformed catalog yields [] rather than raising.
        return self._models_from_catalog(_catalog_path())

    async def list_voices(self) -> list[TtsVoice]:
        # The voice-picker view of the same cards; language is the card's first declared
        # language (matrix.languages), size/description carried through.
        voices: list[TtsVoice] = []
        for m in await self.list_models():
            langs = list(getattr(m.matrix, "languages", []) or []) if m.matrix else []
            voices.append(
                TtsVoice(
                    name=m.name,
                    language=langs[0] if langs else "",
                    size_mb=m.size_mb,
                    description=m.description,
                    downloaded=m.downloaded,
                )
            )
        return voices

    async def download_voice(self, voice_name: str) -> bool:
        """Fetch an engine's weights from its declared HuggingFace ``source`` repo.

        Guarded on ``huggingface_hub`` (an optional dep, not pinned in the manifest) and
        on the voice existing in ``catalog.json`` with a ``source``. Returns False —
        never raises — when either is absent, so the app is honest about what it can do
        without the engine stack. Real resumable download rides LMM-V2 in MI-2c.
        """
        source = ""
        for m in await self.list_models():
            if m.name == voice_name:
                source = m.source
                break
        if not source:
            logger.warning("voice-clone-tts: unknown voice or no source repo: %r", voice_name)
            return False
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            logger.error("voice-clone-tts: huggingface_hub not installed — cannot download %r", voice_name)
            return False
        try:
            snapshot_download(repo_id=source, local_dir=str(_weights_dir() / voice_name))
            return True
        except Exception:  # noqa: BLE001 — download failure degrades to False, never crashes the app
            logger.exception("voice-clone-tts: download failed for %r", voice_name)
            return False

    async def delete_voice(self, voice_name: str) -> bool:
        target = _weights_dir() / voice_name
        if target.is_dir():
            shutil.rmtree(target)
            return True
        return False

    async def can_synthesize(self, voice: str = "") -> bool:
        # Needs the engine runtime; weight-level readiness is verified in synthesize.
        return bool(_detect_engine())

    async def synthesize(
        self,
        text: str,
        voice: str = "",
        output_path: str = "",
        *,
        speed: float = 1.0,
        ref_audio: str = "",
        ref_text: str = "",
        seed: int = 0,
        instruct: str = "",
        design_params: dict | None = None,
        **opts: Any,
    ) -> str | None:
        """Synthesize *text* → audio, conditioned on a reference clip for cloning.

        Accepts the full MI-2a conditioning surface (``ref_audio``/``ref_text``/``seed``/
        ``instruct``/``design_params``) that ``tts.registry.route_synthesis`` threads in.
        Degrades gracefully to ``None`` (never raises) when the optional engine or its
        weights are absent, and validates the reference clip up front so a clone request
        with a missing clip fails fast instead of mis-synthesizing.

        Engine-backed zero-shot inference is wired in MI-2c once the spike pins the
        engine API; MI-2b ships the sidecar contract, capability declaration, catalog
        surface, and detection.
        """
        engine = _detect_engine()
        if not engine:
            logger.info(
                "voice-clone-tts: no cloning engine installed (candidates: %s) — "
                "install one + its weights (MI-2c) to enable synthesis",
                ", ".join(_CANDIDATE_ENGINE_MODULES),
            )
            return None
        if ref_audio and not os.path.isfile(os.path.expanduser(ref_audio)):
            logger.warning("voice-clone-tts: reference clip not found: %r", ref_audio)
            return None
        logger.warning(
            "voice-clone-tts: engine %r detected but real-inference wiring is MI-2c; "
            "returning None (voice=%r, cloning=%s, seed=%s)",
            engine, voice, bool(ref_audio), seed,
        )
        return None


def create_provider(config: dict[str, Any] | None = None) -> VoiceCloneTtsProvider:
    return VoiceCloneTtsProvider()


def availability() -> tuple[bool, str]:
    """Whether cloning synthesis can run here — i.e. a candidate engine is installed."""
    engine = _detect_engine()
    if engine:
        return True, ""
    return False, (
        "No cloning engine detected. Voice Clone TTS needs a torch-based zero-shot engine "
        "(OmniVoice or CosyVoice, selected by the MI-2c spike); install it and download a "
        "model card's weights to enable cloning."
    )
