"""No test here lets a model library write outside its home or report on its use.

Some libraries a model app's tests load do both by themselves: loading onnxruntime (``rapidocr``'s
tests load it for real) starts its maker's telemetry, a device identifier and a queue of events
about the machine kept under ``HOME``, and huggingface_hub keeps a list it fetches in the Hugging
Face folder other tools share. Every ``personalclaw`` command tells each library not to with its
own setting, and the repository's ``conftest.py`` sets the same settings before anything is
collected (``personalclaw.sdk.testing.library_env``).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: The switches, by the libraries' own names: each is on in a PersonalClaw process.
SWITCHES = (
    "HF_HUB_DISABLE_IMPLICIT_TOKEN",
    "HF_HUB_DISABLE_XET",
    "HF_HUB_DISABLE_TELEMETRY",
    "ORT_DISABLE_TELEMETRY",
)
#: The places the libraries are told to keep, or read, their files: a folder, or a file's address.
FOLDERS = ("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR", "TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL")


def test_this_session_tells_the_libraries_what_personalclaw_tells_them():
    for name in SWITCHES:
        assert os.environ.get(name) == "1", f"{name} is not set for this session's tests"
    for name in FOLDERS:
        folder = Path(os.environ.get(name, "").removeprefix("file://"))
        assert any(
            part.startswith("pclaw-apps-tests-") for part in folder.parts
        ), f"{name} points outside the session's scratch folder: {folder}"


#: A proxy nothing listens on, so a probe has nowhere to send what it would upload.
_NOWHERE = "http://127.0.0.1:9"
#: macOS's sandbox profile for a probe: nothing leaves the machine.
_NO_NETWORK = "(version 1)(allow default)(deny network-outbound (remote ip))"


def _load_onnxruntime(home: Path, env: dict[str, str]) -> list[str]:
    """The files under *home* after a fresh interpreter with *env* and that home loads onnxruntime,
    with no route out: a dead proxy, and macOS's sandbox where there is one."""
    home.mkdir()
    child = {k: v for k, v in env.items() if "proxy" not in k.lower()}
    child.update(
        {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "https_proxy": _NOWHERE,
            "HTTPS_PROXY": _NOWHERE,
            "all_proxy": _NOWHERE,
            "ALL_PROXY": _NOWHERE,
        }
    )
    argv = [sys.executable, "-c", "import onnxruntime"]
    if os.path.exists("/usr/bin/sandbox-exec"):
        argv = ["/usr/bin/sandbox-exec", "-p", _NO_NETWORK, *argv]
    subprocess.run(argv, env=child, capture_output=True, timeout=120, check=True)
    return sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file())


@pytest.mark.skipif(
    importlib.util.find_spec("onnxruntime") is None, reason="needs onnxruntime installed"
)
def test_a_test_that_loads_onnxruntime_leaves_no_device_identifier(tmp_path):
    """Found without importing it: this process loading onnxruntime is what would write into the
    real home. The control drops the switch and nothing else, for the moment an import takes."""
    session = {k: v for k, v in os.environ.items() if k not in ("DISABLE_TELEMETRY", "DO_NOT_TRACK")}
    left_alone = {k: v for k, v in session.items() if k != "ORT_DISABLE_TELEMETRY"}

    written = _load_onnxruntime(tmp_path / "control", left_alone)
    assert any(
        Path(f).name == "deviceid" for f in written
    ), f"control: without the switch onnxruntime keeps a device identifier in the home: {written}"

    assert _load_onnxruntime(tmp_path / "told", session) == [], "onnxruntime wrote into HOME"
