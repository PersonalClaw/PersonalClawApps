"""Unit tests for the sentence-transformers (local embeddings) app.

The heavy sentence-transformers/torch import is lazy (inside download/load), so these
tests exercise the catalog + lifecycle wiring without it. The app registers the
``native`` EmbeddingProvider that core's embedding registry resolves the ``embedding``
use-case to.
"""

from __future__ import annotations

import asyncio

import provider as prov

from personalclaw.sdk.embedding import EmbeddingModel, EmbeddingProvider


def _run(coro):
    return asyncio.run(coro)


def test_create_provider_is_embedding_provider():
    p = prov.create_provider({})
    assert isinstance(p, EmbeddingProvider)
    assert p.name == "native"


def test_lists_catalog_models():
    models = _run(prov.create_provider({}).list_models())
    names = {m.id if hasattr(m, "id") else m.name for m in models}
    assert "all-MiniLM-L6-v2" in names
    for m in models:
        assert isinstance(m, EmbeddingModel)
        assert m.dimension in (384, 768)


def test_cache_dir_exposed_for_download_progress():
    # Core's download UI reads cache_dir() to track byte progress without knowing
    # this app's layout.
    p = prov.create_provider({})
    assert p.cache_dir().endswith("models")


def test_availability_false_without_sentence_transformers(monkeypatch):
    # Simulate the package missing (desktop bundle): availability + is_available
    # degrade to False rather than raising.
    import builtins
    real_import = builtins.__import__

    def _no_st(name, *a, **k):
        if name == "sentence_transformers":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_st)
    ok, reason = prov.availability()
    assert ok is False and "sentence-transformers" in reason
    assert _run(prov.create_provider({}).is_available()) is False


def test_download_unknown_model_returns_false():
    # download_model returns False (never raises) for an unknown model → the job
    # records a clean failure.
    assert _run(prov.create_provider({}).download_model("no-such-model")) is False


def test_delete_absent_model_returns_false(tmp_path, monkeypatch):
    # MUST isolate the cache dir — delete_model now removes real on-disk model dirs
    # (both layouts), so an un-monkeypatched call here would wipe the user's actual
    # bound embedding model. Point it at an empty tmp dir: nothing to delete → False.
    monkeypatch.setattr(prov, "_models_dir", lambda: tmp_path)
    assert _run(prov.create_provider({}).delete_model("all-MiniLM-L6-v2")) is False


def test_repo_of_resolves_real_hf_repos():
    # The bare catalog key is a DISPLAY name; the true HF repo is explicit. bge lives
    # under BAAI (not sentence-transformers) — guessing the org 401'd the download.
    assert prov._repo_of("bge-small-en-v1.5") == "BAAI/bge-small-en-v1.5"
    assert prov._repo_of("all-MiniLM-L6-v2") == "sentence-transformers/all-MiniLM-L6-v2"
    # every catalog entry declares a repo, and none is a bare (org-less) id
    for name, info in prov.AVAILABLE_MODELS.items():
        assert "/" in info["repo"], f"{name} repo must be org-qualified: {info.get('repo')!r}"
    # an org-qualified id passed through verbatim; a bare unknown falls back to ST org
    assert prov._repo_of("acme/custom") == "acme/custom"
    assert prov._repo_of("mystery") == "sentence-transformers/mystery"


def test_hf_cache_dir_uses_real_repo(tmp_path, monkeypatch):
    # Detection must look under the REAL repo's cache dir (bge → BAAI), not a guessed one.
    monkeypatch.setattr(prov, "_models_dir", lambda: tmp_path)
    assert prov._hf_cache_dir("bge-small-en-v1.5").name == "models--BAAI--bge-small-en-v1.5"


def test_detection_accepts_hf_cache_layout(tmp_path, monkeypatch):
    # A model fetched on first-use via SentenceTransformer(cache_folder=...) lands in
    # HuggingFace's ``models--sentence-transformers--<name>`` layout, NOT the explicit
    # model.save() ``<name>`` dir. Detection must accept it — else a working, live model
    # reads as "not downloaded" (the bug this covers).
    monkeypatch.setattr(prov, "_models_dir", lambda: tmp_path)
    assert prov.is_model_downloaded("all-MiniLM-L6-v2") is False
    hf = tmp_path / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "abc"
    hf.mkdir(parents=True)
    (hf / "model.safetensors").write_bytes(b"weights")
    assert prov.is_model_downloaded("all-MiniLM-L6-v2") is True


def test_detection_accepts_saved_layout(tmp_path, monkeypatch):
    # The explicit model.save() layout is also honored.
    monkeypatch.setattr(prov, "_models_dir", lambda: tmp_path)
    saved = tmp_path / "all-MiniLM-L6-v2"
    saved.mkdir()
    (saved / "config.json").write_text("{}")
    assert prov.is_model_downloaded("all-MiniLM-L6-v2") is True


def test_delete_removes_both_layouts(tmp_path, monkeypatch):
    # Delete must clear BOTH on-disk layouts, else is_model_downloaded stays True and
    # the "deleted" model re-appears as downloaded.
    monkeypatch.setattr(prov, "_models_dir", lambda: tmp_path)
    saved = tmp_path / "all-MiniLM-L6-v2"; saved.mkdir(); (saved / "config.json").write_text("{}")
    hf = tmp_path / "models--sentence-transformers--all-MiniLM-L6-v2"; hf.mkdir()
    (hf / "model.bin").write_bytes(b"w")
    assert prov.is_model_downloaded("all-MiniLM-L6-v2") is True
    assert _run(prov.create_provider({}).delete_model("all-MiniLM-L6-v2")) is True
    assert prov.is_model_downloaded("all-MiniLM-L6-v2") is False
