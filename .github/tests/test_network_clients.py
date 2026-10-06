"""The network-clients rail reds on an app's own HTTP client, and the tree is clean.

The rail is ``.github/scripts/check_network_clients.py``, which CI runs as a script. These tests
drive that same script, unmodified, against seeded trees: a copy sits at the same place in each,
so the apps it walks are exactly the seeded ones.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "check_network_clients.py"

#: The apps the rail's vacuity floor names, each with the least code that shows the guarded client
#: it is named for.
_KNOWN = {
    "bedrock-models": (
        "import boto3\n\n"
        "from personalclaw.sdk.net import RequestGuard, sync_http_client\n\n\n"
        "def session(profile):\n"
        "    session = boto3.Session(profile_name=profile)\n"
        "    guard = RequestGuard(model_provider=True)\n\n"
        "    def ask(request, **_event):\n"
        "        guard.ask(str(request.url))\n\n"
        "    session.events.register_first(\"before-send\", ask)\n"
        "    return session\n\n\n"
        "def transcript(uri):\n"
        "    with sync_http_client(model_provider=True) as http:\n"
        "        return http.get(uri).json()\n"
    ),
    "google-models": (
        "from personalclaw.sdk.net import http_session\n\n\n"
        "async def models(url):\n"
        "    async with http_session(model_provider=True) as session:\n"
        "        return await session.get(url)\n"
    ),
    "alibaba-models": (
        "from personalclaw.sdk.net import http_session\n\n\n"
        "async def image(url, endpoint):\n"
        "    async with http_session(endpoint=endpoint, model_provider=True) as session:\n"
        "        return await session.post(url)\n"
    ),
    "fal-image": (
        "from personalclaw.sdk.net import http_session\n\n\n"
        "async def video(url):\n"
        "    async with http_session(model_provider=True) as session:\n"
        "        return await session.post(url)\n"
    ),
    "openai-tools": (
        "def connected(url):\n"
        "    from personalclaw.sdk.net import sync_http_client\n\n"
        "    with sync_http_client(timeout=5) as http:\n"
        "        return http.head(url).is_success\n"
    ),
    "skills-sh": (
        "def get(url):\n"
        "    from personalclaw.sdk.net import sync_http_client\n\n"
        "    with sync_http_client(timeout=5) as http:\n"
        "        return http.get(url).json()\n"
    ),
}

_CLEAN = "OK: every HTTP client in the apps asks the egress guard (6 app(s) make one)"


def _seeded(tmp_path: Path, apps: dict[str, dict[str, str]], *, channels: tuple = ()) -> Path:
    """A tree holding the rail's script and exactly *apps*: name → {relative path: source}."""
    root = tmp_path / "repo"
    script = root / ".github" / "scripts" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    for name, files in apps.items():
        bundle = root / name
        bundle.mkdir(parents=True)
        manifest = {"name": name, "version": "0.1.0"}
        if name in channels:
            manifest["provider"] = {"type": "channel"}
        (bundle / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
        for rel, source in files.items():
            (bundle / rel).parent.mkdir(parents=True, exist_ok=True)
            (bundle / rel).write_text(source, encoding="utf-8")
    return root


def _known() -> dict[str, dict[str, str]]:
    return {name: {"provider.py": code} for name, code in _KNOWN.items()}


def _check(root: Path) -> subprocess.CompletedProcess:
    """Run the script in *root*, as CI runs it."""
    script = root / ".github" / "scripts" / SCRIPT.name
    return subprocess.run([sys.executable, str(script)], cwd=root, capture_output=True, text=True)


def test_every_client_in_the_tree_asks_the_guard():
    """The real repository through the real script, exactly as CI runs it."""
    run = _check(ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1].startswith("OK: "), run.stdout


def test_a_clean_seeded_tree_is_green(tmp_path):
    """Vacuity. The seeded harness must be able to go green, or every red below proves nothing.
    A test file's own client is not the app's."""
    apps = _known()
    apps["google-models"]["test_provider.py"] = "import httpx\nhttpx.AsyncClient()\n"
    apps["google-models"]["tests/helpers.py"] = "import aiohttp\naiohttp.ClientSession()\n"
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 0, run.stdout
    assert run.stdout.splitlines() == [_CLEAN], run.stdout


@pytest.mark.parametrize(
    ("source", "what"),
    [
        ("import httpx\n\nclient = httpx.AsyncClient()\n", "httpx.AsyncClient"),
        ("from aiohttp import ClientSession\n\nClientSession()\n", "aiohttp.ClientSession"),
        ("import urllib.request\n\nurllib.request.urlopen('u')\n", "urllib.request.urlopen"),
        ("import openai\n\nopenai.AsyncOpenAI(api_key='k')\n",
         "openai.AsyncOpenAI with no guarded http_client"),
        ("import boto3\n\nboto3.Session().client('s3')\n",
         "an AWS session with no before-send hook that asks the egress guard"),
        ("import boto3\n\nboto3.client('s3')\n", "a client of boto3's default session"),
        ("import botocore.session\n\nbotocore.session.get_session().create_client('s3')\n",
         "an AWS session with no before-send hook that asks the egress guard"),
        ("import openai\nfrom personalclaw.sdk.net import RequestGuard\n"
         "openai.AsyncOpenAI(api_key='k', http_client=RequestGuard())\n",
         "openai.AsyncOpenAI with no guarded http_client"),
    ],
)
def test_an_apps_own_client_reds(tmp_path, source, what):
    apps = _known()
    apps["own-client"] = {"provider.py": source}
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert f"own-client/provider.py:3: {what}" in run.stdout, run.stdout


#: An AWS session whose clients ask the guard, as bedrock-models makes its own (the seed above).
_HOOKED = _KNOWN["bedrock-models"]


@pytest.mark.parametrize(
    ("source", "what"),
    [
        # A client made before the hook is registered asks nothing.
        (
            _HOOKED.replace(
                "    session.events.register_first(",
                "    session.client('s3')\n    session.events.register_first(",
            ),
            "an AWS session that makes a client before its before-send hook is registered",
        ),
        # A hook on another event, on one operation only, or one that asks no guard.
        (_HOOKED.replace('"before-send"', '"after-call"'),
         "an AWS session with no before-send hook that asks the egress guard"),
        (_HOOKED.replace('"before-send"', '"before-send.s3.ListBuckets"'),
         "an AWS session with no before-send hook that asks the egress guard"),
        (_HOOKED.replace("guard.ask(str(request.url))", "print(request.url)"),
         "an AWS session with no before-send hook that asks the egress guard"),
        # No hook at all.
        (_HOOKED.replace('    session.events.register_first("before-send", ask)\n', ""),
         "an AWS session with no before-send hook that asks the egress guard"),
    ],
    ids=["hooked-after-a-client", "another-event", "one-operation", "asks-nothing", "no-hook"],
)
def test_an_aws_session_whose_requests_do_not_all_ask_the_guard_reds(tmp_path, source, what):
    apps = _known()
    apps["own-session"] = {"provider.py": source}
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert f"own-session/provider.py:7: {what}" in run.stdout, run.stdout


def test_a_hooked_aws_session_is_green_whichever_way_its_hook_is_written(tmp_path):
    """The hook as a function, as a lambda, and registered with ``register``: each is the same
    hook, recognised by its registration on ``before-send``, not by a name."""
    apps = _known()
    apps["lambda-hook"] = {
        "provider.py": _HOOKED.replace(
            'session.events.register_first("before-send", ask)',
            'session.events.register("before-send", lambda request, **_: guard.ask(request.url))',
        )
    }
    apps["renamed-hook"] = {
        "provider.py": _HOOKED.replace("def ask(", "def refuse_unless_allowed(").replace(
            '"before-send", ask)', '"before-send", refuse_unless_allowed)'
        )
    }
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 0, run.stdout
    assert run.stdout.splitlines() == [
        "OK: every HTTP client in the apps asks the egress guard (8 app(s) make one)"
    ], run.stdout


def test_a_channel_app_is_outside_the_rail(tmp_path):
    apps = _known()
    apps["chat-channel"] = {"api.py": "import httpx\n\nhttpx.AsyncClient()\n"}
    run = _check(_seeded(tmp_path, apps, channels=("chat-channel",)))
    assert run.returncode == 0, run.stdout


def test_an_app_the_floor_names_that_opens_no_guarded_client_reds(tmp_path):
    apps = _known()
    apps["skills-sh"] = {"provider.py": "def get(url):\n    return None\n"}
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert "vacuity floor: skills-sh was not seen making sync_http_client" in run.stdout
