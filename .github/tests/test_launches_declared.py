"""The launch-declaration rail reds on a planted defect, and the tree is clean.

The rail is ``.github/scripts/check_launches_declared.py``, which CI runs as a script; it reads
spawns with the network rail's own reader, so each seeded tree holds a copy of both scripts at the
same place, and the bundles the rail walks are exactly the seeded ones.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github" / "scripts"
SCRIPT = SCRIPTS / "check_launches_declared.py"
READER = SCRIPTS / "check_network_declared.py"


def _launch(program: str, **extra) -> dict:
    return {"program": program, "why": "It is what the app uses.", **extra}


#: The apps the rail's vacuity floor and its hand-read spawns name, each with the least code that
#: shows the program it starts, and the launches that declare it.
_KNOWN: dict[str, tuple[list[dict], dict[str, str]]] = {
    "menu-bar-companion": (
        [_launch("open"), _launch("osascript")],
        {
            "run.py": (
                "import subprocess\n\n\ndef _open_url(url):\n"
                "    subprocess.run(['/usr/bin/open', url])\n"
            ),
            "menubar_companion/notify.py": (
                "import shutil\nimport subprocess\n\nOSASCRIPT = shutil.which('osascript')\n\n\n"
                "def _default_runner(argv):\n    subprocess.run(list(argv))\n"
            ),
        },
    ),
    "code-review": (
        [_launch("gh")],
        {
            "provider.py": (
                "import asyncio\n\n\nasync def _gh_diff(number):\n"
                "    argv = ['gh', 'pr', 'diff', number]\n"
                "    await asyncio.create_subprocess_exec(*argv)\n"
            ),
        },
    ),
    "diarization-onnx": (
        [_launch("ffmpeg")],
        {
            "provider.py": (
                "import subprocess\n\nfrom personalclaw.sdk.diarization import find_ffmpeg\n\n\n"
                "def _decode(path):\n    ffmpeg = find_ffmpeg()\n"
                "    subprocess.run([ffmpeg, '-i', path])\n"
            ),
        },
    ),
    "git-repo": (
        [_launch("git")],
        {
            "provider.py": (
                "import subprocess\n\nfrom personalclaw.sdk.git import git_argv\n\n\n"
                "def _git(repo, *args):\n    subprocess.run(git_argv(['-C', repo, *args]))\n"
            ),
        },
    ),
    "skills-sh": (
        [_launch("npx", npmPackage="skills")],
        {
            "provider.py": (
                "import shutil\nimport subprocess\n\n\ndef search(query):\n"
                "    npx = shutil.which('npx')\n"
                "    subprocess.run([npx, '-y', 'skills', 'find', query])\n"
            ),
        },
    ),
    "lima-sandbox": (
        [_launch("limactl")],
        {
            "provider.py": (
                "import asyncio\nimport subprocess\n\n_LIMACTL = 'limactl'\n\n\n"
                "class LimaSandboxHandle:\n    async def exec(self, **kwargs):\n"
                "        argv = self._exec_argv(kwargs.pop('cwd', None))\n"
                "        return await asyncio.create_subprocess_exec(*argv, **kwargs)\n\n\n"
                "class LimaSandboxProvider:\n    def _run_limactl(self, args):\n"
                "        return subprocess.run([_LIMACTL, *args])\n"
            ),
        },
    ),
    "rsync-sync": (
        [_launch("rsync")],
        {
            "rsync_sync_transport.py": (
                "import subprocess\n\n\nclass RsyncSyncProvider:\n"
                "    def __init__(self, rsync_bin='rsync'):\n"
                "        self._rsync = rsync_bin or 'rsync'\n\n"
                "    def _run(self, args):\n"
                "        return subprocess.run([self._rsync, *args])\n\n\n"
                "def create_provider():\n    return RsyncSyncProvider()\n"
            ),
        },
    ),
    "issue-radar": (
        [_launch("gh"), _launch("glab")],
        {
            "provider.py": (
                "import asyncio\n\n\nclass IssueRadarProvider:\n"
                "    async def _fetch(self, github):\n        if github:\n"
                "            argv = ['gh', 'issue', 'list']\n        else:\n"
                "            argv = ['glab', 'issue', 'list']\n"
                "        return await self._run_json(argv)\n\n"
                "    async def _run_json(self, argv):\n"
                "        return await asyncio.create_subprocess_exec(*argv)\n"
            ),
        },
    ),
    "ops": (
        [_launch("*")],
        {
            "runbooks.py": (
                "import subprocess\n\n\ndef run_action(action, *, timeout):\n"
                "    return subprocess.run(list(action.argv), timeout=timeout)\n"
            ),
        },
    ),
    "piper-tts": (
        [_launch("piper")],
        {
            "provider.py": (
                "import asyncio\nimport shutil\n\n"
                "from personalclaw.sdk.util import sandbox_wrap_argv\n\n\n"
                "async def _synthesize_piper_chunk(text):\n    command = [shutil.which('piper')]\n"
                "    cmd, cleanup = sandbox_wrap_argv(command, mode='standard')\n"
                "    return await asyncio.create_subprocess_exec(*cmd)\n"
            ),
        },
    ),
}


def _seeded(tmp_path: Path, apps: dict[str, tuple[list[dict] | None, dict[str, str]]]) -> Path:
    """A tree holding the rail's scripts and exactly *apps*: name → (launches, files)."""
    root = tmp_path / "repo"
    scripts = root / ".github" / "scripts"
    scripts.mkdir(parents=True)
    for script in (SCRIPT, READER):
        shutil.copy2(script, scripts / script.name)
    for name, (launches, files) in apps.items():
        bundle = root / name
        bundle.mkdir(parents=True)
        manifest: dict = {"name": name, "version": "0.1.0"}
        if launches is not None:
            manifest["launches"] = launches
        (bundle / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
        for rel, source in files.items():
            (bundle / rel).parent.mkdir(parents=True, exist_ok=True)
            (bundle / rel).write_text(source, encoding="utf-8")
    return root


def _known() -> dict[str, tuple[list[dict] | None, dict[str, str]]]:
    return {name: (list(launches), dict(files)) for name, (launches, files) in _KNOWN.items()}


def _check(root: Path) -> subprocess.CompletedProcess:
    """Run the script in *root*, as CI runs it."""
    script = root / ".github" / "scripts" / SCRIPT.name
    return subprocess.run([sys.executable, str(script)], cwd=root, capture_output=True, text=True)


def _red_for(tmp_path: Path, app: str, source: str, launches: list[dict] | None = None) -> str:
    """Seed the known apps plus *app*, holding *source* and declaring *launches*, and return the
    rail's output, which must be red."""
    apps = _known()
    apps[app] = (launches, {"provider.py": source})
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout + run.stderr
    return run.stdout


def test_every_program_an_app_in_the_tree_starts_is_declared():
    """The real repository through the real script, exactly as CI runs it."""
    run = _check(ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1].startswith("OK: "), run.stdout


def test_a_clean_seeded_tree_is_green(tmp_path):
    """Vacuity. The seeded harness must be able to go green, or every red below proves nothing. A
    test file's spawns, and an app that starts nothing, are no launch to declare."""
    apps = _known()
    apps["quiet-app"] = (
        None,
        {
            "provider.py": "import json\n",
            "test_provider.py": "import subprocess\nsubprocess.run(['ffmpeg', '-version'])\n",
        },
    )
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines() == [
        "OK: 10 app(s) start programs from 12 spawn site(s), and every program is declared"
    ], run.stdout


def test_an_app_that_starts_a_program_it_does_not_declare_reds(tmp_path):
    """The positive control: a fixture app whose code starts ffmpeg, with no launches at all."""
    source = (
        "import subprocess\n\n\ndef decode(path):\n    subprocess.run(['ffmpeg', '-i', path])\n"
    )
    out = _red_for(tmp_path, "undeclared-app", source)
    assert (
        "undeclared-app: undeclared-app/provider.py:5 starts the ffmpeg program, which "
        "undeclared-app/app.json does not declare under launches"
    ) in out, out


def test_declaring_another_program_does_not_cover_it(tmp_path):
    source = (
        "import subprocess\n\n\ndef decode(path):\n    subprocess.run(['ffmpeg', '-i', path])\n"
    )
    out = _red_for(tmp_path, "wrong-app", source, [_launch("ffprobe")])
    assert "wrong-app/provider.py:5 starts the ffmpeg program" in out, out


def test_a_program_you_name_covers_no_program_the_app_names(tmp_path):
    """``*`` is the programs the owner chooses; a program the app's own code names is not one."""
    source = "import subprocess\n\n\ndef diff():\n    subprocess.run(['gh', 'pr', 'diff'])\n"
    out = _red_for(tmp_path, "starred-app", source, [_launch("*")])
    assert "starred-app/provider.py:5 starts the gh program" in out, out


@pytest.mark.parametrize(
    ("source", "said"),
    [
        (
            "import subprocess\n\n\ndef find(q):\n"
            "    subprocess.run(['npx', '-y', 'other-finder', q])\n",
            "runs npx other-finder, but its launches entry's npmPackage is 'skills'",
        ),
        (
            "import subprocess\n\n\ndef find(q):\n"
            "    subprocess.run(['npx', '-y', 'skills@1.0.0', q])\n",
            "runs npx skills@1.0.0, but its launches entry's npmPackage is 'skills'",
        ),
        (
            "import subprocess\n\n\ndef find(pkg, q):\n    subprocess.run(['npx', '-y', pkg, q])\n",
            "runs npx, and the rail cannot read the package it fetches",
        ),
    ],
    ids=["another package", "a pinned version", "a package read at run time"],
)
def test_npx_runs_exactly_the_package_its_entry_names(tmp_path, source, said):
    """The entry's consent says npx fetches the newest version of that package, by name."""
    out = _red_for(tmp_path, "npx-app", source, [_launch("npx", npmPackage="skills")])
    assert f"npx-app: npx-app/provider.py:5 {said}" in out, out


def test_a_spawn_the_rail_cannot_read_reds(tmp_path):
    """A program handed in at run time could be anything, so it is not read as covered."""
    source = "import subprocess\n\n\ndef run(action):\n    subprocess.run(list(action.argv))\n"
    out = _red_for(tmp_path, "opaque-app", source, [_launch("*")])
    assert (
        "opaque-app: opaque-app/provider.py:5 starts a program the rail cannot read "
        "(opaque-app/provider.py::run::subprocess.run)"
    ) in out, out


@pytest.mark.parametrize(
    ("source", "program"),
    [
        ("import os\n\n\ndef build():\n    os.system('make all && scp out host:')\n", "scp"),
        ("import os\n\n\ndef edit(path):\n    os.execvp('vim', ['vim', path])\n", "vim"),
        (
            "import shutil\nimport subprocess\n\n\ndef clone(url):\n    git = shutil.which('git')\n"
            "    subprocess.run([git, 'clone', url])\n",
            "git",
        ),
        (
            "import asyncio\nimport subprocess\n\n\nasync def send(h):\n"
            "    await asyncio.get_running_loop().run_in_executor("
            "None, subprocess.run, ['ssh', h])\n",
            "ssh",
        ),
        (
            "from subprocess import Popen as start\n\n\ndef play(path):\n"
            "    start(['afplay', path])\n",
            "afplay",
        ),
    ],
    ids=["a shell line", "os.execvp", "shutil.which then run", "a spawn handed on", "an alias"],
)
def test_every_way_of_starting_a_program_is_read(tmp_path, source, program):
    out = _red_for(tmp_path, "shapes-app", source)
    assert f"starts the {program} program, which shapes-app/app.json does not declare" in out, out


def test_a_hand_read_spawn_that_is_gone_or_now_readable_reds(tmp_path):
    apps = _known()
    apps["ops"] = (
        [_launch("*")],
        {"runbooks.py": "def run_action(action, *, timeout):\n    pass\n"},
    )
    piper_launches, _files = apps["piper-tts"]
    apps["piper-tts"] = (
        piper_launches,
        {
            "provider.py": (
                "import asyncio\n\n\nasync def _synthesize_piper_chunk(text):\n"
                "    return await asyncio.create_subprocess_exec('piper', '-f', 'out.wav')\n"
            ),
        },
    )
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert (
        "ops/runbooks.py::run_action::subprocess.run is read by hand, but no such spawn exists now"
    ) in run.stdout, run.stdout
    assert (
        "piper-tts/provider.py::_synthesize_piper_chunk::asyncio.create_subprocess_exec is read by "
        "hand, but the rail reads it now"
    ) in run.stdout, run.stdout


def test_a_hand_entry_names_a_program_its_file_names(tmp_path):
    apps = _known()
    launches, files = apps["piper-tts"]
    apps["piper-tts"] = (
        launches,
        {"provider.py": files["provider.py"].replace("'piper'", "'espeak'")},
    )
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert (
        "is read by hand as starting piper, which that file never names" in run.stdout
    ), run.stdout


def test_a_known_app_that_stops_starting_its_program_reds(tmp_path):
    """The vacuity floor: an app named in KNOWN_LAUNCHES must be seen starting its program."""
    apps = _known()
    apps["diarization-onnx"] = ([_launch("ffmpeg")], {"provider.py": "import json\n"})
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert (
        "vacuity floor: diarization-onnx was not seen starting 'ffmpeg'" in run.stdout
    ), run.stdout


def test_a_file_the_rail_cannot_parse_reds(tmp_path):
    """A file that does not parse hides whatever it starts, so it is not read as clean."""
    out = _red_for(tmp_path, "broken-app", "def (:\n")
    assert "cannot read broken-app/provider.py" in out, out
