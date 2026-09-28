"""No test in this repository reaches a real local model server: its port is refused, and charged.

The repository's ``conftest.py`` installs core's port guard (``personalclaw.sdk.testing
.refuse_ports``) over the default ports of the local model servers the apps speak to, before
anything is collected. A connection to one is refused before it is made unless this process is
itself listening there, and the test that asked fails by name.

Every connection here is to a port this test picks from the ephemeral range and adds to the
installed guard, so a broken guard could never let one reach a real server: nothing listens there.
"""

from __future__ import annotations

import socket
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_the_guard_covers_the_local_model_servers_the_apps_speak_to():
    conftest = _conftest()
    assert {11434, 8000, 8188} <= conftest.LOCAL_MODEL_PORTS
    (guard,) = conftest._refused_ports
    assert guard.ports == conftest.LOCAL_MODEL_PORTS


def test_a_connection_to_a_guarded_port_is_refused_before_it_is_made(monkeypatch):
    (guard,) = _conftest()._refused_ports
    port = _free_port()
    monkeypatch.setattr(guard, "ports", guard.ports | {port})
    with pytest.raises(ConnectionRefusedError) as refused:
        socket.create_connection(("127.0.0.1", port), timeout=2)
    assert "local model server's port" in str(refused.value)
    [taken] = guard.take()  # taken here, so this test is not the one failed for it
    assert taken.endswith(f"-> 127.0.0.1:{port}")
    assert "test_a_connection_to_a_guarded_port_is_refused_before_it_is_made" in taken


def test_a_fake_this_test_serves_on_a_guarded_port_is_reached(monkeypatch):
    (guard,) = _conftest()._refused_ports
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = int(server.getsockname()[1])
        monkeypatch.setattr(guard, "ports", guard.ports | {port})
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    assert guard.take() == []
