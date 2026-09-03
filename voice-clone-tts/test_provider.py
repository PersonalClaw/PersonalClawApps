"""Unit tests for the voice-clone-tts app: the manifest + catalog contract, the
cloning-capability declaration MI-2a routes on, catalog-driven model/voice listing,
engine detection, and graceful degradation when the optional engine is absent.

All tests run WITHOUT the heavy engine installed (the contract phase); the one path
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
        # at app-install, so the contract tests run everywhere.
        mf = json.loads((_BUNDLE / "app.json").read_text())
        deps = (mf.get("dependencies") or {}).get("pythonDependencies") or []
        assert deps == [], f"no heavy pythonDependencies expected, got {deps}"

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
        # The MI-2a guard routes a clone-kind request here (instead of 409
        # cloning_unsupported) precisely because this flag is True.
        assert VoiceCloneTtsProvider.supports_cloning is True
        # Design mode is not yet validated (MI-2c) — do not over-claim.
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
            assert await create_provider().synthesize("hello") is None

    @pytest.mark.asyncio
    async def test_clone_request_missing_ref_clip_returns_none(self, tmp_path):
        # Engine present (mocked) but the reference clip is missing → fail fast, no raise.
        with patch("provider._detect_engine", return_value="omnivoice"):
            missing = str(tmp_path / "nope.wav")
            assert await create_provider().synthesize("hi", ref_audio=missing) is None

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
        reason="no cloning engine installed (OmniVoice/CosyVoice) — MI-2c wires real inference",
    )
    @pytest.mark.asyncio
    async def test_engine_detected_reports_available(self):
        assert await create_provider().is_available() is True
