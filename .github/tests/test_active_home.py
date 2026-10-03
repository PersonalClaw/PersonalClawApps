"""The active-home rail reds on a planted defect, and the tree is clean.

The rail is ``.github/scripts/check_active_home.py``, which CI runs as a script. These tests drive
that same script, unmodified, against seeded trees: a copy sits at the same place in each, so the
bundles it walks are exactly the seeded ones. Nothing seeded is ever run: the rail only reads.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "check_active_home.py"

#: The apps the rail's vacuity floor names, each asking for its home the way it is named for.
_KNOWN = {
    "rsync-sync": (
        {},
        {
            "rsync_sync_transport.py": "from personalclaw.sdk.util import config_dir\n\n\n"
            "def workdir():\n    return config_dir() / 'sync' / 'rsync-sync'\n"
        },
    ),
    "growth": (
        {},
        {
            "backend/server.py": "from personalclaw.sdk.util import app_data_dir\n\n"
            "DATA_DIR = app_data_dir('growth')\n"
        },
    ),
    "browser-connector": (
        {
            "platform": {
                "installMode": "client",
                "clientInstall": {
                    "shell": 'DEST="${PERSONALCLAW_HOME:-$HOME/.personalclaw}/apps/'
                    'browser-connector"'
                },
            }
        },
        {},
    ),
}


def _seeded(tmp_path: Path, apps: dict[str, tuple[dict, dict[str, str]]]) -> Path:
    """A tree holding the rail's script and exactly *apps*: name → (manifest fields, files)."""
    root = tmp_path / "repo"
    script = root / ".github" / "scripts" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    for name, (fields, files) in apps.items():
        bundle = root / name
        bundle.mkdir(parents=True)
        manifest = {"name": name, "version": "0.1.0", **fields}
        (bundle / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
        for rel, source in files.items():
            (bundle / rel).parent.mkdir(parents=True, exist_ok=True)
            (bundle / rel).write_text(source, encoding="utf-8")
    return root


def _check(root: Path) -> subprocess.CompletedProcess:
    """Run the script in *root*, as CI runs it."""
    script = root / ".github" / "scripts" / SCRIPT.name
    return subprocess.run([sys.executable, str(script)], cwd=root, capture_output=True, text=True)


def _red_for(tmp_path: Path, app: str, fields: dict, files: dict[str, str]) -> str:
    """Seed the known apps plus *app*, and return the rail's output, which must be red."""
    apps = dict(_KNOWN)
    apps[app] = (fields, files)
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    return run.stdout


def test_no_app_in_the_tree_works_out_personalclaws_home_itself():
    """The real repository through the real script, exactly as CI runs it."""
    run = _check(ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1].startswith("OK: "), run.stdout


def test_a_clean_seeded_tree_is_green(tmp_path):
    """Vacuity. The seeded harness must be able to go green, or every red below proves nothing.
    A skip-list naming ``.personalclaw`` folders, the sync root's marker file, a docstring that
    names the old path, and a test of the default home are none of them the app's code working
    the home out."""
    apps = dict(_KNOWN)
    apps["tidy-app"] = (
        {"provider": {"settingsSchema": {"properties": {"workdir": {"default": ""}}}}},
        {
            "provider.py": '"""Once kept in ~/.personalclaw/sync/tidy-app."""\n'
            'SKIP = frozenset({".git", ".personalclaw"})\n'
            'MARKER = ".personalclaw-sync-root"\n',
            "test_provider.py": "from pathlib import Path\n"
            'DEFAULT = Path.home() / ".personalclaw"\n',
            "tests/helpers.py": 'HOME = "~/.personalclaw"\n',
        },
    )
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 0, run.stdout
    assert run.stdout.splitlines() == [
        "OK: 3 file(s) and 4 manifest(s) read; no app works out PersonalClaw's home itself, and "
        "3 app(s) ask for it"
    ], run.stdout


@pytest.mark.parametrize(
    ("files", "said"),
    [
        (
            {"provider.py": 'def build(workdir: str = "~/.personalclaw/sync/leaky-app"):\n'
             "    pass\n"},
            "leaky-app: leaky-app/provider.py:1: names a path under ~/.personalclaw",
        ),
        (
            {"provider.py": 'def create(config):\n'
             '    return config.get("workdir", "") or "~/.personalclaw/sync/leaky-app"\n'},
            "leaky-app: leaky-app/provider.py:2: names a path under ~/.personalclaw",
        ),
        (
            {"provider.py": "from pathlib import Path\n\n"
             'ROOT = Path.home() / ".personalclaw" / "leaky-app"\n'},
            "leaky-app: leaky-app/provider.py:3: builds ~/.personalclaw from the account's home",
        ),
        (
            {"provider.py": "import os\n\n"
             'HOME = os.environ.get("PERSONALCLAW_HOME", "")\n'},
            "leaky-app: leaky-app/provider.py:3: reads $PERSONALCLAW_HOME",
        ),
        (
            {"backend/server.py": "import os\nfrom pathlib import Path\n\n"
             'DATA_DIR = Path(os.environ.get("PERSONALCLAW_APP_DATA_DIR", '
             'os.path.expanduser("~/.personalclaw/apps/leaky-app/data")))\n'},
            "leaky-app: leaky-app/backend/server.py:4: names a path under ~/.personalclaw",
        ),
    ],
    ids=["a-parameter-default", "a-factory-fallback", "built-from-path-home", "the-variable-read",
         "a-server-fallback"],
)
def test_code_that_works_the_home_out_itself_reds(tmp_path, files, said):
    """The planted defect, in each shape the apps had it in, and the variable read core refuses
    too."""
    out = _red_for(tmp_path, "leaky-app", {}, files)
    assert said in out, out


def test_a_setting_whose_default_names_a_path_in_the_default_home_reds(tmp_path):
    """The Configure page shows a setting's default as its value and saves it, so a default of
    ``~/.personalclaw/…`` is the account's home whatever home PersonalClaw runs on."""
    fields = {
        "provider": {
            "type": "sync",
            "settingsSchema": {
                "properties": {"workdir": {"type": "string", "default": "~/.personalclaw/sync/x"}}
            },
        }
    }
    out = _red_for(tmp_path, "leaky-app", fields, {})
    assert (
        "leaky-app: leaky-app/app.json provider.settingsSchema.properties.workdir.default: names "
        "a path under ~/.personalclaw"
    ) in out, out


def test_the_shells_resolution_is_for_a_client_install_alone(tmp_path):
    """A client install runs in the owner's shell, which may resolve the home itself. A hook the
    gateway runs is not that shell, and asks nothing of it."""
    fields = {"setup": {"onInstall": 'cp -R seed "${PERSONALCLAW_HOME:-$HOME/.personalclaw}/x"'}}
    out = _red_for(tmp_path, "leaky-app", fields, {})
    assert "leaky-app: leaky-app/app.json setup.onInstall: names a path under ~/.personalclaw" in (
        out
    ), out


def test_a_file_the_rail_cannot_parse_reds(tmp_path):
    """A file that does not parse hides whatever it builds, so it is not read as clean."""
    out = _red_for(tmp_path, "broken-app", {}, {"provider.py": "def (:\n"})
    assert "cannot read broken-app/provider.py" in out, out


@pytest.mark.parametrize(
    ("app", "files", "fields", "said"),
    [
        (
            "rsync-sync",
            {"rsync_sync_transport.py": "WORKDIR = '/srv/work'\n"},
            {},
            "vacuity floor: rsync-sync was not seen asking for its home with 'config_dir'",
        ),
        (
            "browser-connector",
            {},
            {"platform": {"clientInstall": {"shell": "echo installed"}}},
            "vacuity floor: browser-connector was not seen asking for its home with \"the shell's "
            'resolution of the home"',
        ),
    ],
    ids=["the-sdk", "the-shell"],
)
def test_a_known_app_that_stops_asking_reds(tmp_path, app, files, fields, said):
    """The vacuity floor: an app named in KNOWN_ASKS must be seen asking the way it is named
    for, so a reader that stops seeing real code turns the rail red instead of clean."""
    apps = dict(_KNOWN)
    apps[app] = (fields, files)
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert said in run.stdout, run.stdout
