"""Unit tests for the piper-tts app: binary resolution, the sandboxed synthesis
subprocess, voice catalog + download-guard, and the TtsProvider surface.

Mirrors the piper coverage that lived in core test_voice_reply.py before the piper
synthesis moved into this app. Patches are app-local (provider.*)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import provider as prov
from provider import PiperTtsProvider, _resolve_piper_binary, _synthesize_piper_chunk


def _make_executable(path: str) -> None:
    with open(path, "wb") as f:
        f.write(b"#!/bin/sh\n")
    os.chmod(path, 0o755)


def _mock_subprocess(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


# ── _resolve_piper_binary ────────────────────────────────────────────────────

class TestResolvePiperBinary:
    def test_configured_path_preferred(self, tmp_path):
        bin_path = tmp_path / "my-piper"
        _make_executable(str(bin_path))
        assert _resolve_piper_binary(str(bin_path)) == str(bin_path)

    def test_configured_missing_returns_none(self, tmp_path):
        assert _resolve_piper_binary(str(tmp_path / "nope")) is None

    def test_falls_back_to_path(self):
        with patch("provider.shutil.which", return_value="/usr/local/bin/piper"), \
             patch("os.path.isfile", return_value=False):
            assert _resolve_piper_binary("") == "/usr/local/bin/piper"

    def test_nothing_found_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr("provider.sys.executable", str(tmp_path / "py" / "python"))
        with patch("provider.shutil.which", return_value=None):
            assert _resolve_piper_binary("") is None


# ── _synthesize_piper_chunk ────────────────────────────────────────────────────

class TestSynthesizePiper:
    @pytest.mark.asyncio
    async def test_binary_not_found_returns_none(self):
        with patch("provider._resolve_piper_binary", return_value=None):
            assert await _synthesize_piper_chunk("hi") is None

    @pytest.mark.asyncio
    async def test_model_missing_returns_none(self, tmp_path):
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        with patch("provider._resolve_piper_binary", return_value=str(bin_path)):
            assert await _synthesize_piper_chunk("hi", piper_model="") is None
            assert await _synthesize_piper_chunk("hi", piper_model=str(tmp_path / "missing.onnx")) is None

    @pytest.mark.asyncio
    async def test_success_returns_wav_path(self, tmp_path):
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[cmd.index("-f") + 1], "wb") as f:
                f.write(b"RIFF" + b"x" * 200)
            return proc

        with patch("provider._resolve_piper_binary", return_value=str(bin_path)), \
             patch("provider.sandbox_wrap_argv", side_effect=lambda c, mode: (c, None)), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_piper_chunk("hello", piper_model=str(model))
        assert result and result.endswith(".wav") and os.path.isfile(result)
        os.unlink(result)

    @pytest.mark.asyncio
    async def test_length_scale_in_cmd(self, tmp_path):
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        proc = _mock_subprocess(returncode=0)
        captured: list[str] = []

        def fake_wrap(cmd, mode):
            captured.extend(cmd)
            return cmd, None

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[cmd.index("-f") + 1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch("provider._resolve_piper_binary", return_value=str(bin_path)), \
             patch("provider.sandbox_wrap_argv", side_effect=fake_wrap), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_piper_chunk("hi", piper_model=str(model), length_scale=0.9)
        os.unlink(result)
        assert "--length-scale" in captured and "0.9" in captured

    @pytest.mark.asyncio
    async def test_nonzero_returncode_returns_none(self, tmp_path):
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        proc = _mock_subprocess(returncode=1, stderr=b"bad voice")
        with patch("provider._resolve_piper_binary", return_value=str(bin_path)), \
             patch("provider.sandbox_wrap_argv", side_effect=lambda c, mode: (c, None)), \
             patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await _synthesize_piper_chunk("hello", piper_model=str(model)) is None

    @pytest.mark.asyncio
    async def test_output_too_small_returns_none(self, tmp_path):
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[cmd.index("-f") + 1], "wb") as f:
                f.write(b"tiny")
            return proc

        with patch("provider._resolve_piper_binary", return_value=str(bin_path)), \
             patch("provider.sandbox_wrap_argv", side_effect=lambda c, mode: (c, None)), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            assert await _synthesize_piper_chunk("hello", piper_model=str(model)) is None

    @pytest.mark.asyncio
    async def test_sandbox_cleanup_unlinked(self, tmp_path):
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        cleanup = tmp_path / "sandbox-profile"
        cleanup.write_text("profile")
        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[cmd.index("-f") + 1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch("provider._resolve_piper_binary", return_value=str(bin_path)), \
             patch("provider.sandbox_wrap_argv", side_effect=lambda c, mode: (c, str(cleanup))), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_piper_chunk("hello", piper_model=str(model))
        os.unlink(result)
        assert not cleanup.exists(), "sandbox cleanup file should be removed"


# ── provider surface ───────────────────────────────────────────────────────────

class TestPiperProvider:
    def test_create_provider(self):
        p = prov.create_provider({})
        assert isinstance(p, PiperTtsProvider)
        assert p.name == "piper"

    @pytest.mark.asyncio
    async def test_list_voices_catalog(self):
        voices = await PiperTtsProvider().list_voices()
        names = {v.name for v in voices}
        assert "en_US-lessac-medium" in names

    @pytest.mark.asyncio
    async def test_download_unknown_voice_false(self):
        assert await PiperTtsProvider().download_voice("no-such-voice") is False

    @pytest.mark.asyncio
    async def test_synthesize_without_voice_returns_none(self):
        # No voice → no model path → None (graceful, never raises).
        assert await PiperTtsProvider().synthesize("hi", voice="") is None
