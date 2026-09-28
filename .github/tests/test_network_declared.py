"""The network-declaration rail reds on a planted defect, and the tree is clean.

The rail is ``.github/scripts/check_network_declared.py``, which CI runs as a script. These tests
drive that same script, unmodified, against seeded trees: a copy sits at the same place in each,
so the bundles it walks are exactly the seeded ones.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "check_network_declared.py"

#: The apps the rail's vacuity floor names, each with the least code that shows its signal.
_KNOWN = {
    "tavily-search": "from personalclaw.sdk.net import fetch\n",
    "email-channel": "import smtplib\n",
    "bedrock-models": "import boto3\n",
    "groq-models": "from personalclaw.sdk.model import register_branded_app\n",
    "claude-code-agent": "from personalclaw.sdk.acp import register_acp_cli_entry\n",
    "git-sync": "def push():\n    subprocess.run(git_argv(['push', 'origin']))\n",
    "rsync-sync": "def send(host):\n    subprocess.run(['ssh', host])\n",
    "code-review": "def diff():\n    asyncio.create_subprocess_exec('gh', 'pr', 'diff')\n",
}


def _seeded(tmp_path: Path, apps: dict[str, tuple[dict, dict[str, str]]]) -> Path:
    """A tree holding the rail's script and exactly *apps*: name → (permissions, files)."""
    root = tmp_path / "repo"
    script = root / ".github" / "scripts" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    for name, (permissions, files) in apps.items():
        bundle = root / name
        bundle.mkdir(parents=True)
        manifest = {"name": name, "version": "0.1.0"}
        if permissions:
            manifest["permissions"] = permissions
        (bundle / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
        for rel, source in files.items():
            (bundle / rel).parent.mkdir(parents=True, exist_ok=True)
            (bundle / rel).write_text(source, encoding="utf-8")
    return root


def _known() -> dict[str, tuple[dict, dict[str, str]]]:
    return {name: ({"network": True}, {"provider.py": code}) for name, code in _KNOWN.items()}


def _check(root: Path) -> subprocess.CompletedProcess:
    """Run the script in *root*, as CI runs it."""
    script = root / ".github" / "scripts" / SCRIPT.name
    return subprocess.run([sys.executable, str(script)], cwd=root, capture_output=True, text=True)


def test_every_app_in_the_tree_that_reaches_the_network_declares_it():
    """The real repository through the real script, exactly as CI runs it."""
    run = _check(ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1].startswith("OK: "), run.stdout


def test_a_clean_seeded_tree_is_green(tmp_path):
    """Vacuity. The seeded harness must be able to go green, or every red below proves nothing.
    A test file's imports and ``aiohttp.web`` are not the app reaching the network."""
    apps = _known()
    apps["local-app"] = ({}, {
        "provider.py": "import json\n",
        "backend/server.py": "from aiohttp import web\n",
        "test_provider.py": "import aiohttp\n",
    })
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 0, run.stdout
    assert run.stdout.splitlines() == [
        "OK: 8 app(s) reach the network, and every one declares it"
    ], run.stdout


def test_an_app_that_reaches_the_network_undeclared_reds(tmp_path):
    """The planted defect: an HTTP client in an app whose manifest says nothing about network."""
    apps = _known()
    apps["leaky-app"] = ({}, {"provider.py": "import aiohttp\n"})
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert (
        "leaky-app: its code reaches the network (leaky-app/provider.py:1: imports aiohttp (an "
        'HTTP client)), but leaky-app/app.json does not declare "network": true under permissions'
    ) in run.stdout, run.stdout


def test_an_app_that_declares_network_false_and_reaches_it_reds(tmp_path):
    apps = _known()
    apps["forge-app"] = ({"storage": True, "network": False}, {
        "provider.py": "def issues():\n    subprocess.run(['glab', 'issue', 'list'])\n",
    })
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert "forge-app: its code reaches the network (forge-app/provider.py:2: starts glab)" in (
        run.stdout
    ), run.stdout


def test_a_file_the_rail_cannot_parse_reds(tmp_path):
    """A file that does not parse hides whatever it imports, so it is not read as clean."""
    apps = _known()
    apps["broken-app"] = ({}, {"provider.py": "def (:\n"})
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert "cannot read broken-app/provider.py" in run.stdout, run.stdout


def test_a_known_app_that_stops_showing_its_signal_reds(tmp_path):
    """The vacuity floor: an app named in KNOWN_SIGNALLED must be seen with its signal."""
    apps = _known()
    apps["bedrock-models"] = ({"network": True}, {"provider.py": "import json\n"})
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert "vacuity floor: bedrock-models was not seen with 'imports boto3'" in run.stdout, (
        run.stdout
    )
