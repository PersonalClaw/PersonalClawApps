"""Every test in this repository runs against a scratch PersonalClaw home of its own.

Core resolves its home (the credential store, the security event log, every app's settings)
from ``PERSONALCLAW_HOME``, else ``~/.personalclaw``. A bundle suite whose own conftest does not
point that elsewhere wrote into the real home of whoever ran it: an apps-repo run on 2026-09-25
left egress rows (local-image-gen's ``/object_info/CheckpointLoaderSimple`` probe, an
``internal.local/hook`` call) in the owner's ``security_events.jsonl``. Core's own suite has a
guard for that. Nothing here did.

Two layers. ``pytest_configure`` points the home at a scratch directory before anything is
collected, because test modules import app code at collection time, before any fixture runs.
Then every test gets a directory of its own under it, created only when something resolves
the home. One home shared by the whole session is not enough: ``spec-builder``'s
``test_doctor_counts_an_unreadable_record`` isolates itself with a ``Path.home`` patch, which a
set ``PERSONALCLAW_HOME`` overrides, and it failed on files an earlier test had left in the
shared home (core rejected a global home for its suite for the same reason). A bundle's
own per-test home still wins inside its test.

The OS keychain is kept out as well: core reads it, and an uninstall's purge deletes from it,
whenever ``keyring`` is importable, and one keychain serves every home on the machine. The switch
is ``personalclaw.sdk.testing.keychain_off``, because this file imports core only through
``personalclaw.sdk``, like the apps it tests. A core without that switch stops the run rather
than letting it reach the real keychain.

So is every local model server on the machine. A connection to one's default port
(:data:`LOCAL_MODEL_PORTS`) is refused before it is made, unless this process is itself listening
there (a test's own fake), and the test that asked fails by name: a test that reached the host's
Ollama, vLLM or ComfyUI loaded a model on it, and what it proved depended on what that machine had
installed. The guard is ``personalclaw.sdk.testing.refuse_ports``, core's own suite's; a core
without it stops the run too. What it cannot see: a connection a child process makes.

``pytest.ini`` beside this file makes pytest load it however it is started, including by core's
quality verifier, which runs each bundle with the bundle as its working directory.
"""

from __future__ import annotations

import itertools
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

_patch = pytest.MonkeyPatch()
_base: list[Path] = []
_restore: list[Callable[[], None]] = []
_serial = itertools.count()
_refused_ports: list = []

#: The default ports of the local model servers the apps speak to: Ollama (11434), vLLM (8000)
#: and ComfyUI (8188).
LOCAL_MODEL_PORTS = frozenset({11434, 8000, 8188})


def pytest_configure(config):
    base = Path(tempfile.mkdtemp(prefix="pclaw-apps-tests-"))
    _base.append(base)
    _patch.setenv("PERSONALCLAW_HOME", str(base / "collect"))
    try:
        from personalclaw.sdk.testing import keychain_off, refuse_ports
    except ModuleNotFoundError as missing:
        if missing.name != "personalclaw":
            raise  # a core without the switches: this run would reach the real keychain
        return  # a bare environment without core: nothing reads a keychain
    _restore.append(keychain_off())
    guard = refuse_ports(LOCAL_MODEL_PORTS, what="a local model server's port")
    _refused_ports.append(guard)
    _restore.append(guard.undo)


@pytest.fixture(autouse=True)
def _no_test_reaches_a_real_local_model_server():
    """Fail the test that tried to connect to a local model server's port. The connection itself
    was refused before it was made (``refuse_ports``)."""
    yield
    for guard in _refused_ports:
        refused = guard.take()
        if refused:
            pytest.fail(
                "this test tried to reach a real local model server, and was refused: "
                + "; ".join(refused)
                + ". Fake the endpoint: a server the test starts on a port of its own, or a "
                "mocked transport (conftest.py).",
                pytrace=False,
            )


@pytest.fixture(autouse=True)
def _scratch_home_per_test(monkeypatch):
    """This test's own home. Not created here: core creates it the first time it is resolved,
    so a test that never touches the home leaves no directory behind."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(_base[0] / f"t{next(_serial)}"))


def pytest_unconfigure(config):
    while _restore:
        _restore.pop()()
    _patch.undo()
    while _base:
        shutil.rmtree(_base.pop(), ignore_errors=True)
