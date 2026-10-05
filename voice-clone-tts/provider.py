"""Voice Clone TTS provider (app) — cloning-capable synthesis beside piper.

A heavier, higher-fidelity local TTS engine that does **zero-shot voice cloning**:
condition synthesis on a short reference clip (the ``ref_audio``/``ref_text`` a
clone-kind voice profile resolves to) instead of a fixed voice bank. It runs as a
**sidecar** (``provider.execution: "sidecar"`` in ``app.json``) because the engine is
torch-heavy diffusion — exactly the crash class sidecars isolate, so a mid-synthesis
crash leaves the gateway up with a typed reason (the SDK's sidecar machinery,
consumed as-is).

Implements the ``LocalTtsProvider`` ABC from ``personalclaw.sdk.tts`` (never core
internals — the app boundary): its downloadable engine weights surface through the
uniform ``list_models``/``download_model``/``delete_model`` contract, and it declares
``supports_cloning = True`` so the ``tts.registry.guard_synthesis_capability``
routes a clone-kind request HERE instead of refusing it with ``409
cloning_unsupported:<provider>``. The engine's model cards (``runtime: torch``,
``matrix.supports_cloning``) are declared in the bundled ``catalog.json``, the single
source of truth for what this app offers.

SCOPE: the heavy ML engine is declared in ``app.json``
``sidecarDependencies``, which Install engine puts in this app's own Python environment where
the sidecar runs, never in ``pythonDependencies`` (those go into the gateway's own packages).
Nothing installs it before the owner asks, and no model weights are vendored, so the
manifest/contract tests run everywhere. When no engine is installed the provider degrades
gracefully (``is_available`` → False, ``synthesize`` → None) rather than raising, and
``availability()`` sends the owner to Install engine. The spike CHOSE OmniVoice (bake-off
0.906 vs 0.658); real zero-shot inference runs in the app's
``worker.py`` through the SDK sidecar runner, weights download resumably with a
completion receipt, and a sidecar killed mid-synthesis surfaces its typed crash
reason here while the gateway stays up.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from personalclaw.sdk.credentials import resolve_token
from personalclaw.sdk.model import ProviderResolutionError, require_model
from personalclaw.sdk.tts import LocalTtsProvider, TtsVoice
from personalclaw.sdk.util import config_dir

logger = logging.getLogger(__name__)

#: The engine the spike selected (OmniVoice 0.906 vs CosyVoice 0.658). Kept as a tuple so
#: detection stays a data-driven probe, but it is deliberately length-one now: the bake-off
#: is decided.
_CANDIDATE_ENGINE_MODULES: tuple[str, ...] = ("omnivoice",)


def _bundle_dir() -> Path:
    return Path(__file__).resolve().parent


def _catalog_path() -> Path:
    return _bundle_dir() / "catalog.json"


def _weights_dir() -> Path:
    """Where the cloning engine's downloaded weights live: the PersonalClaw home
    (``config_dir()``), so an isolated home is actually isolated."""
    d = config_dir() / "models" / "tts-clone"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hub_token() -> str | bool:
    """How a weights download authenticates: the token PersonalClaw resolves, or none at all.

    ``False`` rather than ``None``: with ``None`` huggingface_hub looks for a token itself — the
    environment, then ``huggingface-cli login``'s token file in the Hugging Face folder other
    tools share. That file is outside the home, and PersonalClaw reads it only when the owner
    allows that folder (Settings → Security → Outside PersonalClaw's home); the library's own
    lookup reads it without asking.
    """
    return resolve_token() or False


def _engine_env() -> dict[str, str]:
    """What the engine's own process is told about Hugging Face: use a folder in the home.

    The engine fetches a part it needs from the hub by itself when the weights folder lacks it
    (OmniVoice's audio tokenizer), and no ``token=`` reaches that fetch. Left alone it would read
    the token file in the Hugging Face folder other tools share and download into that folder,
    both outside the home. Pointed here, it finds no token file and writes only in the home.
    """
    return {"HF_HOME": str(_weights_dir() / ".huggingface")}


def _engine_venv() -> Path | None:
    """This app's own Python environment, ``<home>/apps/voice-clone-tts/venv``, when it exists.

    ``SidecarRunner`` runs the worker under that environment's interpreter whenever it has
    one, and under the gateway's only when it does not, so it is where the engine has to be.
    None on a core too old to vend ``sidecar_venv_dir``.
    """
    try:
        from personalclaw.sdk.sidecar import sidecar_venv_dir
    except ImportError:
        return None
    venv = sidecar_venv_dir("voice-clone-tts")
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return venv if python.is_file() else None


def _installed_in(venv: Path, module: str) -> bool:
    """Whether *module* is installed in *venv*, read off disk: its package directory, or the
    ``.dist-info`` an editable install leaves. Nothing is imported or run."""
    for site in [*venv.glob("lib/python*/site-packages"), venv / "Lib" / "site-packages"]:
        if (site / module).is_dir() or any(site.glob(f"{module}-*.dist-info")):
            return True
    return False


def _detect_engine() -> str:
    """Return the import name of the first installed candidate cloning engine, else "".

    Looked for where the sidecar will import it. With this app's own environment present,
    the worker runs there and nowhere else, so only that environment counts. It used to ask
    the gateway's interpreter in every case, so an engine installed where the sidecar runs
    left the app unavailable, and one installed into the gateway was reported ready for a
    worker that could not import it.

    Never imports the multi-GB engine to answer "is it here?": the environment is read off
    disk, and the gateway's interpreter through :func:`importlib.util.find_spec`. A
    namespace/partial-install edge that raises is treated as absent (fail-closed).
    """
    venv = _engine_venv()
    for mod in _CANDIDATE_ENGINE_MODULES:
        if venv is not None:
            if _installed_in(venv, mod):
                return mod
            continue
        try:
            if importlib.util.find_spec(mod) is not None:
                return mod
        except (ImportError, ValueError):
            continue
    return ""


def _worker_path() -> Path:
    """The sidecar worker module bundled beside this provider."""
    return _bundle_dir() / "worker.py"


#: Completion receipt written beside a voice's weights AFTER a full fetch — its absence
#: over a non-empty tree is exactly "interrupted, resumable".
_RECEIPT_NAME = ".download-complete.json"


def _make_runner() -> Any:
    """Build + register this app's :class:`SidecarRunner` through the SDK boundary.

    Returns None (with a log line) on a core too old to vend ``personalclaw.sdk.sidecar``
    — the provider then degrades exactly like the engineless case instead of crashing,
    which keeps the app installable against older gateways.
    """
    try:
        from personalclaw.sdk.sidecar import SidecarRunner, register_runner
    except ImportError:
        logger.warning(
            "voice-clone-tts: this core has no personalclaw.sdk.sidecar — "
            "upgrade PersonalClaw to run cloning synthesis"
        )
        return None
    runner = SidecarRunner(app="voice-clone-tts", worker=_worker_path(), env_extra=_engine_env())
    register_runner(runner)
    return runner


class VoiceCloneTtsProvider(LocalTtsProvider):
    """Cloning-capable local TTS provider. Declares ``supports_cloning`` so a clone-kind
    profile (one carrying a reference clip) resolves here instead of a 409 refusal."""

    #: Capability surface: this backend opts into voice CLONING. ``supports_voice_design``
    #: stays False until the spike validates the engine's instruct/design mode — the
    #: catalog cards mirror that (cloning true, design false) so nothing over-claims.
    supports_cloning = True
    supports_voice_design = False

    #: Compute device for the diffusion engine (settingsSchema ``device``), threaded to
    #: the worker's ``load``. Set by :func:`create_provider` from the app config.
    _device: str = "cpu"
    #: The live sidecar runner (lazy — built on first synthesis, registered with core).
    _runner: Any = None
    #: The typed reason of the most recent sidecar death (``sidecar_crashed:<why>``),
    #: empty after a clean synthesis. The health/residency surfaces read the runner's
    #: own record; this mirrors it at the provider for callers that only see the app.
    last_crash_reason: str = ""

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
    # is exactly what the catalog.json cards (runtime torch, matrix flags) need,
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
        """Fetch an engine's weights from its declared HuggingFace ``source`` repo,
        RESUMABLY: an interrupted fetch leaves its partial files in place and the
        next call continues from them.

        Two mechanisms compose: ``huggingface_hub.snapshot_download`` already resumes
        partial per-file transfers against the same ``local_dir``, and a completion
        RECEIPT (``.download-complete.json``) is written only after a fetch returns —
        so ``downloaded_voice`` distinguishes "every byte present" from "died halfway",
        and a partial tree is never deleted on failure (deleting it is what would make
        the interruption unsurvivable). Returns False — never raises — when the hub
        library or the card's ``source`` is absent.
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
        target = _weights_dir() / voice_name
        try:
            snapshot_download(repo_id=source, local_dir=str(target), token=_hub_token())
        except Exception:  # noqa: BLE001 — partial files stay for the resume; never crashes the app
            logger.exception(
                "voice-clone-tts: download interrupted for %r — partial files kept, "
                "re-run download to resume",
                voice_name,
            )
            return False
        receipt = {"source": source, "voice": voice_name, "complete": True}
        (target / _RECEIPT_NAME).write_text(json.dumps(receipt), encoding="utf-8")
        return True

    def downloaded_voice(self, voice_name: str) -> bool:
        """Whether *voice_name*'s weights finished downloading (receipt present) —
        a partial tree from an interrupted fetch answers False."""
        return (_weights_dir() / voice_name / _RECEIPT_NAME).is_file()

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

        Accepts the full conditioning surface (``ref_audio``/``ref_text``/``seed``/
        ``instruct``/``design_params``) that ``tts.registry.route_synthesis`` threads in.
        Degrades gracefully to ``None`` (never raises) when the optional engine or its
        weights are absent, and validates the reference clip up front so a clone request
        with a missing clip fails fast instead of mis-synthesizing.

        Engine-backed zero-shot inference: the bundled ``worker.py`` runs the
        real OmniVoice pipeline inside this app's sidecar child. A child killed
        mid-synthesis raises core's typed ``SidecarCrashed``; it is caught HERE — the
        gateway stays up, the typed reason (``sidecar_crashed:signal_9``) is recorded on
        :attr:`last_crash_reason` and logged, and the call degrades to ``None``.
        """
        # Like chat, a call names its voice (the text-to-speech binding's model), and one that
        # names none is refused before the engine loads: it used to load the weights folder's
        # root in its place.
        try:
            voice = require_model(voice)
        except ProviderResolutionError as exc:
            logger.warning("voice-clone-tts refused: %s", exc)
            return None
        engine = _detect_engine()
        if not engine:
            logger.info(
                "voice-clone-tts: no cloning engine installed (candidates: %s) — "
                "install one + its weights to enable synthesis",
                ", ".join(_CANDIDATE_ENGINE_MODULES),
            )
            return None
        if ref_audio and not os.path.isfile(os.path.expanduser(ref_audio)):
            logger.warning("voice-clone-tts: reference clip not found: %r", ref_audio)
            return None
        runner = self._runner if self._runner is not None else _make_runner()
        if runner is None:
            return None
        self._runner = runner

        if not output_path:
            fd, output_path = tempfile.mkstemp(suffix=".wav", prefix="pc-clone-")
            os.close(fd)
        try:
            from personalclaw.sdk.sidecar import SidecarCrashed, SidecarWorkerError
        except ImportError:  # pragma: no cover — _make_runner already gated this
            return None
        try:
            await runner.acall(
                "load",
                {"device": self._device, "weights_dir": str(_weights_dir() / voice)},
            )
            result = await runner.acall(
                "call",
                {
                    "method": "synthesize",
                    "payload": {
                        "text": text,
                        "output_path": output_path,
                        "ref_audio": os.path.expanduser(ref_audio) if ref_audio else "",
                        "ref_text": ref_text,
                        "seed": seed,
                        "speed": speed,
                        "instruct": instruct,
                    },
                },
            )
        except SidecarCrashed as exc:
            # The crash boundary working as designed: child died, gateway up, reason typed.
            self.last_crash_reason = exc.typed_reason
            logger.error(
                "voice-clone-tts: sidecar died mid-synthesis (%s) — gateway unaffected",
                exc.typed_reason,
            )
            return None
        except SidecarWorkerError as exc:
            # Child alive, call refused (engine API drift, bad weights, …) — no restart burned.
            logger.error("voice-clone-tts: synthesis failed in the worker: %s", exc)
            return None
        self.last_crash_reason = ""
        produced = str((result or {}).get("output_path") or "")
        return produced or None


def create_provider(config: dict[str, Any] | None = None) -> VoiceCloneTtsProvider:
    provider = VoiceCloneTtsProvider()
    provider._device = str((config or {}).get("device") or "cpu")
    return provider


def availability() -> tuple[bool, str]:
    """Whether cloning synthesis can run here — i.e. a candidate engine is installed.

    The engine is ``dependencies.sidecarDependencies``, which Install engine puts in this app's
    own Python environment, so the reason sends the owner there rather than to a shell.
    """
    if _detect_engine():
        return True, ""
    return False, (
        "The OmniVoice cloning engine is not installed. Install engine, on this app's card in "
        "Settings → Providers, puts it in the app's own Python environment (it brings torch, "
        "several GB). Then download OmniVoice in Settings → Models."
    )
