"""The ``ScanOnce`` copies in slack-channel's and email-channel's install tests run core's real scan.

Those two test files install the real bundle with core's own installer, ``app_manager.install``,
once per test. The installer's supply-chain scan is the slow part, so each file keeps a copy of
``ScanOnce``, which runs that scan once per staged bundle's bytes and reuses the report while the
bytes are the same. The copy lives in each test file, not in ``apps_testkit``, because it reaches
core internals (the installer's scanner and core's bundle digest), and only tests may.

Each copy is taken from between its file's markers and driven against core's installer on a
planted bundle in a scratch home, with a counter around core's real scanner:

- an install through ``ScanOnce`` reaches core's real scanner, and its report is the real one;
- a second install of the same bytes runs no scan and gets a report equal to the first;
- a bundle changed in any file is scanned afresh;
- a new ``ScanOnce`` (another test file) shares nothing with the last one;
- an install with no ``ScanOnce`` in use is scanned every time, as before.

And of the files themselves: both copies are the same, and each file's install fixture uses it.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_FILES = {
    app: ROOT / app / "tests" / "test_setup_secrets_uninstall.py"
    for app in ("slack-channel", "email-channel")
}
_BEGIN = "# --- scan once per file: begin"
_END = "# --- scan once per file: end ---"
_APP = "planted-app"


def _block(app: str) -> str:
    """The copy of ``ScanOnce`` in ``app``'s install tests, without its first marker line."""
    lines = _FILES[app].read_text(encoding="utf-8").splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(_BEGIN)]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\n") == _END]
    assert len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0], (
        f"{_FILES[app].relative_to(ROOT)} has no single ScanOnce block between its markers")
    return "".join(lines[starts[0] + 1:ends[0] + 1])


def _scan_once_class(app: str):
    """``ScanOnce`` as that file defines it, run against core's own installer."""
    from personalclaw.apps import app_manager

    namespace = {"app_manager": app_manager, "copy": copy, "Path": Path,
                 "__name__": f"scan_once_{app.replace('-', '_')}"}
    exec(compile(_block(app), str(_FILES[app]), "exec"), namespace)  # noqa: S102 - our own file
    return namespace["ScanOnce"]


@pytest.fixture(params=sorted(_FILES))
def ScanOnce(request):  # noqa: N802 - reads as the class under test
    return _scan_once_class(request.param)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A scratch home, a planted bundle, and a count of core's real scans."""
    from personalclaw.apps import app_manager

    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "home"))
    bundle = tmp_path / _APP
    bundle.mkdir()
    (bundle / "app.json").write_text(json.dumps({
        "name": _APP, "version": "0.0.1", "displayName": "Planted", "description": "Planted.",
    }), encoding="utf-8")
    (bundle / "provider.py").write_text("VALUE = 1\n", encoding="utf-8")
    (bundle / "LICENSE").write_text("MIT License\n", encoding="utf-8")

    real = app_manager.default_scanner
    scans: list[Path] = []

    class Counting:
        def scan(self, staged, *args, **kwargs):
            scans.append(Path(staged))
            return real.scan(staged, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(app_manager, "default_scanner", Counting())
    yield app_manager, bundle, scans
    app_manager.force_uninstall(_APP)


def _install(app_manager, bundle):
    result = app_manager.install(bundle, confirm=True)
    assert result.ok, result.error
    app_manager.force_uninstall(_APP)
    return result


def test_the_first_install_runs_core_s_real_scan(world, monkeypatch, ScanOnce):
    app_manager, bundle, scans = world
    once = ScanOnce()
    once.use(monkeypatch)
    result = _install(app_manager, bundle)
    assert len(scans) == 1 and (once.real_scans, once.reused) == (1, 0)
    assert result.scan is not None and result.scan.surfaces_scanned


def test_a_second_install_of_the_same_bytes_reuses_the_first_report(world, monkeypatch, ScanOnce):
    app_manager, bundle, scans = world
    once = ScanOnce()
    once.use(monkeypatch)
    first = _install(app_manager, bundle)
    second = _install(app_manager, bundle)
    assert len(scans) == 1, "the second install ran the real scan again"
    assert (once.real_scans, once.reused) == (1, 1)
    assert second.scan.to_dict() == first.scan.to_dict()


def test_the_report_reused_is_what_core_s_scan_says_of_those_bytes(world, monkeypatch, ScanOnce):
    # Against a scan of the same bundle run apart from ScanOnce: the reused report says the same.
    app_manager, bundle, scans = world
    plain = _install(app_manager, bundle)
    once = ScanOnce()
    once.use(monkeypatch)
    _install(app_manager, bundle)
    reused = _install(app_manager, bundle)
    assert once.reused == 1
    assert reused.scan.to_dict() == plain.scan.to_dict()


def test_a_bundle_changed_in_any_file_is_scanned_afresh(world, monkeypatch, ScanOnce):
    app_manager, bundle, scans = world
    once = ScanOnce()
    once.use(monkeypatch)
    _install(app_manager, bundle)
    (bundle / "provider.py").write_text("VALUE = 2\n", encoding="utf-8")
    _install(app_manager, bundle)
    (bundle / "extra.txt").write_text("a file the first scan never saw\n", encoding="utf-8")
    _install(app_manager, bundle)
    assert len(scans) == 3 and (once.real_scans, once.reused) == (3, 0)


def test_another_test_file_s_scan_once_shares_nothing(world, monkeypatch, ScanOnce):
    app_manager, bundle, scans = world
    ScanOnce().use(monkeypatch)
    _install(app_manager, bundle)
    monkeypatch.undo()
    from personalclaw.apps import app_manager as again

    real = again.default_scanner
    monkeypatch.setattr(again, "default_scanner", type(real)())  # core's scanner, uncounted
    other = ScanOnce()
    other.use(monkeypatch)
    _install(again, bundle)
    assert (other.real_scans, other.reused) == (1, 0)


def test_without_scan_once_every_install_is_scanned(world):
    app_manager, bundle, scans = world
    _install(app_manager, bundle)
    _install(app_manager, bundle)
    assert len(scans) == 2


def test_both_copies_are_the_same():
    slack, email = (_block(app) for app in sorted(_FILES, reverse=True))
    assert slack == email, "the ScanOnce copies in slack-channel and email-channel have drifted"


@pytest.mark.parametrize("app", sorted(_FILES))
def test_each_install_fixture_scans_through_its_copy(app):
    # The `home` fixture takes the module-scoped `scans` and calls `scans.use(...)` before it
    # installs, and `scans` returns this file's ScanOnce.
    tree = ast.parse(_FILES[app].read_text(encoding="utf-8"))
    funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "scans" in funcs and "home" in funcs, f"{app}: no scans/home fixture"
    assert "scans" in [a.arg for a in funcs["home"].args.args], f"{app}: home does not take scans"
    body = ast.unparse(funcs["home"])
    use, install = body.find("scans.use(monkeypatch)"), body.find("app_manager.install(")
    assert 0 <= use < install, f"{app}: home installs before (or without) scans.use"
    scope = ast.unparse(funcs["scans"].decorator_list[0])
    assert "scope='module'" in scope, f"{app}: scans is not module-scoped, so nothing is reused"
    assert "return ScanOnce()" in ast.unparse(funcs["scans"]), f"{app}: scans is not a ScanOnce"
