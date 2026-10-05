"""Unit tests for the voice-clone-tts app: the manifest + catalog contract, the
cloning-capability declaration core routes on, catalog-driven model/voice listing,
engine detection, and graceful degradation when the optional engine is absent.

All tests run WITHOUT the heavy engine installed; the one path
that needs a real engine is guarded with ``skipif``. Patches are app-local (provider.*)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import provider as prov
from provider import VoiceCloneTtsProvider, availability, create_provider, _detect_engine

_BUNDLE = Path(__file__).resolve().parent


# ── manifest + catalog contract (pure, no engine) ──────────────────────────────

class TestManifestAndCatalog:
    def test_app_json_declares_sidecar_tts_provider(self):
        mf = json.loads((_BUNDLE / "app.json").read_text())
        assert mf["name"] == "voice-clone-tts"
        p = mf["provider"]
        assert p["type"] == "model"
        assert p["capabilities"] == ["tts"]
        assert p["execution"] == "sidecar", "the torch engine must run isolated"
        assert p["implementation"] == "provider:create_provider"

    def test_manifest_does_not_pin_heavy_engine(self):
        # Scope rule: the multi-GB engine is an OPTIONAL lazy dep — never pip-installed
        # at app-install (that installs into the gateway's own packages, and the engine
        # belongs in the sidecar's environment), so the contract tests run everywhere. The
        # one declared dependency is the weights download library, which runs in the gateway.
        mf = json.loads((_BUNDLE / "app.json").read_text())
        deps = (mf.get("dependencies") or {}).get("pythonDependencies") or []
        assert deps == ["huggingface-hub>=0.23"], f"only the download library, got {deps}"

    def test_catalog_cards_declare_cloning_and_torch(self):
        raw = json.loads((_BUNDLE / "catalog.json").read_text())
        cards = raw.get("models", raw) if isinstance(raw, dict) else raw
        assert cards, "catalog must list at least one engine model"
        for c in cards:
            assert "tts" in c["capabilities"]
            assert c["runtime"] == "torch", "engine cards declare the torch runtime"
            assert c["matrix"]["supports_cloning"] is True
            assert c.get("source"), "a card needs a source repo for weight download"


# ── provider contract (pure, no engine) ────────────────────────────────────────

class TestProviderContract:
    def test_create_provider(self):
        p = create_provider({})
        assert isinstance(p, VoiceCloneTtsProvider)
        assert p.name == "voice-clone-tts"
        assert p.display_name == "Voice Clone TTS"

    def test_declares_supports_cloning(self):
        # The guard routes a clone-kind request here (instead of 409
        # cloning_unsupported) precisely because this flag is True.
        assert VoiceCloneTtsProvider.supports_cloning is True
        # Design mode is not yet validated — do not over-claim.
        assert VoiceCloneTtsProvider.supports_voice_design is False

    @pytest.mark.asyncio
    async def test_list_models_carry_matrix_and_runtime(self):
        models = await create_provider().list_models()
        assert models, "catalog cards should surface as models"
        assert all(m.runtime == "torch" for m in models)
        assert any(m.matrix and m.matrix.supports_cloning for m in models)

    @pytest.mark.asyncio
    async def test_list_voices_derived_from_catalog(self):
        voices = await create_provider().list_voices()
        assert voices and all(v.name for v in voices)
        model_names = {m.name for m in await create_provider().list_models()}
        assert {v.name for v in voices} == model_names


# ── engine detection + graceful degradation (no engine installed) ──────────────

class TestDegradation:
    @pytest.mark.asyncio
    async def test_unavailable_without_engine(self):
        with patch("provider._detect_engine", return_value=""):
            p = create_provider()
            assert await p.is_available() is False
            assert await p.can_synthesize("omnivoice-zeroshot") is False
            ok, reason = availability()
            # availability() reflects the real host; assert its shape either way
            assert isinstance(ok, bool) and isinstance(reason, str)

    @pytest.mark.asyncio
    async def test_synthesize_without_engine_returns_none(self):
        with patch("provider._detect_engine", return_value=""):
            assert await create_provider().synthesize("hello", voice="omnivoice-zeroshot") is None

    @pytest.mark.asyncio
    async def test_clone_request_missing_ref_clip_returns_none(self, tmp_path):
        # Engine present (mocked) but the reference clip is missing → fail fast, no raise.
        with patch("provider._detect_engine", return_value="omnivoice"):
            missing = str(tmp_path / "nope.wav")
            assert (
                await create_provider().synthesize(
                    "hi", voice="omnivoice-zeroshot", ref_audio=missing
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_delete_voice_absent_returns_false(self):
        assert await create_provider().delete_voice("no-such-voice") is False

    @pytest.mark.asyncio
    async def test_download_unknown_voice_returns_false(self):
        assert await create_provider().download_voice("no-such-voice") is False


# ── real engine path (skipped unless an engine is actually installed) ──────────

class TestEnginePath:
    @pytest.mark.skipif(
        _detect_engine() == "",
        reason="no cloning engine installed (OmniVoice/CosyVoice)",
    )
    @pytest.mark.asyncio
    async def test_engine_detected_reports_available(self):
        assert await create_provider().is_available() is True


# ── weights, and what the engine fetches by itself, stay in the home ───────────────────────


def _fake_hub(monkeypatch) -> list[dict]:
    """``huggingface_hub`` as far as a weights download reaches it: ``snapshot_download``
    records what it was handed and makes the folder it was told to fill."""
    import sys
    import types

    calls: list[dict] = []

    def snapshot_download(*, repo_id, local_dir, **kwargs):
        calls.append({"repo_id": repo_id, "local_dir": local_dir, **kwargs})
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return local_dir

    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    return calls


@pytest.mark.asyncio
async def test_a_weights_download_goes_to_the_home_and_never_reads_the_cli_token(monkeypatch):
    """With no token PersonalClaw resolves, the fetch is told to use none (``False``). It used to
    pass no token at all, and then huggingface_hub looks for one itself: its lookup opens
    ``huggingface-cli login``'s token file, outside the home, without asking the owner."""
    from personalclaw.sdk.util import config_dir

    monkeypatch.setattr(prov, "resolve_token", lambda: "")
    calls = _fake_hub(monkeypatch)

    assert await create_provider().download_voice("omnivoice-zeroshot") is True

    [call] = calls
    assert call["repo_id"] == "k2-fsa/OmniVoice" and call["token"] is False
    assert Path(call["local_dir"]).resolve().is_relative_to(config_dir().resolve())


@pytest.mark.asyncio
async def test_a_weights_download_uses_the_token_personalclaw_resolves(monkeypatch):
    monkeypatch.setattr(prov, "resolve_token", lambda: "hf_from_the_cascade")
    calls = _fake_hub(monkeypatch)
    assert await create_provider().download_voice("omnivoice-zeroshot") is True
    assert [c["token"] for c in calls] == ["hf_from_the_cascade"]


def test_the_engine_process_uses_a_hugging_face_folder_in_the_home(monkeypatch):
    """The engine fetches a part it needs from the hub by itself (OmniVoice's audio tokenizer,
    when the weights folder lacks one), and no ``token=`` reaches that fetch. Its process is
    started with a Hugging Face folder in the home, so that fetch finds no token file outside
    the home and writes nothing there. It used to inherit the folder other tools share."""
    from personalclaw.sdk import sidecar as sdk_sidecar
    from personalclaw.sdk.util import config_dir

    made: list[dict] = []

    class _Runner:
        def __init__(self, **kwargs):
            made.append(kwargs)

    monkeypatch.setattr(sdk_sidecar, "SidecarRunner", _Runner)
    monkeypatch.setattr(sdk_sidecar, "register_runner", lambda runner: None)

    prov._make_runner()

    [kwargs] = made
    assert kwargs["app"] == "voice-clone-tts"
    hf_home = Path(kwargs["env_extra"]["HF_HOME"]).resolve()
    assert hf_home.is_relative_to(config_dir().resolve())


def test_the_engine_folder_is_not_refused_by_the_child_environment_floor(monkeypatch):
    """A child's environment is an allowlist with the starter's own values on top, and the floor
    refuses credential-shaped names among those. ``HF_HOME`` is not one, so the engine's process
    gets the folder set here, over whatever the gateway's own environment holds."""
    from personalclaw.sdk.util import child_process_env

    monkeypatch.setenv("HF_HOME", "/elsewhere/huggingface")
    env = child_process_env(extra=prov._engine_env())
    assert env["HF_HOME"] == prov._engine_env()["HF_HOME"] != "/elsewhere/huggingface"


def test_a_media_call_that_names_no_model_is_refused_and_sends_nothing():
    """A synthesis names its voice (the text-to-speech binding's model). One that names none is
    refused with the SDK's sentence before the engine loads, and can_synthesize says it cannot
    speak for it."""
    import asyncio

    from apps_testkit.model_wire import (
        media_adapters,
        media_refusal_expected,
        media_refusal_report,
    )

    adapters = media_adapters(Path(__file__).parent, prov.create_provider)
    report = asyncio.run(media_refusal_report(adapters))
    assert report == media_refusal_expected(adapters)
