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
own per-test home still wins inside its test. A test that takes the home away, to see what the
default is, resolves ``~/.personalclaw`` under the real ``HOME``, and core makes it: the test
during which the real one appears fails by name.

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

And so is the machine's git configuration, and with it its credential helpers: on a Mac, the one
Apple's git bundles hands credentials to the owner's real keychain. Every test's git runs with no
system configuration, a global file of the test's own, and an empty ``credential.helper`` on its
command line (``personalclaw.sdk.testing.neutral_git_env``), and a git that could still sign in
with a helper of the machine's own is refused before it starts, and the test that started it fails
by name (``refuse_git_helpers``, the guard core's own suite installs). Core's git reads the same
files; a core whose git would still name the machine's helper stops the run.

And so are the libraries a model app's tests load. Some write outside any home by themselves, or
report on their use to the people who make them: loading onnxruntime (``rapidocr``'s tests load it
for real) starts its maker's telemetry, a device identifier and a queue of events about the
machine kept under the real ``HOME``, and huggingface_hub keeps a list it fetches in the Hugging
Face folder other tools share. Every ``personalclaw`` command tells each library not to with its
own setting, and so does this file, before anything is collected
(``personalclaw.sdk.testing.library_env``), so a test loads them the way PersonalClaw does. A core
without that switch stops the run.

All four are core's. A bare environment without core installs none of them: nothing there reads a
keychain, and every test here that signs git in needs core to run at all.

``pytest.ini`` beside this file makes pytest load it however it is started, including by core's
quality verifier, which runs each bundle with the bundle as its working directory.
"""

from __future__ import annotations

import itertools
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

_patch = pytest.MonkeyPatch()
_base: list[Path] = []
_restore: list[Callable[[], None]] = []
_serial = itertools.count()
_refused_ports: list = []
_git_guards: list = []

#: The default ports of the local model servers the apps speak to: Ollama (11434), vLLM (8000)
#: and ComfyUI (8188).
LOCAL_MODEL_PORTS = frozenset({11434, 8000, 8188})

#: The home of whoever runs the tests, read before any test can point ``HOME`` elsewhere: its
#: ``~/.gitconfig`` is theirs.
REAL_HOME = os.path.realpath(os.path.expanduser("~"))
#: The PersonalClaw home core resolves when nothing names one: theirs too, so no test makes it.
DEFAULT_HOME = Path(REAL_HOME) / ".personalclaw"

#: A remote no git in the check below reaches: nothing is started for it.
_NOWHERE = "https://example.invalid/state.git"


def pytest_configure(config):
    base = Path(tempfile.mkdtemp(prefix="pclaw-apps-tests-"))
    _base.append(base)
    _patch.setenv("PERSONALCLAW_HOME", str(base / "collect"))
    try:
        from personalclaw.sdk.git import git_argv, git_env
        from personalclaw.sdk.testing import (
            keychain_off,
            library_env,
            neutral_git_env,
            refuse_git_helpers,
            refuse_ports,
        )
    except ModuleNotFoundError as missing:
        if missing.name != "personalclaw":
            raise  # a core without the guards: this run would reach the real keychain
        # A bare environment without core, as the per-bundle runner's own rail makes: nothing
        # there reads a keychain, and every test here that signs git in needs core to run at all.
        return
    for name, value in neutral_git_env(base / "collect.gitconfig").items():
        _patch.setenv(name, value)
    for name, value in library_env().items():
        _patch.setenv(name, value)
    git = refuse_git_helpers(real_home=REAL_HOME, own=[str(base), str(ROOT)])
    _git_guards.append(git)
    _restore.append(git.undo)
    _restore.append(keychain_off())
    guard = refuse_ports(LOCAL_MODEL_PORTS, what="a local model server's port")
    _refused_ports.append(guard)
    _restore.append(guard.undo)
    why = git.refusal(git_argv(["ls-remote", _NOWHERE]), git_env(remote=True))
    if why:
        raise pytest.UsageError(
            f"core's git would sign a test in with the machine's own credential helper: it {why}. "
            "A core whose personalclaw.sdk.git.git_env drops GIT_CONFIG_NOSYSTEM and "
            "GIT_CONFIG_GLOBAL does this; run the tests against one that keeps them."
        )


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
def _no_test_signs_git_in_with_the_machines_helper():
    """Fail the test that started a git that could sign in with a credential helper of the
    machine's own. The git itself was refused before it started (``refuse_git_helpers``)."""
    yield
    for guard in _git_guards:
        refused = guard.take()
        if refused:
            pytest.fail(
                "this test started a git that could sign in with the machine's own credential "
                "helper, and it was refused: " + "; ".join(refused) + ". Run it with the "
                "environment the tests inherit (conftest.py), and put a setting of the test's "
                "own in the file GIT_CONFIG_GLOBAL names.",
                pytrace=False,
            )


@pytest.fixture(autouse=True)
def _no_test_makes_the_default_home():
    """Fail the test during which the real ``~/.personalclaw`` appeared. Core makes the home the
    first time it resolves it, so a test that takes this file's home away, to see what the
    default is, makes the real one unless its ``HOME`` is a folder of its own too: two did, and
    every run left an empty real home behind. A child process the test started may have made it,
    or a process outside this run. The folder is left as it was found."""
    there = os.path.lexists(DEFAULT_HOME)
    yield
    if not there and os.path.lexists(DEFAULT_HOME):
        pytest.fail(
            f"{DEFAULT_HOME} did not exist as this test started, and it does now: the test, or a "
            "process it started, resolved the default home under the real HOME (unless a process "
            "outside this run made it). Point HOME at a folder of the test's own before taking "
            "PERSONALCLAW_HOME away (conftest.py). The folder is left as it was found.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _scratch_home_per_test(monkeypatch):
    """This test's own home, and its own global git file. Neither is created here: core creates
    the home the first time it is resolved, and git the file on the first write, so a test that
    never touches them leaves nothing behind."""
    serial = next(_serial)
    monkeypatch.setenv("PERSONALCLAW_HOME", str(_base[0] / f"t{serial}"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_base[0] / f"t{serial}.gitconfig"))


def pytest_unconfigure(config):
    while _restore:
        _restore.pop()()
    _patch.undo()
    while _base:
        shutil.rmtree(_base.pop(), ignore_errors=True)
