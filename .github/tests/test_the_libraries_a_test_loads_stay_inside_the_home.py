"""No test here lets a model library write outside its home or report on its use.

Some libraries a model app's tests load do both by themselves: loading onnxruntime (``rapidocr``'s
tests load it for real) starts its maker's telemetry, a device identifier and a queue of events
about the machine kept under ``HOME``, and huggingface_hub keeps a list it fetches in the Hugging
Face folder other tools share. Every ``personalclaw`` command tells each library not to with its
own setting, and the repository's ``conftest.py`` sets the same settings before anything is
collected (``personalclaw.sdk.testing.library_env``).

The onnxruntime probe loads it with its telemetry on as a control, so on macOS it runs only inside
a no-network sandbox. A run that is itself inside a macOS sandbox cannot start one: macOS refuses
with exactly ``sandbox-exec: sandbox_apply: Operation not permitted`` and exit 71, and the probe
skips naming that. Any other outcome of the sandbox, a bad profile included, still reaches the
probe and fails it.
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


_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
#: What macOS answers when a process already inside a sandbox asks for another: sandbox_apply
#: fails with EPERM and sandbox-exec exits 71, before it runs anything.
_NESTED_REFUSAL = "sandbox-exec: sandbox_apply: Operation not permitted"


def _sandbox_refused(sandbox_exec: str = _SANDBOX_EXEC) -> str | None:
    """macOS's refusal, word for word, when it will not apply the probe's sandbox; None when the
    sandbox applies, and None for any other failure too, so that one reaches the probe and fails."""
    run = subprocess.run([sandbox_exec, "-p", _NO_NETWORK, "/usr/bin/true"],
                         capture_output=True, text=True, timeout=30)
    said = run.stderr.strip()
    return said if run.returncode == 71 and said == _NESTED_REFUSAL else None


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
    if os.path.exists(_SANDBOX_EXEC) and (refusal := _sandbox_refused()):
        # The probe loads onnxruntime with its telemetry on (the control), so it runs only where
        # nothing can leave the machine. A run that is itself inside a macOS sandbox (an agent's
        # or an IDE's) cannot start a second one, and the probe is not run without it.
        pytest.skip(f"macOS will not nest the probe's no-network sandbox in this already "
                    f"sandboxed process ({refusal!r}, exit 71)")
    session = {k: v for k, v in os.environ.items() if k not in ("DISABLE_TELEMETRY", "DO_NOT_TRACK")}
    left_alone = {k: v for k, v in session.items() if k != "ORT_DISABLE_TELEMETRY"}

    written = _load_onnxruntime(tmp_path / "control", left_alone)
    assert any(
        Path(f).name == "deviceid" for f in written
    ), f"control: without the switch onnxruntime keeps a device identifier in the home: {written}"

    assert _load_onnxruntime(tmp_path / "told", session) == [], "onnxruntime wrote into HOME"


def _fake_sandbox_exec(tmp_path: Path, status: int, stderr: str) -> str:
    """A stand-in for ``sandbox-exec`` that writes *stderr* and exits *status*, whatever it is
    asked to run."""
    fake = tmp_path / "sandbox-exec"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' {stderr!r} >&2\nexit {status}\n", encoding="utf-8")
    fake.chmod(0o755)
    return str(fake)


def test_a_nested_sandbox_refusal_is_recognised_by_its_exact_signature(tmp_path):
    refused = _fake_sandbox_exec(tmp_path, 71, "sandbox-exec: sandbox_apply: Operation not permitted")
    assert _sandbox_refused(refused) == "sandbox-exec: sandbox_apply: Operation not permitted"


@pytest.mark.parametrize(
    ("status", "stderr"),
    [
        (0, ""),
        (65, "sandbox-exec: unbound variable: bogus-rule at <input string>, line 1, column 13"),
        (71, "sandbox-exec: execvp() of 'python' failed: No such file or directory"),
        (1, "sandbox-exec: sandbox_apply: Operation not permitted"),
    ],
    ids=["it runs", "a bad profile", "another exit-71 error", "the message under another status"],
)
def test_any_other_sandbox_outcome_is_not_taken_for_one(tmp_path, status, stderr):
    """Only the refusal itself skips the probe: anything else reaches it and fails there."""
    assert _sandbox_refused(_fake_sandbox_exec(tmp_path, status, stderr)) is None
