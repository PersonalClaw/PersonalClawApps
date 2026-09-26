"""Unit tests for the piper-tts app: binary resolution, the sandboxed synthesis
subprocess, voice catalog + download-guard, and the TtsProvider surface.

Mirrors the piper coverage that lived in core test_voice_reply.py before the piper
synthesis moved into this app. Patches are app-local (provider.*)."""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import provider as prov
from provider import PiperTtsProvider, _piper_command, _synthesize_piper_chunk


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


# ── _piper_command ────────────────────────────────────────────────────────────

#: A stand-in for the ``piper-tts`` package: ``python -m piper -m <model> -f <out>`` writes a WAV.
_FAKE_PIPER_MAIN = """
import sys
out = sys.argv[sys.argv.index("-f") + 1]
sys.stdin.read()
with open(out, "wb") as f:
    f.write(b"RIFF" + b"x" * 400)
"""


@pytest.fixture
def packages_outside_the_default_path(tmp_path, monkeypatch):
    """``piper`` installed where the gateway puts an app's packages since core #3605: a
    directory the gateway appends to its OWN ``sys.path`` and that no plain Python child sees."""
    site = tmp_path / "app-python" / "site-packages"
    (site / "piper").mkdir(parents=True)
    (site / "piper" / "__init__.py").write_text("", encoding="utf-8")
    (site / "piper" / "__main__.py").write_text(_FAKE_PIPER_MAIN, encoding="utf-8")
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delitem(sys.modules, "piper", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr("provider.shutil.which", lambda _name: None)  # no piper on PATH
    return site


class TestPiperCommand:
    def test_configured_path_preferred(self, tmp_path):
        bin_path = tmp_path / "my-piper"
        _make_executable(str(bin_path))
        assert _piper_command(str(bin_path)) == ([str(bin_path)], None)

    def test_configured_missing_returns_none(self, tmp_path):
        assert _piper_command(str(tmp_path / "nope")) is None

    def test_falls_back_to_path(self):
        with patch("provider.shutil.which", return_value="/usr/local/bin/piper"):
            assert _piper_command("") == (["/usr/local/bin/piper"], None)

    def test_the_declared_package_runs_as_a_module_that_can_import_itself(
        self, packages_outside_the_default_path
    ):
        prefix, env = _piper_command("")
        assert prefix == [sys.executable, "-m", "piper"]
        assert str(packages_outside_the_default_path) in env["PYTHONPATH"].split(os.pathsep)

    def test_nothing_found_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("provider.shutil.which", return_value=None), \
             patch("provider.importlib.util.find_spec", return_value=None):
            assert _piper_command("") is None


@pytest.mark.asyncio
async def test_synthesis_works_when_piper_lives_only_in_the_app_packages(
    tmp_path, packages_outside_the_default_path
):
    """A REAL child process: it can import piper only because the app hands it the directory the
    gateway imports piper from. Before, the app looked for a ``piper`` console script beside the
    interpreter — where pip no longer puts it — and a plain child could not have imported piper."""
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"m")
    with patch("provider.sandbox_wrap_argv", side_effect=lambda c, mode: (c, None)):
        result = await _synthesize_piper_chunk("hello", piper_model=str(model))
    assert result is not None and os.path.getsize(result) > 100
    os.unlink(result)


# ── _synthesize_piper_chunk ────────────────────────────────────────────────────

class TestSynthesizePiper:
    @pytest.mark.asyncio
    async def test_binary_not_found_returns_none(self):
        with patch("provider._piper_command", return_value=None):
            assert await _synthesize_piper_chunk("hi") is None

    @pytest.mark.asyncio
    async def test_model_missing_returns_none(self, tmp_path):
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        with patch("provider._piper_command", return_value=([str(bin_path)], None)):
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

        with patch("provider._piper_command", return_value=([str(bin_path)], None)), \
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

        with patch("provider._piper_command", return_value=([str(bin_path)], None)), \
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
        with patch("provider._piper_command", return_value=([str(bin_path)], None)), \
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

        with patch("provider._piper_command", return_value=([str(bin_path)], None)), \
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

        with patch("provider._piper_command", return_value=([str(bin_path)], None)), \
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
