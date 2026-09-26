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
shared home (core rejected a global home for its suite for the same reason, CRE-6). A bundle's
own per-test home still wins inside its test.

The OS keychain is kept out as well: core reads it, and an uninstall's purge deletes from it,
whenever ``keyring`` is importable, and one keychain serves every home on the machine.

``pytest.ini`` beside this file makes pytest load it however it is started, including by core's
quality verifier, which runs each bundle with the bundle as its working directory.
"""

from __future__ import annotations

import itertools
import shutil
import tempfile
from pathlib import Path

import pytest

_patch = pytest.MonkeyPatch()
_base: list[Path] = []
_serial = itertools.count()


def pytest_configure(config):
    base = Path(tempfile.mkdtemp(prefix="pclaw-apps-tests-"))
    _base.append(base)
    _patch.setenv("PERSONALCLAW_HOME", str(base / "collect"))
    try:
        from personalclaw.config import credentials
    except ImportError:  # a bare environment without core: nothing reads a keychain
        return
    _patch.setattr(credentials, "_usable_keyring", lambda: None)


@pytest.fixture(autouse=True)
def _scratch_home_per_test(monkeypatch):
    """This test's own home. Not created here: core creates it the first time it is resolved,
    so a test that never touches the home leaves no directory behind."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(_base[0] / f"t{next(_serial)}"))


def pytest_unconfigure(config):
    _patch.undo()
    while _base:
        shutil.rmtree(_base.pop(), ignore_errors=True)
