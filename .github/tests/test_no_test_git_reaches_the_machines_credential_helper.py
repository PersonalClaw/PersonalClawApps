"""No test in this repository can sign git in with a credential helper of the machine's own.

The repository's ``conftest.py`` gives every test's git a neutral environment
(``personalclaw.sdk.testing.neutral_git_env``): no system configuration, a global file of the
test's own, and an empty ``credential.helper`` on git's command line. Core's git reads the same
files. A git that could still sign in with a helper of the machine's own is refused before it
starts, and the test that started it fails by name (``refuse_git_helpers``): on a Mac the file
Apple's git bundles names ``osxkeychain``, and on 2026-09-28 a test's own ``git clone`` handed a
token to it and waited ten minutes on the owner's real keychain. The guard is the one core's own
suite installs, and core's rail holds it to every way a git could reach the machine's helper;
this one holds this repository's run to it.

Nothing here reaches a remote or a real helper. The refusals are asked of the guard, the one git
that is refused would only have dialed a port on this machine that nothing listens on, the one
that runs asks git's credential machinery with prompts off and nothing to answer, and every
helper named is this file's own invention.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _conftest():
    """The repository's conftest, as pytest loaded it."""
    path = str(ROOT / "conftest.py")
    loaded = [m for m in sys.modules.values() if getattr(m, "__file__", "") == path]
    assert loaded, "the repository's conftest.py was not loaded"
    return loaded[0]


def _guard():
    (guard,) = _conftest()._git_guards
    return guard


def _nowhere() -> str:
    """An https URL on this machine that nothing answers."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return f"https://127.0.0.1:{port}/state.git"


def _effective(values: list[str]) -> list[str]:
    """The helpers git runs from its ``credential.helper`` values, in order: an empty one clears
    the ones before it."""
    helpers: list[str] = []
    for value in values:
        helpers = [*helpers, value] if value else []
    return helpers


def test_every_tests_git_reads_no_configuration_of_the_machines(tmp_path):
    """Asked from a folder that is no repository, git names only what its command line sets."""
    nosystem = os.environ.get("GIT_CONFIG_NOSYSTEM")
    assert nosystem == "1", nosystem  # the value alone: the environment holds the owner's secrets
    own = Path(os.environ.get("GIT_CONFIG_GLOBAL", ""))
    assert own.parent == _conftest()._base[0], "the global file is not the test's own"

    listed = subprocess.run(
        ["git", "config", "--show-origin", "--list"],
        cwd=tmp_path,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    assert listed, "git listed nothing at all: the check below would be vacuous"
    assert [line for line in listed if not line.startswith("command line:")] == [], listed
    helpers = subprocess.run(
        ["git", "config", "--get-all", "credential.helper"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")[:-1]
    assert helpers and _effective(helpers) == [], helpers


def test_cores_git_signs_a_test_in_with_no_helper_of_the_machines():
    from personalclaw.sdk.git import git_argv, git_env

    env = git_env(remote=True)
    argv = git_argv(["ls-remote", _nowhere()])

    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.environ["GIT_CONFIG_GLOBAL"]
    named = [s.split("=", 1)[1] for f, s in zip(argv, argv[1:]) if f == "-c"
             and s.lower().startswith("credential.helper=")]
    assert named and _effective(named) == [], argv
    assert _guard().refusal(argv, env) == ""


def test_a_git_that_could_sign_in_with_the_machines_helper_is_refused_and_named():
    with pytest.raises(PermissionError) as refused:
        subprocess.run(
            ["git", "-c", "credential.helper=pc-fixture-machine-helper", "ls-remote", _nowhere()],
            capture_output=True,
            timeout=30,
        )

    assert "refused by the test suite" in str(refused.value)
    [taken] = _guard().take()  # taken here, so this test is not the one failed for it
    assert "test_a_git_that_could_sign_in_with_the_machines_helper_is_refused_and_named" in taken
    assert taken.endswith("signs in with a credential helper of the machine's own "
                          "(pc-fixture-machine-helper)")


def test_a_git_that_is_allowed_really_runs_and_no_helper_answers(tmp_path):
    """The guard lets a neutral git start: git's own credential machinery, asked for a sign-in
    with nothing to answer it and prompts off, gives up without a password."""
    helpers = subprocess.run(
        ["git", "config", "--get-all", "credential.helper"], cwd=tmp_path, capture_output=True,
        text=True,
    ).stdout.split("\n")[:-1]
    assert _effective(helpers) == [], "not neutral, so git credential is not started"

    asked = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=example.invalid\n\n",
        cwd=tmp_path,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert asked.returncode != 0 and "password=" not in asked.stdout, asked
    assert "terminal prompts disabled" in asked.stderr
    assert _guard().take() == []


def test_the_guard_is_the_one_cores_own_suite_installs():
    from personalclaw.sdk.testing import GitGuard

    assert isinstance(_guard(), GitGuard)


def test_a_core_whose_git_would_name_the_machines_helper_stops_the_run(tmp_path):
    """The conftest asks core's git before any test runs: a core that still reads the machine's
    own configuration, and so signs in with its keychain helper, stops the run rather than letting
    a test's git reach it. Played by a core whose owner sign-in is replaced before pytest starts
    (``sitecustomize``), in a run of one test of this file."""
    plant = tmp_path / "plant"
    plant.mkdir()
    (plant / "sitecustomize.py").write_text(
        "import personalclaw.net.git as g\n"
        "g._owner_auth_settings = lambda: ['credential.helper=pc-fixture-machine-helper']\n",
        encoding="utf-8",
    )
    path = os.pathsep.join(p for p in (str(plant), os.environ.get("PYTHONPATH", "")) if p)
    env = {**os.environ, "PYTHONPATH": path}
    env.pop("PYTEST_CURRENT_TEST", None)
    one = f"{Path(__file__)}::test_the_guard_is_the_one_cores_own_suite_installs"

    ran = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", one],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )

    said = ran.stdout + ran.stderr
    assert ran.returncode != 0, said
    assert "core's git would sign a test in with the machine's own credential helper" in said
    assert "(pc-fixture-machine-helper)" in said
    assert " passed" not in said, "a test ran after the check said to stop"
