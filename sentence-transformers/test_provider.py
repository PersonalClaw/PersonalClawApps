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


# ── downloads: into the home, and never with the Hugging Face libraries' own token lookup ──


def _fake_sentence_transformers(monkeypatch) -> list[dict]:
    """``sentence_transformers`` as far as a fetch reaches it: each model records what it was
    built with, and saving one writes a config where it was told to."""
    import sys
    import types
    from pathlib import Path

    built: list[dict] = []

    class SentenceTransformerModelCardData:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class SentenceTransformer:
        def __init__(self, name_or_path, **kwargs):
            built.append({"name_or_path": name_or_path, **kwargs})

        def save(self, path):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "config.json").write_text("{}")

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = SentenceTransformer
    module.SentenceTransformerModelCardData = SentenceTransformerModelCardData
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(prov, "_pin_torch_single_thread", lambda: None)
    monkeypatch.setattr(prov, "_loaded_model", None)
    monkeypatch.setattr(prov, "_loaded_model_name", None)
    return built


def test_a_download_goes_to_the_home_and_never_reads_the_cli_token(monkeypatch):
    """With no token PersonalClaw resolves, the fetch is told to use none (``False``). It used to
    pass no token at all, and then the libraries look for one themselves: their lookup opens
    ``huggingface-cli login``'s token file, outside the home, without asking the owner.

    The model card is the lookup no ``token=`` reaches — it asks the hub about the base model as
    the model loads and again as it is saved — so it is told to stay local."""
    from pathlib import Path

    from personalclaw.sdk.util import config_dir

    monkeypatch.setattr(prov, "resolve_token", lambda: "")
    built = _fake_sentence_transformers(monkeypatch)

    saved = prov.download_model("all-MiniLM-L6-v2")

    [fetch] = built
    assert fetch["name_or_path"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert fetch["token"] is False
    assert fetch["model_card_data"].kwargs == {"local_files_only": True}
    home = config_dir().resolve()
    assert Path(fetch["cache_folder"]).resolve().is_relative_to(home)
    assert saved.resolve().is_relative_to(home)
    assert prov.is_model_downloaded("all-MiniLM-L6-v2")


def test_a_first_use_fetch_uses_the_token_personalclaw_resolves(monkeypatch):
    monkeypatch.setattr(prov, "resolve_token", lambda: "hf_from_the_cascade")
    built = _fake_sentence_transformers(monkeypatch)

    prov.load_model("bge-small-en-v1.5")

    [fetch] = built
    assert fetch["name_or_path"] == "BAAI/bge-small-en-v1.5"
    assert fetch["token"] == "hf_from_the_cascade"
    assert fetch["model_card_data"].kwargs == {"local_files_only": True}


def test_a_saved_model_loads_from_disk_and_never_asks_the_hub(monkeypatch):
    monkeypatch.setattr(prov, "resolve_token", lambda: "")
    built = _fake_sentence_transformers(monkeypatch)
    saved = prov._models_dir() / "all-MiniLM-L6-v2"
    saved.mkdir(parents=True)
    (saved / "config.json").write_text("{}")

    prov.load_model("all-MiniLM-L6-v2")

    assert built == [{"name_or_path": str(saved), "local_files_only": True}]


def test_an_embedding_that_names_no_model_is_refused_and_loads_nothing(monkeypatch):
    """Like chat, an embedding call names its model (the Embedding binding). Both calls used to
    load all-MiniLM-L6-v2 when handed none."""
    loaded = []
    monkeypatch.setattr(prov, "load_model", lambda name: loaded.append(name))
    monkeypatch.setattr(prov, "make_native_embed_fn", lambda name: loaded.append(name))

    provider = prov.create_provider()
    assert _run(provider.embed("a heron")) is None
    assert _run(provider.embed_batch(["a heron", "a kestrel"])) == [None, None]
    assert loaded == []



def test_a_media_call_that_names_no_model_is_refused_and_sends_nothing():
    """An embedding names its model (the Embedding binding). Both calls that name none are
    refused with the SDK's sentence before a model loads."""
    from pathlib import Path

    from apps_testkit.model_wire import (
        media_adapters,
        media_refusal_expected,
        media_refusal_report,
    )

    adapters = media_adapters(Path(__file__).parent, prov.create_provider)
    report = asyncio.run(media_refusal_report(adapters))
    assert report == media_refusal_expected(adapters)
