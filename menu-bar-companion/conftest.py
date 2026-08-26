"""Test wiring for the menu-bar companion.

Puts the app dir on ``sys.path`` (the way ``run.py`` does at runtime) and pins the
companion's state directory at a per-test tmp dir via ``PERSONALCLAW_COMPANION_HOME``.

The env var is the lever on purpose: ``settings.companion_home()`` re-reads it on every
call and caches nothing, so setting the env isolates every path in the package at once.
Monkeypatching a module attribute instead would not be undoable once a consumer module
had already bound it at import time — and it would leave a run able to write the real
``~/Library/Application Support`` (or, for the manifest test, the real
``~/.personalclaw``, which nothing here touches).
"""

import sys
from pathlib import Path

import pytest

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))


@pytest.fixture(autouse=True)
def _isolate_companion_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("companion-home")
    monkeypatch.setenv("PERSONALCLAW_COMPANION_HOME", str(home))
    # Credentials-by-env is a supported mode; clear it so a developer's shell can't
    # change what these tests measure.
    monkeypatch.delenv("PERSONALCLAW_COMPANION_URL", raising=False)
    monkeypatch.delenv("PERSONALCLAW_COMPANION_TOKEN", raising=False)
    return home
