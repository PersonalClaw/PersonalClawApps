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

import pytest

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


def _red_for(tmp_path: Path, app: str, source: str) -> str:
    """Seed the known apps plus *app*, which declares nothing and holds *source*, and return
    the rail's output, which must be red."""
    apps = _known()
    apps[app] = ({}, {"provider.py": source})
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    return run.stdout


def test_an_undeclared_import_by_name_of_a_client_reds(tmp_path):
    """An HTTP client imported by name at run time is the same reach as a static import."""
    source = "import importlib\n\nhttpx = importlib.import_module('httpx')\n"
    out = _red_for(tmp_path, "lazy-app", source)
    assert (
        "lazy-app: its code reaches the network (lazy-app/provider.py:3: imports httpx (an HTTP "
        "client))"
    ) in out, out


def test_a_bare_run_imported_from_subprocess_reds(tmp_path):
    """``run`` is ``subprocess.run`` once ``from subprocess import run`` bound it."""
    source = "from subprocess import run\n\n\ndef send(host):\n    run(['ssh', host])\n"
    out = _red_for(tmp_path, "bare-run-app", source)
    assert (
        "bare-run-app: its code reaches the network (bare-run-app/provider.py:5: starts ssh)"
    ) in out, out


@pytest.mark.parametrize(("source", "signal"), [
    ("import subprocess as sp\n\n\ndef send(src, dst):\n    sp.run(['rsync', src, dst])\n",
     "starts rsync"),
    ("from subprocess import run as r\n\n\ndef push():\n    r(['git', 'push'])\n", "runs git push"),
    ("from asyncio import create_subprocess_exec\n\n\nasync def diff():\n"
     "    await create_subprocess_exec('gh', 'pr', 'diff')\n", "starts gh"),
])
def test_a_spawn_under_another_name_reds(tmp_path, source, signal):
    """A spawn is what the file imported it as, not the last two parts of its name."""
    out = _red_for(tmp_path, "alias-app", source)
    assert f"alias-app: its code reaches the network (alias-app/provider.py:5: {signal})" in (
        out
    ), out


def test_a_shell_command_line_that_reaches_another_machine_reds(tmp_path):
    """A command line for a shell is read by its first word."""
    source = "import os\n\n\ndef uptime(host):\n    os.system(f'ssh {host} uptime')\n"
    out = _red_for(tmp_path, "shell-app", source)
    assert "shell-app: its code reaches the network (shell-app/provider.py:5: starts ssh)" in (
        out
    ), out


def test_a_module_named_at_run_time_reds_unless_the_app_declares_network(tmp_path):
    """A module whose name is only known at run time could be anything, so an app that does not
    declare network has its network use unknown. One that declares it is not made wrong."""
    source = "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"
    apps = _known()
    apps["plugin-app"] = ({}, {"provider.py": source})
    apps["declared-app"] = ({"network": True}, {"provider.py": source})
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert (
        "plugin-app: plugin-app/provider.py:5 imports a module named only at run time, so "
        "whether plugin-app reaches the network is unknown"
    ) in run.stdout, run.stdout
    assert "declared-app" not in run.stdout, run.stdout


@pytest.mark.parametrize(("source", "signal"), [
    ("import subprocess\n\nrun = subprocess.run\n\n\ndef send(host):\n    run(['ssh', host])\n",
     "provider.py:7: starts ssh"),
    ("import subprocess\n\n\nclass Syncer:\n    def __init__(self):\n"
     "        self._run = subprocess.run\n\n    def send(self, src, dst):\n"
     "        self._run(['rsync', src, dst])\n", "provider.py:9: starts rsync"),
])
def test_a_spawn_bound_to_a_name_reds(tmp_path, source, signal):
    """``run = subprocess.run`` makes a call through ``run`` a spawn, at any scope."""
    out = _red_for(tmp_path, "bound-app", source)
    assert f"bound-app: its code reaches the network (bound-app/{signal})" in out, out


def test_a_spawn_handed_on_as_a_callable_reds(tmp_path):
    """A spawn run through ``run_in_executor`` is never called by its own name."""
    source = (
        "import asyncio\nimport subprocess\n\n\nasync def send(host):\n"
        "    loop = asyncio.get_running_loop()\n"
        "    await loop.run_in_executor(None, subprocess.run, ['ssh', host])\n"
    )
    out = _red_for(tmp_path, "executor-app", source)
    assert (
        "executor-app: its code reaches the network (executor-app/provider.py:7: starts ssh)"
    ) in out, out


@pytest.mark.parametrize(("line", "signal"), [
    ("'cd repo && git push'", "runs git push"),
    ("\"sh -c 'git fetch origin'\"", "runs git fetch"),
    ("'sudo -u deploy rsync -a src host:dst'", "starts rsync"),
    ("\"sh -c 'cd repo\\ngit push'\"", "runs git push"),
])
def test_every_command_of_a_shell_line_is_read(tmp_path, line, signal):
    """The remote command is not the first word of the line."""
    source = f"import subprocess\n\n\ndef publish():\n    subprocess.run({line}, shell=True)\n"
    out = _red_for(tmp_path, "shell-line-app", source)
    said = f"shell-line-app: its code reaches the network (shell-line-app/provider.py:5: {signal})"
    assert said in out, out


@pytest.mark.parametrize(("load", "said"), [
    ("importlib.util.find_spec('httpx')",
     "loader-app: its code reaches the network (loader-app/provider.py:5: imports httpx (an HTTP "
     "client))"),
    ("importlib.util.spec_from_file_location('plugin', path)",
     "loader-app: loader-app/provider.py:5 loads code from a path only known at run time, so "
     "whether loader-app reaches the network is unknown"),
])
def test_code_loaded_through_importlib_util_is_read(tmp_path, load, said):
    """A spec that is executed runs its module: a named one is judged like an import, and one
    from a path only known at run time leaves the app's network use unknown."""
    source = (
        f"import importlib.util\n\n\ndef load(path):\n    spec = {load}\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(module)\n    return module\n"
    )
    out = _red_for(tmp_path, "loader-app", source)
    assert said in out, out
