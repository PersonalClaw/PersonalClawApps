"""Sentence Transformers — local, in-process embedding model provider (app).

Runs embedding models locally via the ``sentence-transformers`` package (pulls
``torch``). This app owns that heavy dependency + the local-model catalog +
download/load/delete lifecycle, so core stays torch-free and boots without an
embedder; the user installs this app (or seeds it) to get local embeddings.

Implements the ``EmbeddingProvider`` ABC from ``personalclaw.sdk.embedding``. The
core embedding registry resolves the ``embedding`` use-case to whatever provider is
bound in Settings → Models; this is the in-process one (``name="native"``, also
matched by the ``sentence-transformers``/``sentence_transformers`` aliases). Because
it also implements ``list_models``/``download_model``/``delete_model``, it is the
catalog/management surface for local embedding models (the Settings download UI drives
these through the provider, mirroring how ollama manages its models).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from pathlib import Path
from typing import Any, Callable

from personalclaw.sdk.embedding import EmbeddingModel, EmbeddingProvider
from personalclaw.sdk.local_model import LocalModelProvider

logger = logging.getLogger(__name__)

# Run torch/tokenizers SINGLE-PROCESS + single-threaded. In the long-lived gateway
# (which also holds faiss + av/ffmpeg native libs + async ACP subprocess trackers),
# a multi-worker encode spawns ``loky`` subprocesses whose native-lib teardown
# segfaults the whole process on some platforms (observed: a 768-dim mpnet re-index
# of the full store crashed the gateway, leaving the vector store unsearchable). These
# env flags MUST be set before torch/transformers import, so set them at module load.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")


def _pin_torch_single_thread() -> None:
    """Best-effort: cap torch to one intra-op thread so encode() never forks workers."""
    try:
        import torch
        torch.set_num_threads(1)
    except Exception:  # noqa: BLE001 — torch may be absent / already configured
        pass

# The local model catalog (dim + approx download size). This is the app's own
# knowledge of the sentence-transformers models it can run — it moved out of core.
# Each entry carries the true HuggingFace `repo` id. Most sentence-transformers models
# live under the `sentence-transformers/` org, but some (e.g. BGE → `BAAI`) do NOT — so
# the repo is explicit here rather than guessed by prepending an org (guessing 401'd bge).
AVAILABLE_MODELS: dict[str, dict] = {
    "all-MiniLM-L6-v2": {"repo": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384, "size_mb": 80, "description": "Fast, general-purpose (384 dim, ~80 MB)"},
    "all-MiniLM-L12-v2": {"repo": "sentence-transformers/all-MiniLM-L12-v2", "dim": 384, "size_mb": 120, "description": "Balanced quality/speed (384 dim, ~120 MB)"},
    "bge-small-en-v1.5": {"repo": "BAAI/bge-small-en-v1.5", "dim": 384, "size_mb": 130, "description": "High quality for retrieval (384 dim, ~130 MB)"},
    "all-mpnet-base-v2": {"repo": "sentence-transformers/all-mpnet-base-v2", "dim": 768, "size_mb": 420, "description": "Best quality, slower (768 dim, ~420 MB)"},
}


def _repo_of(model_name: str) -> str:
    """The true HuggingFace repo id for a catalog model (or an org-qualified id passed
    through verbatim). This is what SentenceTransformer(...) must be given — the bare
    catalog key is a display name and does NOT always map to sentence-transformers/<key>."""
    info = AVAILABLE_MODELS.get(model_name)
    if info and info.get("repo"):
        return info["repo"]
    return model_name if "/" in model_name else f"sentence-transformers/{model_name}"

_DEFAULT_NATIVE_MODEL = "all-MiniLM-L6-v2"

_loaded_model = None
_loaded_model_name: str | None = None


# ── Local model cache + lifecycle (the substrate that carried torch; now app-local) ──

def _models_dir() -> Path:
    home = os.environ.get("PERSONALCLAW_HOME", str(Path.home() / ".personalclaw"))
    return Path(home) / "models"


def _hf_cache_dir(model_name: str) -> Path:
    """HuggingFace's on-disk cache layout for a repo id: ``models--{org}--{model}``.
    Uses the entry's true `repo` (e.g. bge → BAAI), so detection looks where the
    weights ACTUALLY land, not a guessed sentence-transformers/<name>."""
    return _models_dir() / ("models--" + _repo_of(model_name).replace("/", "--"))


def _has_weights(d: Path) -> bool:
    """True if a cache dir holds real model weights (not just an empty/partial dir)."""
    if not d.is_dir():
        return False
    return any(d.rglob("*.safetensors")) or any(d.rglob("*.bin")) or any(d.rglob("config.json"))


def is_model_downloaded(model_name: str) -> bool:
    # A model can land in EITHER layout: the explicit `model.save()` copy
    # (``<cache>/<name-with-underscores>``) OR HuggingFace's own cache
    # (``<cache>/models--sentence-transformers--<name>``) when it was fetched on
    # first use via SentenceTransformer(cache_folder=...) rather than the download
    # button. Detection must accept both, else a usage-fetched model reads as
    # "not downloaded" even while it embeds live.
    saved = _models_dir() / model_name.replace("/", "_")
    return _has_weights(saved) or _has_weights(_hf_cache_dir(model_name))


def download_model(model_name: str) -> Path:
    """Download a sentence-transformers model to the local cache. Returns its path."""
    if model_name not in AVAILABLE_MODELS:
        raise ValueError(f"Unknown model '{model_name}'. Available: {list(AVAILABLE_MODELS.keys())}")
    from sentence_transformers import SentenceTransformer

    cache_dir = _models_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_path = cache_dir / model_name.replace("/", "_")
    logger.info("Downloading embedding model '%s' (repo %s) to %s", model_name, _repo_of(model_name), model_path)
    model = SentenceTransformer(_repo_of(model_name), cache_folder=str(cache_dir))
    model.save(str(model_path))
    logger.info("Model '%s' downloaded successfully", model_name)
    return model_path


def load_model(model_name: str) -> object:
    """Load a sentence-transformers model (downloads if not cached), process-cached."""
    global _loaded_model, _loaded_model_name
    if _loaded_model is not None and _loaded_model_name == model_name:
        return _loaded_model
    _pin_torch_single_thread()
    from sentence_transformers import SentenceTransformer

    model_path = _models_dir() / model_name.replace("/", "_")
    if model_path.exists() and any(model_path.iterdir()):
        _loaded_model = SentenceTransformer(str(model_path))
    else:
        cache_dir = _models_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        _loaded_model = SentenceTransformer(_repo_of(model_name), cache_folder=str(cache_dir))
        _loaded_model.save(str(model_path))
    _loaded_model_name = model_name
    return _loaded_model


def make_native_embed_fn(model_name: str) -> Callable[[str], list[float] | None]:
    """Return a sync ``(str) -> list[float] | None`` callable using a local model."""

    @functools.lru_cache(maxsize=4096)
    def _cached_embed(text: str) -> tuple[float, ...]:
        model = load_model(model_name)
        embedding = model.encode(text, normalize_embeddings=True)
        return tuple(embedding.tolist())

    def _embed(text: str) -> list[float] | None:
        try:
            return list(_cached_embed(text))
        except Exception:
            logger.debug("Native embed failed", exc_info=True)
            return None

    return _embed


def availability() -> tuple[bool, str]:
    """Whether local embedding models can run here, + a UI reason if not. torch is a
    heavy optional dep (omitted from the desktop PyInstaller bundle)."""
    try:
        import sentence_transformers  # noqa: F401
        return True, ""
    except ImportError:
        return False, ("Local embedding models need the sentence-transformers package "
                       "(server/container build) — or bind a remote embedding provider.")


# ── The embedding provider (registered into core's embedding registry by the loader) ──
# Implements BOTH axes: EmbeddingProvider (inference: embed) + LocalModelProvider
# (management: list/download/delete of local weights). A remote embedder would implement
# only EmbeddingProvider.

class NativeEmbeddingProvider(EmbeddingProvider, LocalModelProvider):
    @property
    def name(self) -> str:
        return "native"

    @property
    def display_name(self) -> str:
        return "Sentence Transformers (local)"

    async def is_available(self) -> bool:
        return availability()[0]

    def cache_dir(self) -> str:
        """Where downloaded weights land — lets the core download UI track byte
        progress without knowing this app's cache layout."""
        return str(_models_dir())

    async def list_models(self) -> list[EmbeddingModel]:
        result = []
        for name, info in AVAILABLE_MODELS.items():
            result.append(EmbeddingModel(
                name=name, dimension=info["dim"], size_mb=info["size_mb"],
                description=info["description"], downloaded=is_model_downloaded(name),
            ))
        return result

    async def download_model(self, model_name: str) -> bool:
        def _download():
            try:
                download_model(model_name)
                return True
            except Exception:
                logger.debug("download_model failed", exc_info=True)
                return False
        return await asyncio.to_thread(_download)

    async def delete_model(self, model_name: str) -> bool:
        import shutil
        # Remove BOTH on-disk layouts (the explicit model.save() copy AND HuggingFace's
        # own models--… cache) so a delete is honest — leaving either behind would keep
        # is_model_downloaded() True and the model would re-appear as "downloaded".
        removed = False
        for target in (_models_dir() / model_name.replace("/", "_"), _hf_cache_dir(model_name)):
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                removed = True
        if removed:
            global _loaded_model, _loaded_model_name
            if _loaded_model_name == model_name:
                _loaded_model = None
                _loaded_model_name = None
        return removed

    async def embed(self, text: str, model: str = "") -> list[float] | None:
        def _run():
            return make_native_embed_fn(model or _DEFAULT_NATIVE_MODEL)(text)
        return await asyncio.to_thread(_run)

    async def embed_batch(self, texts: list[str], model: str = "") -> list[list[float]]:
        def _run():
            m = load_model(model or _DEFAULT_NATIVE_MODEL)
            # Explicit single-process encode: no progress bar, modest batch — never let
            # sentence-transformers spawn loky workers (they segfault the gateway on
            # teardown, see module header). normalize_embeddings matches embed().
            return m.encode(
                texts, batch_size=16, show_progress_bar=False,
                normalize_embeddings=True, convert_to_numpy=True,
            ).tolist()
        return await asyncio.to_thread(_run)


def create_provider(config: dict[str, Any] | None = None) -> NativeEmbeddingProvider:
    """App factory — the loader registers the returned provider into core's embedding
    registry (via the ModelTypeHandler ``embedding``-capability seam)."""
    return NativeEmbeddingProvider()
