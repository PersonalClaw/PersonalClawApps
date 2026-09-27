"""The engine is looked for where the sidecar runs it, and the app says how to put it there.

Measured before this was written:

* ``SidecarRunner`` runs the worker under this app's own environment
  (``<home>/apps/voice-clone-tts/venv``) whenever that environment has an interpreter, and
  under the gateway's only when it does not. ``_detect_engine`` asked the gateway's
  interpreter in every case, so an engine installed where the sidecar runs left the app
  "unavailable" for good, and one only the gateway had was reported ready for a worker that
  could not import it.
* The unavailable card on Settings → Providers read "install it and download a model card's
  weights", naming a CosyVoice candidate the spike had already dropped, and gave no command.
  PersonalClaw has no UI that installs the engine, so that sentence is the only way in, and
  core shows at most 500 characters of it.
* The weights download runs in the gateway and imports ``huggingface_hub``, which core ships
  only in its ``tts`` extra. The app declared nothing, so the Download button failed on a
  plain install.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import provider

_BUNDLE = Path(__file__).resolve().parent


def _venv() -> Path:
    from personalclaw.sdk.sidecar import sidecar_venv_dir

    return sidecar_venv_dir("voice-clone-tts")


def _make_venv(*, engine: str = "") -> Path:
    """An environment the way ``python -m venv`` lays it out, with the engine if asked for:
    ``"package"`` is a regular install, ``"editable"`` leaves only the ``.dist-info``."""
    venv = _venv()
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    if engine == "package":
        (site / "omnivoice").mkdir()
        (site / "omnivoice" / "__init__.py").write_text("")
    elif engine == "editable":
        (site / "omnivoice-0.2.1.dist-info").mkdir()
    return venv


@pytest.fixture
def gateway_has_the_engine(monkeypatch, tmp_path):
    """An ``omnivoice`` the gateway's own interpreter can find (never imported)."""
    (tmp_path / "omnivoice").mkdir()
    (tmp_path / "omnivoice" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))


@pytest.mark.asyncio
async def test_an_engine_in_the_apps_own_environment_makes_it_available():
    """🔴 Red on main: the gateway's interpreter was asked, and it has no engine."""
    _make_venv(engine="package")
    assert provider._detect_engine() == "omnivoice"
    assert provider.availability() == (True, "")
    assert await provider.create_provider({}).is_available() is True


def test_an_editable_install_is_found_by_its_dist_info():
    _make_venv(engine="editable")
    assert provider._detect_engine() == "omnivoice"


@pytest.mark.usefixtures("gateway_has_the_engine")
def test_once_the_app_has_its_own_environment_the_gateway_copy_does_not_count():
    """🔴 Red on main: reported ready, for a worker that runs where there is no engine."""
    _make_venv()
    assert provider._detect_engine() == ""


@pytest.mark.usefixtures("gateway_has_the_engine")
def test_without_its_own_environment_the_gateway_interpreter_is_asked():
    """The runner's fallback, mirrored: no environment, so the worker runs on the gateway's."""
    assert not _venv().exists()
    assert provider._detect_engine() == "omnivoice"


def test_the_card_gives_the_command_with_this_machines_paths():
    """🔴 Red on main: no command, and a CosyVoice candidate that no longer exists."""
    ok, reason = provider.availability()
    assert ok is False
    app_dir = _venv().parent
    command = f"`cd {app_dir} && {sys.executable} -m venv venv && venv/bin/pip install omnivoice`"
    assert command in reason
    assert "PersonalClaw does not install it" in reason
    assert "Check again" in reason and reason.endswith("Settings → Models.")
    assert "CosyVoice" not in reason
    assert len(reason) <= provider._REASON_BUDGET, "core keeps 500 characters of a reason"


def test_paths_too_long_for_the_card_point_at_the_readme(monkeypatch):
    """Core cuts a reason at 500 characters, and a command cut short is worse than none. The
    first version of this sentence named the environment's path three times and lost its end
    ("download OmniVoice in Setting") on the card itself."""
    monkeypatch.setattr(provider.sys, "executable", "/" + "x" * 400 + "/python3")
    ok, reason = provider.availability()
    assert ok is False and len(reason) <= provider._REASON_BUDGET
    assert "README" in reason and "pip install" not in reason
    assert reason.endswith("Settings → Models.")


def test_the_listing_says_the_engine_is_not_installed_for_you():
    """🔴 Red on main: the description read as if cloning worked once the app was in."""
    description = json.loads((_BUNDLE / "app.json").read_text(encoding="utf-8"))["description"]
    assert "PersonalClaw does not install" in description
    assert "Settings → Providers" in description


def test_the_weights_download_brings_its_library():
    """🔴 Red on main: nothing declared, so Download failed without core's ``tts`` extra."""
    manifest = json.loads((_BUNDLE / "app.json").read_text(encoding="utf-8"))
    deps = manifest["dependencies"]["pythonDependencies"]
    assert any(d.startswith("huggingface-hub") for d in deps)
    assert "from huggingface_hub import snapshot_download" in (_BUNDLE / "provider.py").read_text(
        encoding="utf-8"
    ), "the download no longer uses huggingface_hub: re-argue this dependency"
