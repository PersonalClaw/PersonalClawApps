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
def packages_outside_the_default_path(monkeypatch):
    """``piper`` installed where the gateway puts an app's packages since core #3605 — this
    test's ``<home>/app-python``, which is what core's ``app_packages_env`` reads: a directory
    the gateway appends to its OWN ``sys.path`` and that no plain Python child sees."""
    from personalclaw.apps.app_python import site_dirs

    site = site_dirs()[0]
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
        assert _piper_command(str(bin_path)) == ([str(bin_path)], False)

    def test_configured_missing_returns_none(self, tmp_path):
        assert _piper_command(str(tmp_path / "nope")) is None

    def test_falls_back_to_path(self):
        with patch("provider.shutil.which", return_value="/usr/local/bin/piper"):
            assert _piper_command("") == (["/usr/local/bin/piper"], False)

    def test_the_declared_package_runs_as_a_module_that_can_import_itself(
        self, packages_outside_the_default_path
    ):
        prefix, runs_declared_package = _piper_command("")
        assert prefix == [sys.executable, "-m", "piper"] and runs_declared_package is True

    def test_nothing_found_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("provider.shutil.which", return_value=None), \
             patch("provider.importlib.util.find_spec", return_value=None):
            assert _piper_command("") is None


#: What the gateway's environment may hold, which piper (someone else's program, reading a voice
#: someone else trained) must not see. Plain words, not key-shaped strings.
_PLANTED = {
    "ANTHROPIC_API_KEY": "planted-provider-key",
    "SLACK_BOT_TOKEN": "planted-bot-token",
    "BILLING_SERVICE_PASSWORD": "planted-password",
}

#: A piper that writes its WAV and, beside it, the NAMES of what it was started with and its
#: PYTHONPATH (names only, so a failing assert never prints a value).
_RECORDING_PIPER = """
import json, os, sys
out = sys.argv[sys.argv.index("-f") + 1]
sys.stdin.read()
with open(out, "wb") as f:
    f.write(b"RIFF" + b"x" * 400)
with open(out + ".env.json", "w") as f:
    json.dump({"names": sorted(os.environ), "pythonpath": os.environ.get("PYTHONPATH", "")}, f)
"""


def _plant(monkeypatch) -> None:
    for name, value in _PLANTED.items():
        monkeypatch.setenv(name, value)


def _recorded(wav: str) -> dict:
    import json

    with open(wav + ".env.json") as f:
        return json.load(f)


@pytest.mark.asyncio
async def test_a_piper_binary_runs_without_the_gateways_secrets(tmp_path, monkeypatch):
    """A REAL child: a piper on PATH is handed the child allowlist, never the gateway's
    environment. Before, it inherited all of it (``env=None``)."""
    _plant(monkeypatch)
    piper = tmp_path / "piper"
    piper.write_text(f"#!{sys.executable}\n" + _RECORDING_PIPER)
    piper.chmod(0o755)
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"m")
    monkeypatch.setattr("provider.shutil.which", lambda _name: str(piper))
    with patch("provider.sandbox_wrap_argv", side_effect=lambda c, mode: (c, None)):
        result = await _synthesize_piper_chunk("hello", piper_model=str(model))
    assert result is not None
    seen = _recorded(result)
    os.unlink(result)
    leaked = sorted(set(_PLANTED) & set(seen["names"]))
    assert leaked == [] and "PATH" in seen["names"] and "HOME" in seen["names"]


@pytest.mark.asyncio
async def test_the_declared_package_runs_with_the_app_packages_and_no_gateway_secret(
    tmp_path, monkeypatch, packages_outside_the_default_path
):
    """The declared package's child gets the app packages on its PYTHONPATH, and nothing else of
    the gateway's environment."""
    _plant(monkeypatch)
    (packages_outside_the_default_path / "piper" / "__main__.py").write_text(
        _RECORDING_PIPER, encoding="utf-8"
    )
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"m")
    with patch("provider.sandbox_wrap_argv", side_effect=lambda c, mode: (c, None)):
        result = await _synthesize_piper_chunk("hello", piper_model=str(model))
    assert result is not None
    seen = _recorded(result)
    os.unlink(result)
    leaked = sorted(set(_PLANTED) & set(seen["names"]))
    on_path = str(packages_outside_the_default_path) in seen["pythonpath"].split(os.pathsep)
    assert leaked == [] and on_path


@pytest.mark.asyncio
async def test_synthesis_works_when_piper_lives_only_in_the_app_packages(
    tmp_path, packages_outside_the_default_path
):
    """A REAL child process: it can import piper only because the app hands it core's app-packages
    environment. Before #124, the app looked for a ``piper`` console script beside the
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
        with patch("provider._piper_command", return_value=([str(bin_path)], False)):
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

        with patch("provider._piper_command", return_value=([str(bin_path)], False)), \
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

        with patch("provider._piper_command", return_value=([str(bin_path)], False)), \
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
        with patch("provider._piper_command", return_value=([str(bin_path)], False)), \
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

        with patch("provider._piper_command", return_value=([str(bin_path)], False)), \
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

        with patch("provider._piper_command", return_value=([str(bin_path)], False)), \
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


# ── voice downloads: into the home, and never with huggingface_hub's own token lookup ──────


def _fake_hub(monkeypatch) -> list[dict]:
    """``huggingface_hub`` as far as a voice download reaches it: ``hf_hub_download`` records
    what it was handed and writes the file where it was told to."""
    import types
    from pathlib import Path

    calls: list[dict] = []

    def hf_hub_download(*, repo_id, filename, local_dir, **kwargs):
        calls.append({"repo_id": repo_id, "filename": filename, "local_dir": local_dir, **kwargs})
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"voice")
        return str(target)

    hub = types.ModuleType("huggingface_hub")
    hub.hf_hub_download = hf_hub_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    return calls


@pytest.mark.asyncio
async def test_a_voice_download_goes_to_the_home_and_never_reads_the_cli_token(monkeypatch):
    """With no token PersonalClaw resolves, each fetch is told to use none (``False``). It used
    to pass no token at all, and then huggingface_hub looks for one itself: its lookup opens
    ``huggingface-cli login``'s token file, outside the home, without asking the owner."""
    from pathlib import Path

    from personalclaw.sdk.util import config_dir

    monkeypatch.setattr(prov, "resolve_token", lambda: "")
    calls = _fake_hub(monkeypatch)

    assert await PiperTtsProvider().download_voice("en_US-lessac-medium") is True

    assert [c["filename"].rsplit("/", 1)[-1] for c in calls] == [
        "en_US-lessac-medium.onnx",
        "en_US-lessac-medium.onnx.json",
    ]
    assert [c["token"] for c in calls] == [False, False]
    home = config_dir().resolve()
    assert all(Path(c["local_dir"]).resolve().is_relative_to(home) for c in calls)


@pytest.mark.asyncio
async def test_a_voice_download_uses_the_token_personalclaw_resolves(monkeypatch):
    monkeypatch.setattr(prov, "resolve_token", lambda: "hf_from_the_cascade")
    calls = _fake_hub(monkeypatch)
    assert await PiperTtsProvider().download_voice("en_US-lessac-medium") is True
    assert [c["token"] for c in calls] == ["hf_from_the_cascade"] * 2


def test_a_media_call_that_names_no_model_is_refused_and_sends_nothing():
    """A synthesis names its voice (the text-to-speech binding's model). One that names none is
    refused with the SDK's sentence, and can_synthesize says it cannot speak for it. It used to
    return nothing and say nothing, and can_synthesize said it could.

    A voice is on disk first, as on any home that downloaded one: an empty voice name used to
    find the first ``.onnx`` under the voices folder, so a claim made for no voice read as
    true there and only there."""
    import asyncio
    from pathlib import Path

    from apps_testkit.model_wire import (
        media_adapters,
        media_refusal_expected,
        media_refusal_report,
    )

    voice = prov._voices_dir() / "en_US-lessac-medium"
    voice.mkdir(parents=True)
    (voice / "en_US-lessac-medium.onnx").write_bytes(b"onnx")
    assert prov.voice_model_path("") != "", "precondition: an empty name finds a downloaded voice"

    adapters = media_adapters(Path(__file__).parent, prov.create_provider)
    report = asyncio.run(media_refusal_report(adapters))
    assert report == media_refusal_expected(adapters)


def test_asking_whether_voices_can_download_loads_no_library(monkeypatch):
    """Every Models page asks this, and asking used to import huggingface_hub."""
    monkeypatch.delitem(sys.modules, "huggingface_hub", raising=False)

    prov.availability()

    assert "huggingface_hub" not in sys.modules


def test_without_huggingface_hub_it_says_how_to_get_it(monkeypatch):
    """The package ships with this app, so the fix is its reinstall, which the desktop app
    cannot do."""
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)

    assert prov.availability() == (
        False,
        "Piper voice downloads need huggingface-hub, which ships with this app, not with "
        "PersonalClaw itself. Reinstall Piper TTS from the Store. The desktop app cannot install "
        "it: use the server or container build there.",
    )
