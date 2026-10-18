"""Every stub server a test starts stops at once, not on its next half-second poll.

``socketserver``'s ``serve_forever`` checks for a shutdown request every ``poll_interval`` seconds,
0.5 by default, and ``shutdown()`` waits for that check. A test whose stub server is started with
the default therefore waits up to half a second when it stops, whatever it tested: s3-sync's suite
spent most of its 50 s there, one stub per test. This rail reads every tracked ``.py`` file and
fails on a ``serve_forever`` started without a ``poll_interval`` of at most 0.05 s: called
directly, or handed to a ``threading.Thread`` as its target (with the interval in ``kwargs``).
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The longest poll a stub server may keep: a stop then costs at most this.
MAX_POLL = 0.05

#: The stub servers in the tree as this rail lands: a scan that stopped finding them would pass
#: while checking nothing.
KNOWN = {
    "apps_testkit/egress.py", "apps_testkit/model_wire.py",
    "bedrock-models/test_embedding_models.py", "bedrock-models/test_image_models.py",
    "git-sync/test_provider.py", "local-image-gen/test_provider.py",
    "s3-sync/test_provider.py", "vector-store-qdrant/test_api_key.py",
}


def _is_serve_forever(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "serve_forever"


def _poll(keywords: list[ast.keyword]) -> float | None:
    for keyword in keywords:
        if keyword.arg == "poll_interval" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _thread_poll(call: ast.Call) -> float | None:
    """The ``poll_interval`` in a ``Thread(target=…serve_forever, kwargs={…})``."""
    for keyword in call.keywords:
        if keyword.arg == "kwargs" and isinstance(keyword.value, ast.Dict):
            for key, value in zip(keyword.value.keys, keyword.value.values):
                if (isinstance(key, ast.Constant) and key.value == "poll_interval"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, (int, float))):
                    return float(value.value)
    return None


def problems(source: str, name: str) -> list[str]:
    """Each ``serve_forever`` in *source* started without a short ``poll_interval``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=name)):
        if not isinstance(node, ast.Call):
            continue
        if _is_serve_forever(node.func):
            poll = _poll(node.keywords)
        elif any(k.arg == "target" and _is_serve_forever(k.value) for k in node.keywords):
            poll = _thread_poll(node)
        else:
            continue
        if poll is None or poll > MAX_POLL:
            found.append(f"{name}:{node.lineno}: serve_forever with poll_interval "
                         f"{'unset (0.5 s)' if poll is None else poll}; pass "
                         f"poll_interval={MAX_POLL} or less, so stopping it costs no wait")
    return found


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=ROOT, check=True,
                         capture_output=True).stdout
    return sorted(p.decode() for p in out.split(b"\0") if p)


def test_every_stub_server_in_the_tree_stops_at_once():
    found, starters = [], set()
    for name in _tracked():
        source = (ROOT / name).read_text(encoding="utf-8")
        if "serve_forever" not in source:
            continue
        if name.startswith(".github/tests/"):
            continue  # this rail's own planted sources
        starters.add(name)
        found += problems(source, name)
    assert KNOWN <= starters, f"the scan no longer finds {sorted(KNOWN - starters)}"
    assert not found, "\n".join(found)


# -- the scan, on planted sources -------------------------------------------------------------

def test_a_thread_target_without_a_poll_interval_is_named():
    src = "import threading\nthreading.Thread(target=srv.serve_forever, daemon=True).start()\n"
    assert problems(src, "x.py") == [
        "x.py:2: serve_forever with poll_interval unset (0.5 s); pass poll_interval=0.05 or "
        "less, so stopping it costs no wait"]


def test_a_direct_call_without_a_poll_interval_is_named():
    assert len(problems("srv.serve_forever()\n", "x.py")) == 1


def test_a_poll_interval_longer_than_the_limit_is_named():
    assert len(problems("srv.serve_forever(poll_interval=0.5)\n", "x.py")) == 1
    src = "Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.2})\n"
    assert len(problems(src, "x.py")) == 1


def test_a_short_poll_interval_passes_either_way():
    assert problems("srv.serve_forever(poll_interval=0.01)\n", "x.py") == []
    src = "Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)\n"
    assert problems(src, "x.py") == []
