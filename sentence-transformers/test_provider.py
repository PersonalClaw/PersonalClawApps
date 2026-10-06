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


def test_asking_whether_it_can_run_loads_no_library(monkeypatch):
    """Every Models page asks this, and asking used to import sentence-transformers, which imports
    torch: 171.8 s cold, measured, to answer one question."""
    import sys

    for name in ("sentence_transformers", "torch"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    prov.availability()
    _run(prov.create_provider({}).is_available())

    assert "sentence_transformers" not in sys.modules and "torch" not in sys.modules


def test_without_sentence_transformers_it_says_how_to_get_it(monkeypatch):
    """Found missing without importing anything: availability and is_available degrade to False
    rather than raising, and the reason names the reinstall that brings the package back."""
    import sys

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    assert prov.availability() == (
        False,
        "Local embedding models need sentence-transformers, which ships with this app, not with "
        "PersonalClaw itself. Reinstall Sentence Transformers (local embeddings) from the Store, "
        "or bind a remote embedding provider. The desktop app cannot install it: use the server "
        "or container build there.",
    )
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

    assert built == [{"name_or_path": str(saved), "device": "cpu", "local_files_only": True}]


def test_an_embedding_that_names_no_model_is_refused_and_loads_nothing(monkeypatch):
    """Like chat, an embedding call names its model (the Embedding binding). Both calls used to
    load all-MiniLM-L6-v2 when handed none."""
    loaded = []
    monkeypatch.setattr(prov, "load_model", lambda name: loaded.append(name))

    provider = prov.create_provider()
    assert _run(provider.embed("a heron")) is None
    assert _run(provider.embed_batch(["a heron", "a kestrel"])) == [None, None]
    assert loaded == []


# ── one model, built once, doing one thing at a time, on the CPU ──
#
# The gateway embeds from several threads at once: a binding's re-index check, the Settings page's
# own re-index start, a recall in a chat, a re-index batch. Each used to build a copy of the model
# of its own when none was loaded yet, and each ran it on the GPU the library picks on Apple silicon
# (mps), whose kernel cache torch shares across the whole process and does not guard: two first
# embeddings at once crashed the gateway.


def _fake_library(monkeypatch, *, hold: float = 0.05):
    """torch and sentence_transformers as the provider reaches them. Left to choose a device, the
    library takes the GPU, as the real one does on Apple silicon. Each model records what it was
    built from and on, and every model call (building, encoding, saving) records how many ran at
    once. ``state.gate``, when a test sets it, holds an encoding until the test lets it go."""
    import sys
    import threading
    import time
    import types
    from pathlib import Path
    from types import SimpleNamespace

    import numpy as np

    state = SimpleNamespace(built=[], at_once=0, most_at_once=0, gate=None)
    count = threading.Lock()

    def model_work(gate=None):
        with count:
            state.at_once += 1
            state.most_at_once = max(state.most_at_once, state.at_once)
        if gate is not None:
            gate.wait(10)
        time.sleep(hold)
        with count:
            state.at_once -= 1

    torch = types.ModuleType("torch")
    torch.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True))
    torch.cuda = SimpleNamespace(is_available=lambda: False)
    torch.set_num_threads = lambda n: None

    class SentenceTransformerModelCardData:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class SentenceTransformer:
        def __init__(self, name_or_path, device=None, **kwargs):
            model_work()
            self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
            state.built.append({"name_or_path": name_or_path, "device": self.device, **kwargs})

        def encode(self, sentences, **kwargs):
            model_work(state.gate)
            texts = [sentences] if isinstance(sentences, str) else list(sentences)
            vectors = np.array([[float(len(text)), 1.0] for text in texts])
            return vectors[0] if isinstance(sentences, str) else vectors

        def save(self, path):
            model_work()
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "config.json").write_text("{}")

    library = types.ModuleType("sentence_transformers")
    library.SentenceTransformer = SentenceTransformer
    library.SentenceTransformerModelCardData = SentenceTransformerModelCardData
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", library)
    monkeypatch.setattr(prov, "resolve_token", lambda: "")
    monkeypatch.setattr(prov, "_loaded_model", None)
    monkeypatch.setattr(prov, "_loaded_model_name", None)
    return state


def _at_once(*calls):
    """Run each call on a thread of its own, all let go together; their answers, in order."""
    import threading

    start = threading.Barrier(len(calls))
    answers: list = [None] * len(calls)
    raised: list = []

    def run(i, call):
        start.wait()
        try:
            answers[i] = call()
        except BaseException as exc:  # noqa: BLE001 — re-raised below, on the test's thread
            raised.append(exc)

    threads = [threading.Thread(target=run, args=(i, call)) for i, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "a call never finished"
    if raised:
        raise raised[0]
    return answers


def _on_disk(model_name: str):
    saved = prov._models_dir() / model_name
    saved.mkdir(parents=True)
    (saved / "config.json").write_text("{}")
    return saved


def test_first_uses_at_once_build_the_model_once_and_never_side_by_side(monkeypatch):
    """Bind a model that is not downloaded yet, as Change & re-index in Settings → Models does:
    the binding's re-index check and the page's own re-index start probe it at once, and a recall
    and a re-index batch can arrive beside them. The first to arrive fetches the model, the others
    wait for it, and the model does one thing at a time."""
    state = _fake_library(monkeypatch)
    provider = prov.create_provider()
    model = "all-MiniLM-L6-v2"
    probe = provider.get_embed_fn(model)

    answers = _at_once(
        lambda: probe("readiness probe"),
        lambda: probe("readiness probe"),
        lambda: _run(provider.embed("a heron", model=model)),
        lambda: _run(provider.embed_batch(["a heron", "a kestrel"], model=model)),
    )

    assert [b["name_or_path"] for b in state.built] == ["sentence-transformers/all-MiniLM-L6-v2"]
    assert state.most_at_once == 1
    assert answers == [[15.0, 1.0], [15.0, 1.0], [7.0, 1.0], [[7.0, 1.0], [9.0, 1.0]]]


def test_a_start_with_a_bound_model_loads_it_once_and_answers_every_caller(monkeypatch):
    """A gateway starting with a model bound and downloaded (the one bound when it last stopped,
    however it stopped) reaches it from the start's re-index check, a recall and a re-index batch
    at once. It loads the model from disk once, on the CPU, and answers each caller in turn."""
    state = _fake_library(monkeypatch)
    model = "all-MiniLM-L6-v2"
    saved = _on_disk(model)
    provider = prov.create_provider()
    probe = provider.get_embed_fn(model)

    answers = _at_once(
        lambda: probe("readiness probe"),
        lambda: _run(provider.embed("a heron", model=model)),
        lambda: _run(provider.embed_batch(["a heron", "a kestrel"], model=model)),
    )

    assert state.built == [{"name_or_path": str(saved), "device": "cpu", "local_files_only": True}]
    assert state.most_at_once == 1
    assert answers == [[15.0, 1.0], [7.0, 1.0], [[7.0, 1.0], [9.0, 1.0]]]


def test_every_model_is_built_on_the_cpu_though_the_library_would_take_the_gpu(monkeypatch):
    """Left to choose, sentence-transformers puts a model on the GPU (mps on Apple silicon), where
    torch's kernel cache is one for the whole process and unguarded, and the gateway's process is
    shared with every app. So every model this app builds, loaded from disk, downloaded or fetched
    on its first use, is built on the CPU."""
    state = _fake_library(monkeypatch, hold=0)
    _on_disk("all-MiniLM-L6-v2")

    prov.load_model("all-MiniLM-L6-v2")
    prov.download_model("bge-small-en-v1.5")
    assert _run(prov.create_provider().embed("a heron", model="all-mpnet-base-v2")) == [7.0, 1.0]

    assert [b["device"] for b in state.built] == ["cpu", "cpu", "cpu"]


def test_a_download_and_a_first_use_of_one_model_build_it_one_after_the_other(monkeypatch):
    """Download in Settings → Models while the model, just bound, is being probed: both build it,
    never at the same time, and the copy on disk is a whole one."""
    state = _fake_library(monkeypatch)
    model = "all-MiniLM-L6-v2"

    saved, vector = _at_once(
        lambda: prov.download_model(model),
        lambda: _run(prov.create_provider().embed("a heron", model=model)),
    )

    assert state.most_at_once == 1
    assert vector == [7.0, 1.0]
    assert (saved / "config.json").is_file()


def test_deleting_a_model_waits_for_the_call_that_is_using_it(monkeypatch):
    """Delete in Settings → Models while an embedding runs with that model: its files go once the
    call is done, never from under it, and the next embedding loads the model again."""
    import threading
    import time

    state = _fake_library(monkeypatch, hold=0)
    model = "all-MiniLM-L6-v2"
    saved = _on_disk(model)
    provider = prov.create_provider()
    state.gate = threading.Event()
    embedding = threading.Thread(target=lambda: _run(provider.embed("a heron", model=model)))
    embedding.start()
    deadline = time.monotonic() + 10
    while state.at_once == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    deleted: list = []
    deleting = threading.Thread(target=lambda: deleted.append(_run(provider.delete_model(model))))
    deleting.start()

    time.sleep(0.2)
    assert saved.is_dir(), "the model's files went while a call was using it"

    state.gate.set()
    embedding.join(10)
    deleting.join(10)
    assert deleted == [True] and not saved.exists()
    assert _run(provider.embed("a kestrel", model=model)) == [9.0, 1.0]
    assert [b["name_or_path"] for b in state.built] == [
        str(saved),
        "sentence-transformers/all-MiniLM-L6-v2",
    ]


def test_each_model_says_it_runs_on_the_cpu():
    """Where Settings → Models shows a local embedding model, it says what it runs on."""
    models = _run(prov.create_provider().list_models())

    assert models and all("runs on the CPU" in m.description for m in models)



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


def test_the_declared_floor_is_a_release_whose_model_card_can_stay_local():
    """The fetch hands the model a card told to stay local
    (``SentenceTransformerModelCardData(local_files_only=True)``), or it asks Hugging Face about
    the base model through a lookup no token setting reaches. 5.6 is the oldest release that was
    checked for it, so nothing older may satisfy the declaration."""
    import json
    from pathlib import Path

    from packaging.requirements import Requirement

    manifest = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))
    [declared] = [
        Requirement(d)
        for d in manifest["dependencies"]["pythonDependencies"]
        if d.startswith("sentence-transformers")
    ]
    assert declared.specifier.contains("5.6.0") and not declared.specifier.contains("5.5.9")


def test_the_installed_library_takes_the_local_model_card():
    import pytest

    library = pytest.importorskip("sentence_transformers")
    card = library.SentenceTransformerModelCardData(local_files_only=True)
    assert card.local_files_only is True
