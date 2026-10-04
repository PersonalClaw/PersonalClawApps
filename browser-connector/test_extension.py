"""The extension's worker, driven in a browser double by Node's own test runner.

``test_extension.mjs`` loads the real ``extension/background.js`` against a fake browser API (tabs,
tab groups, windows, storage) and a fake loopback gateway and debugger, and checks what the worker
does: which page it announces, which tab each contract verb touches, and what it reports when a
run's tab closes or is taken over. This file runs it once and holds every case below to a pass BY
NAME, so a case that is renamed, dropped or skipped fails here instead of quietly vanishing.

Needs Node.js 18 or newer on PATH; no npm install and no browser. The copy runs in a scratch
folder beside a ``package.json`` that marks the files as ES modules, so it does not depend on a
Node new enough to detect that itself.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

CASES = (
    "the attach announces the run's own tab, never the first listed page",
    "a granted run's tab opens in the background, in a group named after the task",
    "every verb acts on the run's tab while another tab has focus",
    "a verb for another run, or for no run, is refused",
    "closing the group ends the run",
    "closing just the run's tab ends the run too",
    "bringing the run's tab to the front is a take-over",
    "a browser with no tab groups uses a window of its own",
    "a run whose tab cannot be opened is refused, never moved to an open tab",
    "a tab the debugger never lists is closed again and the run refused",
    "a run that ended is forgotten, and its tab is left to the owner",
    "a worker started again still knows each run's tab",
    "after the gateway restarts, the worker attaches again",
    "a worker another browser replaced stops asking, and attaches nothing over it",
    "a worker whose pairing was revoked stops asking",
)

_RESULT = re.compile(r"^(ok|not ok) \d+ - (.+?)(?: # (SKIP|TODO).*)?$", re.M)


@pytest.fixture(scope="module")
def node_results(tmp_path_factory) -> tuple[dict[str, str], str]:
    node = shutil.which("node")
    assert node, "the extension's tests need Node.js 18 or newer on PATH"
    work = tmp_path_factory.mktemp("extension")
    shutil.copytree(HERE / "extension", work / "extension")
    shutil.copy2(HERE / "test_extension.mjs", work / "test_extension.mjs")
    (work / "package.json").write_text('{"type": "module"}\n', encoding="utf-8")
    proc = subprocess.run(
        [node, "--test", "--test-reporter=tap", "test_extension.mjs"],
        cwd=work,
        env={"PATH": "/usr/bin:/bin:" + str(Path(node).parent), "HOME": str(work)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    results: dict[str, str] = {}
    for verdict, name, directive in _RESULT.findall(proc.stdout):
        results[name] = directive.lower() if directive else verdict
    return results, output


@pytest.mark.parametrize("case", CASES)
def test_the_worker_case_passes(node_results, case: str) -> None:
    results, output = node_results
    assert results.get(case) == "ok", f"{case!r} did not pass:\n{output}"


def test_no_case_runs_unnamed_here(node_results) -> None:
    """A case added to the JS file is held here too, so none can fail where nobody looks."""
    results, output = node_results
    assert set(results) == set(CASES), output
