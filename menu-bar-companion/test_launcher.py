"""The launcher: the command a user actually types, and the manifest that types it.

``run.py`` is this app's entire entry surface, and one of its flags is named by
``app.json`` itself — ``platform.clientInstall.postInstall`` runs ``run.py --check`` as
the first thing that happens on a fresh Mac. That makes the manifest and the launcher two
sides of ONE contract, and until this file existed nothing joined them: ``test_manifest``
asserted ``"--check" in ci.postInstall`` while ``main()`` was never called at all, so
renaming the flag in ``run.py`` (or moving the bundle directory) left both suites green and
every new install printing usage instead of verifying the token.

So the rails here are deliberately two-sided. The install path is DERIVED from the
``clientInstall.shell`` one-liner (its ``DEST`` plus its sparse-checkout directory) and
then required to be the path ``postInstall`` invokes — drift on either side fails. The
flag is taken from ``postInstall`` and required to actually dispatch, and the vacuity floor
is a flag that does NOT (it must fall through to the live path instead).

ENVIRONMENT LIMIT — not closeable here, or anywhere automated: a real ``NSStatusItem``
drawn by real PyObjC on a Mac with a window server. Nothing in this file (or in
``test_status_item.py``, which drives the same code against a stubbed ``rumps``) proves a
menu-bar item appears, that its title fits the bar, or that its menu opens on click. What
is proven is everything below the toolkit: which function each command dispatches to, the
exit code the installer sees, the credentials file's mode, and the rendered text.
``_run_headless``'s print-on-change loop is likewise unasserted: it blocks forever by
design, and its body is one call to ``headless_render``, which ``test_menu`` pins.
"""

from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
from pathlib import Path

import pytest
from _ws_fakes import FakeOpener
from menubar_companion.settings import Settings

APP_DIR = Path(__file__).resolve().parent
RAW = json.loads((APP_DIR / "app.json").read_text(encoding="utf-8"))
CLIENT_INSTALL = RAW["platform"]["clientInstall"]

APPROVALS = [{"id": "a1", "tool": "bash"}]
LOOPS = {"loops": [{"id": "L2", "name": "which db?", "status": "needs_input"}]}


def _healthy_opener() -> FakeOpener:
    return FakeOpener(
        {
            "/api/approvals": json.dumps(APPROVALS).encode(),
            "/api/loops": json.dumps(LOOPS).encode(),
        }
    )


def _unreachable_opener() -> FakeOpener:
    refused = urllib.error.URLError(OSError("Connection refused"))
    return FakeOpener({"/api/loops": refused, "/api/approvals": refused})


def _inject_opener(monkeypatch, launcher, opener) -> None:
    """Make ``_check``'s own ``build_companion(settings)`` call use a fake transport.

    ``_check`` builds the app itself (that is the thing under test), so the seam is the
    module-level name it calls through — not a re-implementation of it here.
    """
    real = launcher.build_companion
    monkeypatch.setattr(
        launcher,
        "build_companion",
        lambda cfg: real(cfg, opener=opener, runner=lambda _argv: None),
    )


def _configure_home() -> Settings:
    settings = Settings(url="http://127.0.0.1:10000", token="tok")
    settings.save()
    return settings


@pytest.fixture(autouse=True)
def _live_path_fails_instead_of_blocking(monkeypatch):
    """Make an unexpected fall-through to the live path FAIL rather than BLOCK.

    ``_run_headless`` is a ``while True`` print loop and ``_run_status_item`` enters the
    host's run loop; neither returns. Without this fixture a dispatch regression would
    HANG the suite instead of failing it, which is strictly worse — CI reports a timeout
    with no failing test name, and the operator has to bisect to learn what broke.

    This is not hypothetical: renaming the ``--check`` literal in ``main()`` (the exact
    mutation this file's dispatch rail exists to catch) blocked the whole bundle run in
    ``_run_headless`` until it was killed. The rail caught the regression; the suite just
    could not say so. Tests that legitimately drive the live path re-patch these names.
    """
    import run as launcher

    def _blocked(*_args, **_kwargs):
        raise AssertionError(
            "reached run.py's live path — a command that should have been handled "
            "headlessly fell through to the status-item/headless loop"
        )

    monkeypatch.setattr(launcher, "_run_headless", _blocked)
    monkeypatch.setattr(launcher, "_run_status_item", _blocked)


# ── the manifest and the launcher are one contract ──


def _postinstall_flags() -> list[str]:
    return [tok for tok in CLIENT_INSTALL["postInstall"].split() if tok.startswith("--")]


def test_every_flag_the_manifest_runs_on_install_is_a_flag_the_launcher_dispatches(
    monkeypatch,
):
    """``postInstall`` names a flag; ``main()`` must route it to the verification path."""
    import run as launcher

    flags = _postinstall_flags()
    # VACUITY: a regex that matched nothing would make the loop below run zero times and
    # pass without testing anything.
    assert flags, f"no flags found in postInstall: {CLIENT_INSTALL['postInstall']!r}"

    for flag in flags:
        _configure_home()  # configured, so a fall-through would reach the LIVE path
        called: list[str] = []
        monkeypatch.setattr(launcher, "_check", lambda: called.append("check") or 0)
        monkeypatch.setattr(
            launcher, "_run_status_item", lambda *_a: called.append("live") or 0
        )
        monkeypatch.setattr(launcher, "_run_headless", lambda *_a: called.append("live") or 0)

        assert launcher.main([flag]) == 0
        assert called == ["check"], f"{flag} did not dispatch to the verification path"


def test_vacuity_floor_an_unrecognised_flag_falls_through_to_the_live_path(monkeypatch):
    """Prove the dispatch rail above discriminates.

    The same harness, with a flag ``main()`` does not know, must reach the live status-item
    path instead. Without this half, a ``main()`` that ignored its arguments entirely and
    always ran ``_check`` would satisfy the test above.
    """
    import run as launcher

    _configure_home()
    called: list[str] = []
    monkeypatch.setattr(launcher, "_check", lambda: called.append("check") or 0)
    monkeypatch.setattr(launcher, "_run_status_item", lambda *_a: called.append("live") or 0)
    monkeypatch.setattr(launcher, "_run_headless", lambda *_a: called.append("live") or 0)
    monkeypatch.setattr(launcher, "build_companion", lambda _cfg: object())

    assert launcher.main(["--not-a-real-flag"]) == 0
    assert called == ["live"], "an unknown flag must not be treated as --check"


#: ``[^\s;"]+`` and not ``\S+``: the shell chains with ``;``, so a greedy match captures
#: ``menu-bar-companion;`` and the derived path never matches anything. Found by this
#: rail failing on the shipped manifest, which is the behaviour a computed floor should
#: have — it reads the shell rather than restating it.
_SPARSE_RE = re.compile(r'sparse-checkout set ([^\s;"]+)')
_DEST_RE = re.compile(r'DEST="([^"]+)"')


def _script_path_from(shell: str) -> str:
    """Where *shell* actually puts ``run.py``, read out of the one-liner itself."""
    dest, sparse = _DEST_RE.search(shell), _SPARSE_RE.search(shell)
    assert dest and sparse, f"cannot read DEST/sparse-checkout out of {shell!r}"
    return f"{dest.group(1)}/{sparse.group(1)}/run.py"


def test_the_install_one_liner_and_the_postinstall_command_agree_on_where_run_py_lands():
    """Both sides of the install, derived rather than restated.

    ``postInstall`` hard-codes an absolute path. If the clone destination or the
    sparse-checkout directory ever changes, that path silently stops existing and the
    first thing a new user sees is a Python file-not-found.
    """
    derived = _script_path_from(CLIENT_INSTALL["shell"])
    assert derived in CLIENT_INSTALL["postInstall"], (
        f"postInstall does not invoke the script the shell installs.\n"
        f"  shell puts it at: {derived}\n  postInstall runs:  {CLIENT_INSTALL['postInstall']}"
    )
    # …and the sparse-checkout directory is THIS bundle, which really does hold run.py.
    sparse_dir = _SPARSE_RE.search(CLIENT_INSTALL["shell"]).group(1)
    assert sparse_dir == APP_DIR.name, "the shell checks out a directory that is not this app"
    assert (APP_DIR / "run.py").is_file()


def test_vacuity_floor_the_install_path_rail_notices_a_moved_bundle():
    """Prove the derivation above can fail: move the bundle, break the agreement."""
    moved = CLIENT_INSTALL["shell"].replace(APP_DIR.name, "somewhere-else")
    assert moved != CLIENT_INSTALL["shell"], "the mutation did not apply"
    assert _script_path_from(moved) not in CLIENT_INSTALL["postInstall"]
    # And the sparse directory the mutation produced is no longer this bundle.
    assert _SPARSE_RE.search(moved).group(1) != APP_DIR.name


# ── the exit code the installer actually sees ──


def test_check_exits_2_and_says_how_to_configure_when_there_are_no_credentials(capsys):
    """A fresh install with no token must explain itself, not traceback."""
    import run as launcher

    assert launcher.main(["--check"]) == 2
    err = capsys.readouterr().err
    assert "not configured" in err
    assert "--configure" in err, "the message names the command that fixes it"


def test_check_exits_1_and_shows_the_reason_when_the_gateway_is_unreachable(
    monkeypatch, capsys
):
    """A wrong URL or a stopped gateway is a FAILED check, and the reason is printed."""
    import run as launcher

    _configure_home()
    _inject_opener(monkeypatch, launcher, _unreachable_opener())

    assert launcher.main(["--check"]) == 1, "an unreachable gateway is not a passing check"
    out = capsys.readouterr().out
    assert "Connection refused" in out, out
    assert "PersonalClaw • badge 0" in out


def test_check_exits_0_and_prints_the_menu_it_would_show(monkeypatch, capsys):
    """The success half — and the vacuity floor for the two failures above.

    Same command, same code path, reachable gateway: exit 0 and the real derived badge.
    """
    import run as launcher

    _configure_home()
    _inject_opener(monkeypatch, launcher, _healthy_opener())

    assert launcher.main(["--check"]) == 0
    out = capsys.readouterr().out
    # 1 pending approval + 1 needs-input loop — the same arithmetic the badge uses.
    assert "PersonalClaw • badge 2" in out
    assert "Approvals waiting: 1" in out
    assert "Needs your input: 1" in out
    # It also reports whether this machine can draw the item, rather than assuming.
    assert "status-item host:" in out


def test_a_bare_launch_without_credentials_checks_instead_of_opening_a_menu_bar_item(
    monkeypatch,
):
    """An unconfigured double-click must not reach the GUI at all.

    ``resolve_host`` is the first thing the live path touches; asserting it is never
    called is what proves the short-circuit, rather than inferring it from the exit code
    (which ``_check`` and a failed launch could share).
    """
    import run as launcher

    touched: list[str] = []
    monkeypatch.setattr(launcher, "resolve_host", lambda: touched.append("host") or (None, "x"))

    assert launcher.main([]) == 2
    assert touched == [], "an unconfigured launch reached the status-item host"


# ── --configure ──


def test_configure_persists_credentials_0600_and_normalises_the_url():
    import run as launcher

    assert launcher.main(["--configure", "http://127.0.0.1:10000/", "tok-xyz"]) == 0

    loaded = Settings.load()
    assert loaded.url == "http://127.0.0.1:10000", "the trailing slash is stripped once, here"
    assert loaded.token == "tok-xyz"
    mode = stat.S_IMODE(os.stat(loaded.path).st_mode)
    assert mode == 0o600, oct(mode)


def test_configure_does_not_silently_unmute_a_user_who_had_muted(monkeypatch):
    """Credentials and preferences share one file; writing one must not reset the other."""
    Settings(url="http://old:1", token="old").save()
    Settings.load().set_muted(True)
    assert Settings.load().notifications_muted is True

    import run as launcher

    assert launcher.main(["--configure", "http://new:2", "new-token"]) == 0

    after = Settings.load()
    assert after.token == "new-token"
    assert after.notifications_muted is True, "configuring a token turned notifications back on"


def test_configure_without_enough_arguments_explains_the_usage(capsys):
    import run as launcher

    assert launcher.main(["--configure"]) == 2
    assert "usage: run.py --configure" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--configure", "http://h:1", "t"], ["--check"]])
def test_no_command_writes_outside_the_isolated_companion_home(argv, _isolate_companion_home):
    """Every path this file drives stays inside the tmp home the fixture pinned."""
    import run as launcher

    launcher.main(argv)
    written = list(Path(_isolate_companion_home).rglob("*"))
    assert all(str(p).startswith(str(_isolate_companion_home)) for p in written)
