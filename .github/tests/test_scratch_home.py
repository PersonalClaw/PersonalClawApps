"""Every test in this repository runs against a scratch PersonalClaw home, however pytest starts.

The repository's ``conftest.py`` points ``PERSONALCLAW_HOME`` at a scratch directory of each
test's own, and ``pytest.ini`` is what makes pytest load it. Without the ini, pytest started
inside a bundle takes the bundle as its rootdir and never reads a conftest above it. That is how
core's quality verifier starts every bundle it checks.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_this_session_runs_against_a_scratch_home():
    home = Path(os.environ.get("PERSONALCLAW_HOME", ""))
    assert str(home) != ".", "PERSONALCLAW_HOME is unset, so core would resolve ~/.personalclaw"
    # This test's own directory under the session's scratch base, not the base every test shares.
    assert home.parent.name.startswith("pclaw-apps-tests-") and home.name.startswith("t"), home
    assert home.resolve() != (Path.home() / ".personalclaw").resolve()


def test_a_run_started_inside_a_bundle_loads_it_too(tmp_path):
    """Core's quality verifier runs ``python -m pytest <bundle> -q -p no:cacheprovider`` with the
    bundle as the working directory. The same command, collecting only, must register the
    repository's conftest. Without ``pytest.ini`` it does not: measured, only the bundle's own
    ``tests/conftest.py`` loads.

    The child gets no ``PERSONALCLAW_HOME`` from this session, and a scratch ``HOME``: collecting
    imports core, and when this check fails there is no conftest to keep that off the real home."""
    bundle = ROOT / "mail-inbox"
    env = {k: v for k, v in os.environ.items() if k != "PERSONALCLAW_HOME"}
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(bundle), "-q", "-p", "no:cacheprovider",
         "--collect-only", "--trace-config"],
        cwd=bundle,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"from '{ROOT / 'conftest.py'}'" in proc.stdout, proc.stdout[-2000:]


_TAKES_THE_HOME_AWAY = '''
from personalclaw.sdk.util import config_dir


def test_innocent():
    assert True


def test_takes_it_away_with_a_home_of_its_own(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PERSONALCLAW_HOME")
    config_dir()


def test_takes_the_home_away(monkeypatch):
    monkeypatch.delenv("PERSONALCLAW_HOME")
    config_dir()
'''


def test_the_test_during_which_the_default_home_appears_fails_by_name(tmp_path):
    """Core makes the home the first time it resolves it, so a test that takes the conftest's home
    away resolves ``~/.personalclaw`` under the ``HOME`` of whoever runs the tests, and makes it.
    Driven in a child pytest that loads the repository's conftest, with a scratch ``HOME`` standing
    for the real one: the test that made it fails by name, and the one that pointed ``HOME`` at a
    folder of its own first passes."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "test_inner.py").write_text(_TAKES_THE_HOME_AWAY, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "PERSONALCLAW_"))}
    env["HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), env.get("PYTHONPATH", "")) if p)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider",
         "-p", "conftest", str(work / "test_inner.py")],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert (home / ".personalclaw").is_dir(), "control: the test did make the default home" + out
    assert proc.returncode == 1, out[-3000:]
    assert "ERROR test_inner.py::test_takes_the_home_away" in out, out[-3000:]
    assert f"{home / '.personalclaw'} did not exist as this test started" in out, out[-3000:]
    assert "3 passed, 1 error" in out, out[-3000:]
