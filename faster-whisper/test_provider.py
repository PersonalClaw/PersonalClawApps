"""Unit tests for the faster-whisper (local STT) app.

The faster_whisper/CTranslate2 import is lazy (inside download/transcribe), so these
tests exercise the catalog + provider surface without it. The app registers the STT
provider that core's stt registry resolves the ``stt`` use-case to."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import provider as prov

from personalclaw.sdk.stt import SttModel, SttProvider


def _run(coro):
    return asyncio.run(coro)


def _seed_snapshot(root: Path, model_name: str) -> Path:
    """Write the minimum a real ``huggingface_hub`` snapshot leaves behind for *model_name*
    under *root*, so presence detection is exercised against a REAL tree rather than a mock."""
    d = prov._repo_dir(root, model_name) / "snapshots" / "00000000"
    d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"\x00" * 16)
    (d / "config.json").write_text("{}")
    return d


def test_create_provider_is_stt_provider():
    p = prov.create_provider({})
    assert isinstance(p, SttProvider)
    assert p.name == "faster_whisper"
    assert p.supports_streaming is True


def test_lists_catalog_models():
    models = _run(prov.create_provider({}).list_models())
    names = {m.name for m in models}
    assert "turbo" in names and "tiny" in names
    for m in models:
        assert isinstance(m, SttModel)
        # app doesn't mark active (core's Settings layer does)
        assert m.active is False


def test_cache_dir_exposed():
    assert prov.create_provider({}).cache_dir()  # non-empty path


# ── weights live in the PersonalClaw home; the shared Hugging Face folder is read only if allowed ──


def _home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "pclaw-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    # The machine-wide Hugging Face folder, as huggingface_hub finds it.
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    return home


def _shared_hub(tmp_path) -> Path:
    return tmp_path / "hf" / "hub"


def _allow_shared(home: Path) -> None:
    """What Settings → Security → Outside PersonalClaw's home writes when the owner turns on
    the Hugging Face folder other tools share."""
    (home / "config.json").write_text(
        json.dumps({"security": {"outside_home": ["huggingface-cache"]}}), encoding="utf-8"
    )


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) if root.exists() else []


def test_write_root_is_under_personalclaw_home(monkeypatch, tmp_path):
    """The WRITE target is in the PersonalClaw home, so an isolated home is isolated.
    Asserted on the RESOLVED path (not a mock call): setting PERSONALCLAW_HOME must move
    the tree, and no part of it may sit in the machine-wide Hugging Face folder."""
    home = _home(monkeypatch, tmp_path)
    root = prov._models_dir()
    assert home in root.parents, f"{root} is not under PERSONALCLAW_HOME {home}"
    assert (tmp_path / "hf") not in root.parents
    assert ".cache" not in root.parts
    assert Path(prov.create_provider({}).cache_dir()) == root


def test_write_root_defaults_to_dot_personalclaw_not_dot_cache(monkeypatch):
    """Unset, the root is the default home, ``~/.personalclaw``, NOT the host's ``~/.cache``,
    which is what this app used to fall back to and is why an isolated home leaked."""
    monkeypatch.delenv("PERSONALCLAW_HOME", raising=False)
    root = prov._models_dir()
    assert Path.home() / ".personalclaw" in root.parents
    assert Path.home() / ".cache" not in root.parents


def test_the_shared_folder_is_not_read_until_the_owner_allows_it(monkeypatch, tmp_path):
    """Weights another tool (or an earlier release) left in the machine-wide Hugging Face
    folder are outside the home, so they do not count until the owner turns that folder on."""
    _home(monkeypatch, tmp_path)
    _seed_snapshot(_shared_hub(tmp_path), "small")
    assert prov._shared_hub() is None
    assert prov._model_downloaded("small") is False
    models = _run(prov.create_provider({}).list_models())
    assert next(m for m in models if m.name == "small").downloaded is False


def test_allowed_shared_weights_report_downloaded_and_fetch_nothing(monkeypatch, tmp_path):
    """Allowed, weights already in the shared folder report as downloaded even though the
    home is EMPTY, so the UI never invites a multi-GB re-fetch. Presence checking must not
    construct a WhisperModel (that is what would download), so the stub explodes if touched."""
    home = _home(monkeypatch, tmp_path)
    _allow_shared(home)
    _seed_snapshot(_shared_hub(tmp_path), "small")
    before = _files(tmp_path / "hf")

    import faster_whisper

    def _boom(*a, **k):
        raise AssertionError("presence check constructed a WhisperModel — that downloads")

    monkeypatch.setattr(faster_whisper, "WhisperModel", _boom, raising=False)

    assert prov._model_downloaded("small") is True
    models = _run(prov.create_provider({}).list_models())
    small = next(m for m in models if m.name == "small")
    assert small.downloaded is True
    assert "Hugging Face folder other tools share" in small.description
    assert not prov._models_dir().exists()  # nothing written to the home
    assert _files(tmp_path / "hf") == before  # nor to the shared folder


def test_the_loader_reads_an_allowed_shared_snapshot_in_place(monkeypatch, tmp_path):
    """With weights only in the allowed shared folder, the LOADER is handed that snapshot's
    own folder, which faster-whisper loads without asking huggingface_hub anything: no
    download root, nothing fetched, nothing written there or copied to the home."""
    home = _home(monkeypatch, tmp_path)
    _allow_shared(home)
    snapshot = _seed_snapshot(_shared_hub(tmp_path), "small")
    before = _files(tmp_path / "hf")
    captured = {}

    class _StubWord:
        start = 0.0; end = 0.5; word = "hi"; probability = 0.9

    class _StubSeg:
        start = 0.0; end = 0.5; text = "hi"; words = [_StubWord()]

    class _StubModel:
        def __init__(self, target, *a, **k):
            captured["target"] = target
            captured["kwargs"] = k

        def transcribe(self, path, **kwargs):
            class _Info: language = "en"; duration = 0.5
            return iter([_StubSeg()]), _Info()

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)

    r = _run(prov.create_provider({}).transcribe_detailed("/tmp/x.wav", model="small"))
    assert r is not None and r.text
    assert captured["target"] == str(snapshot)
    assert "download_root" not in captured["kwargs"]
    assert not prov._repo_dir(prov._models_dir(), "small").exists()  # no copy
    assert _files(tmp_path / "hf") == before


def test_a_download_goes_to_the_home_and_never_reads_the_cli_token(monkeypatch, tmp_path):
    """cache_dir() is what core's download UI reads for byte progress, so it must be the
    directory that ACTUALLY fills. And with no token PersonalClaw resolves, the fetch is told
    to use none, so huggingface_hub does not open ``huggingface-cli login``'s token file,
    which is outside the home."""
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(prov, "resolve_token", lambda: "")
    captured = {}

    class _StubModel:
        def __init__(self, *a, **k):
            captured.update(k)

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)

    p = prov.create_provider({})
    reported = p.cache_dir()
    assert _run(p.download_model("small")) is True
    assert captured["download_root"] == reported
    assert captured["use_auth_token"] is False
    assert Path(reported).is_dir()  # the download really created the tree it reports


def test_a_download_uses_the_token_personalclaw_resolves(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(prov, "resolve_token", lambda: "hf_from_the_cascade")
    captured = {}

    class _StubModel:
        def __init__(self, *a, **k):
            captured.update(k)

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)
    assert _run(prov.create_provider({}).download_model("small")) is True
    assert captured["use_auth_token"] == "hf_from_the_cascade"


def test_cache_dir_is_the_home_even_when_the_shared_folder_holds_weights(monkeypatch, tmp_path):
    """Presence-reporting and progress-reporting must not disagree. Allowed shared weights
    make presence TRUE, but a deliberate download still fills the home, so cache_dir() keeps
    pointing there, and so does core's delete sweep, which works over cache_dir()."""
    home = _home(monkeypatch, tmp_path)
    _allow_shared(home)
    _seed_snapshot(_shared_hub(tmp_path), "small")
    assert prov._model_downloaded("small") is True

    captured = {}

    class _StubModel:
        def __init__(self, *a, **k):
            captured.update(k)

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)

    p = prov.create_provider({})
    assert _run(p.download_model("small")) is True
    assert captured["download_root"] == p.cache_dir() == str(prov._models_dir())
    assert Path(p.cache_dir()).is_relative_to(home)


def test_delete_removes_the_home_copy_and_never_the_shared_one(monkeypatch, tmp_path):
    """The public name ``turbo`` resolves to a non-templated upstream repository.

    Seed the literal Hugging Face cache path rather than asking the provider where it
    expects the files; otherwise the fixture would repeat the implementation bug. Deleting
    removes the home's copy; the allowed shared copy stays exactly as it was, so the model
    still reads as downloaded, from there."""
    home = _home(monkeypatch, tmp_path)
    _allow_shared(home)

    from faster_whisper import utils as faster_whisper_utils

    monkeypatch.setitem(
        faster_whisper_utils._MODELS,
        "turbo",
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    )
    cache_name = "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"

    shared_snapshot = _shared_hub(tmp_path) / cache_name / "snapshots" / "00000000"
    shared_snapshot.mkdir(parents=True)
    (shared_snapshot / "model.bin").write_bytes(b"\x00" * 16)
    before = _files(tmp_path / "hf")

    assert not prov._models_dir().exists()
    assert prov._model_downloaded("turbo") is True
    assert prov._load_target("turbo")[0] == str(shared_snapshot)

    home_snapshot = prov._models_dir() / cache_name / "snapshots" / "00000000"
    home_snapshot.mkdir(parents=True)
    (home_snapshot / "model.bin").write_bytes(b"\x00" * 16)

    assert _run(prov.create_provider({}).delete_model("turbo")) is True
    assert not (prov._models_dir() / cache_name).exists()
    assert _files(tmp_path / "hf") == before
    assert prov._model_downloaded("turbo") is True
    # Only the shared copy is left, and deleting again removes nothing there.
    assert _run(prov.create_provider({}).delete_model("turbo")) is False
    assert _files(tmp_path / "hf") == before


def test_repo_resolution_fallback_is_logged(monkeypatch, caplog, tmp_path):
    """A release without the private map degrades visibly to the old template."""
    from faster_whisper import utils as faster_whisper_utils

    monkeypatch.delattr(faster_whisper_utils, "_MODELS")
    with caplog.at_level("WARNING", logger=prov.__name__):
        repo_dir = prov._repo_dir(tmp_path, "small")

    assert repo_dir == tmp_path / "models--Systran--faster-whisper-small"
    assert "using fallback repository" in caplog.text


def test_partial_snapshot_is_not_weights(monkeypatch, tmp_path):
    """A bare ``models--…`` shell is what an interrupted download leaves behind. Counting it
    as present is how a shared-folder read silently resolves to an unusable tree."""
    home = _home(monkeypatch, tmp_path)
    _allow_shared(home)
    prov._repo_dir(_shared_hub(tmp_path), "small").mkdir(parents=True)
    assert prov._model_downloaded("small") is False


def test_download_unknown_model_false():
    assert _run(prov.create_provider({}).download_model("no-such-model")) is False


def test_bias_prompt_capped_and_single_lever(monkeypatch):
    """Regression: a large Lexicon (many bias terms) must NOT overflow Whisper's
    224-token prompt window. The bias string is capped ~200 chars AND passed through
    exactly ONE lever (hotwords OR initial_prompt, never both) — else the decoder raises
    'No position encodings ... >= 448' and the whole transcription silently returns None
    (empty transcript on audio ingestion). Captures the transcribe() kwargs via a stub."""
    captured = {}

    class _StubWord:
        start = 0.0; end = 0.5; word = "hi"; probability = 0.9

    class _StubSeg:
        start = 0.0; end = 0.5; text = "hi"; words = [_StubWord()]

    class _StubModel:
        def __init__(self, *a, **k): pass
        def transcribe(self, path, **kwargs):
            captured.update(kwargs)
            class _Info: language = "en"; duration = 0.5
            return iter([_StubSeg()]), _Info()

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)

    huge_bias = [f"Term Number {i} With Some Length" for i in range(80)]  # ~2000 chars raw
    r = _run(
        prov.create_provider({}).transcribe_detailed(
            "/tmp/x.wav", model="turbo", bias_terms=huge_bias
        )
    )
    assert r is not None and r.text  # did NOT silently fail
    # exactly one bias lever, and it's short (well under the 224-token window)
    levers = [k for k in ("hotwords", "initial_prompt") if k in captured]
    assert len(levers) == 1, f"expected ONE bias lever, got {levers}"
    assert len(captured[levers[0]]) <= 200


def test_a_call_that_names_no_model_is_refused_and_loads_nothing(monkeypatch):
    """Like chat, a transcription names its model (the speech-to-text binding). With none it
    is refused before anything is loaded: this used to load ``turbo`` in its place."""
    loaded = []

    class _StubModel:
        def __init__(self, target, *a, **k):
            loaded.append(target)

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _StubModel, raising=False)

    provider = prov.create_provider({})
    assert _run(provider.transcribe_detailed("/tmp/x.wav")) is None
    assert _run(provider.transcribe("/tmp/x.wav")) is None
    assert loaded == []


def test_availability_reason_without_faster_whisper(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _no_fw(name, *a, **k):
        if name == "faster_whisper":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_fw)
    ok, reason = prov.availability()
    assert ok is False and "stt" in reason.lower()


def test_a_media_call_that_names_no_model_is_refused_and_sends_nothing():
    """A transcription names its model (the speech-to-text binding). Both calls that name none
    are refused with the SDK's sentence, before a model loads or the audio is read."""
    from apps_testkit.model_wire import (
        media_adapters,
        media_refusal_expected,
        media_refusal_report,
    )

    adapters = media_adapters(Path(__file__).parent, prov.create_provider)
    report = asyncio.run(media_refusal_report(adapters))
    assert report == media_refusal_expected(adapters)
