"""The egress-policy rail reds on a planted defect, and the tree is clean.

The rail is ``.github/scripts/check_egress_policy.py``, which CI runs as a script. These tests
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
SCRIPT = ROOT / ".github" / "scripts" / "check_egress_policy.py"

_LAYERED = (
    "from personalclaw.sdk.net import CONNECTOR, egress_policy_for, fetch\n\n\n"
    "async def search(url):\n"
    "    return await fetch(url, policy=egress_policy_for(CONNECTOR))\n"
)

#: The apps the rail's vacuity floor and PASS_THROUGH name, each with the least code that shows
#: what the rail reads there.
_KNOWN = {
    "searxng-search": _LAYERED,
    "brave-search": _LAYERED,
    "tavily-search": _LAYERED,
    "webhook-action": (
        "from personalclaw.sdk.net import WEBHOOK, egress_policy_for, fetch\n\n\n"
        "async def send(url, timeout):\n"
        "    policy = egress_policy_for(WEBHOOK).with_overrides(timeout_s=float(timeout))\n"
        "    return await fetch(url, policy=policy, method='POST')\n"
    ),
    "s3-sync": (
        "from personalclaw.sdk.net import fetch\n"
        "from personalclaw.sdk.sync import sync_egress_policy\n\n\n"
        "class S3SyncProvider:\n"
        "    def _policy(self):\n"
        "        return sync_egress_policy(self._endpoint)\n\n"
        "    def _request(self, url):\n"
        "        policy = self._policy()\n"
        "        return fetch(url, policy=policy)\n"
    ),
    "a2a-action": (
        "from personalclaw.sdk.net import a2a_outbound_policy\n"
        "from personalclaw.sdk.net import fetch as net_fetch\n\n\n"
        "async def execute(url):\n"
        "    policy = a2a_outbound_policy()\n"
        "    return await net_fetch(url, policy=policy, method='POST')\n"
    ),
    "git-repo": (
        "from personalclaw.sdk.net import CONNECTOR, egress_policy_for, fetch\n\n\n"
        "class GitRepoSourceProvider:\n"
        "    async def _fetch_json(self, url, *, policy, headers):\n"
        "        resolved = policy if policy is not None else egress_policy_for(CONNECTOR)\n"
        "        return await fetch(url, policy=resolved, headers=headers)\n"
    ),
    "openrouter-models": (
        "from personalclaw.sdk.net import CONNECTOR, egress_policy_for, fetch\n\n\n"
        "def _json_policy():\n"
        "    return egress_policy_for(CONNECTOR)\n\n\n"
        "async def _request_json(url, *, policy=None):\n"
        "    return await fetch(url, policy=policy if policy is not None else _json_policy())\n"
    ),
}

_CLEAN = (
    "OK: 8 request site(s) through the SDK's egress guard, every one under the owner's network "
    "settings (2 passed through from a caller)"
)


def _seeded(tmp_path: Path, apps: dict[str, dict[str, str]]) -> Path:
    """A tree holding the rail's script and exactly *apps*: name → {relative path: source}."""
    root = tmp_path / "repo"
    script = root / ".github" / "scripts" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    for name, files in apps.items():
        bundle = root / name
        bundle.mkdir(parents=True)
        (bundle / "app.json").write_text(
            json.dumps({"name": name, "version": "0.1.0"}), encoding="utf-8"
        )
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


def _with(tmp_path: Path, app: str, source: str) -> subprocess.CompletedProcess:
    """Seed the known apps plus *app*, whose provider is *source*, and run the rail."""
    apps = _known()
    apps[app] = {"provider.py": source}
    return _check(_seeded(tmp_path, apps))


def test_every_request_in_the_tree_honours_the_owners_network_settings():
    """The real repository through the real script, exactly as CI runs it."""
    run = _check(ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1].startswith("OK: "), run.stdout


def test_a_clean_seeded_tree_is_green(tmp_path):
    """Vacuity. The seeded harness must be able to go green, or every red below proves nothing.
    A test file's own requests are not the app's."""
    apps = _known()
    apps["searxng-search"]["test_provider.py"] = (
        "from personalclaw.sdk.net import CONNECTOR, fetch\n\n\n"
        "async def probe(url):\n    return await fetch(url, policy=CONNECTOR)\n"
    )
    apps["searxng-search"]["tests/helpers.py"] = "from personalclaw.sdk.net import fetch\n"
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 0, run.stdout
    assert run.stdout.splitlines() == [_CLEAN], run.stdout


def test_a_request_under_the_bare_profile_reds(tmp_path):
    """The planted defect: a request under CONNECTOR itself, which the owner's settings never
    reach."""
    source = (
        "from personalclaw.sdk.net import CONNECTOR, fetch\n\n\n"
        "class Provider:\n"
        "    async def search(self, url):\n"
        "        return await fetch(url, policy=CONNECTOR, method='GET')\n"
    )
    run = _with(tmp_path, "bare-search", source)
    assert run.returncode == 1, run.stdout
    assert (
        "bare-search/provider.py:6 makes its request under the bare CONNECTOR profile, which "
        "leaves out the owner's Settings → Security → Network egress: pass "
        "egress_policy_for(<profile>) (bare-search/provider.py::Provider.search::fetch)"
    ) in run.stdout, run.stdout


@pytest.mark.parametrize(("body", "what"), [
    ("    return await fetch(url)\n", "no policy, so the guard's default"),
    ("    return await fetch(url, policy=EgressPolicy(name='mine'))\n",
     "a policy the app builds itself"),
    ("    return evaluate(url, CONNECTOR)\n", "the bare CONNECTOR profile"),
    ("    return await net.fetch(url, policy=net.WEBHOOK)\n", "the bare WEBHOOK profile"),
    ("    return await fetch(url, policy=egress_policy_for(CONNECTOR).with_overrides("
     "deny_hosts=()))\n", "a policy with the owner's settings overridden (deny_hosts)"),
    ("    return await fetch(url, policy=replace(egress_policy_for(CONNECTOR), "
     "allow_hosts=()))\n", "a policy with the owner's settings overridden (allow_hosts)"),
    ("    return await fetch(url, policy=CONNECTOR if url else egress_policy_for(CONNECTOR))\n",
     "the bare CONNECTOR profile"),
])
def test_a_request_the_owners_settings_do_not_reach_reds(tmp_path, body, what):
    """Every other shape of a policy the owner's settings are not in."""
    source = (
        "from dataclasses import replace\n\n"
        "from personalclaw.sdk import net\n"
        "from personalclaw.sdk.net import CONNECTOR, EgressPolicy, egress_policy_for, evaluate, "
        "fetch\n\n\n"
        "async def search(url):\n" + body
    )
    run = _with(tmp_path, "shape-app", source)
    assert run.returncode == 1, run.stdout
    assert f"shape-app/provider.py:8 makes its request under {what}," in run.stdout, run.stdout


def test_a_policy_layered_anywhere_in_the_file_is_read_as_layered(tmp_path):
    """A layered policy is found through a function or method of the same file, a name bound to
    one, a narrowing copy of one and the SDK module by name, so none of those needs a comment."""
    source = (
        "from dataclasses import replace\n\n"
        "from personalclaw.sdk import net\n"
        "from personalclaw.sdk.net import CONNECTOR, egress_policy_for, evaluate, fetch\n\n\n"
        "def _policy():\n    return egress_policy_for(CONNECTOR)\n\n\n"
        "class Client:\n"
        "    def _own(self):\n        return _policy().with_overrides(timeout_s=5.0)\n\n"
        "    async def calls(self, url, timeout):\n"
        "        policy = egress_policy_for(CONNECTOR)\n"
        "        if timeout:\n            policy = replace(policy, timeout_s=float(timeout))\n"
        "        await fetch(url, policy=policy)\n"
        "        await fetch(url, policy=self._own())\n"
        "        await net.fetch(url, policy=net.egress_policy_for(net.CONNECTOR))\n"
        "        return evaluate(url, _policy())\n"
    )
    apps = _known()
    apps["layered-app"] = {"provider.py": source}
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 0, run.stdout
    assert run.stdout.splitlines() == [_CLEAN.replace("OK: 8 ", "OK: 12 ")], run.stdout


def test_a_policy_the_rail_cannot_place_reds(tmp_path):
    source = (
        "from personalclaw.sdk.net import fetch\n"
        "from elsewhere import policy_for\n\n\n"
        "async def search(url):\n    return await fetch(url, policy=policy_for(url))\n"
    )
    run = _with(tmp_path, "opaque-app", source)
    assert run.returncode == 1, run.stdout
    assert (
        "opaque-app/provider.py:6: where this request's policy comes from cannot be read "
        "(opaque-app/provider.py::search::fetch)"
    ) in run.stdout, run.stdout


def test_a_policy_passed_in_by_an_unnamed_caller_reds(tmp_path):
    """A seam that takes its policy from its caller is named in PASS_THROUGH, or the rail cannot
    say the owner's settings reach it."""
    source = (
        "from personalclaw.sdk.net import fetch\n\n\n"
        "async def get(url, policy):\n    return await fetch(url, policy=policy)\n"
    )
    run = _with(tmp_path, "seam-app", source)
    assert run.returncode == 1, run.stdout
    assert (
        "seam-app/provider.py:5 takes its policy from its parameter 'policy' "
        "(seam-app/provider.py::get::fetch): name it in PASS_THROUGH"
    ) in run.stdout, run.stdout


def test_a_pass_through_entry_that_no_longer_passes_through_reds(tmp_path):
    """A stale entry reds whether its seam is gone or now builds its own layered policy."""
    apps = _known()
    del apps["git-repo"]
    apps["openrouter-models"] = {"provider.py": _LAYERED.replace("search", "_request_json")}
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert (
        "git-repo/provider.py::GitRepoSourceProvider._fetch_json::fetch is in PASS_THROUGH but no "
        "longer exists; remove it"
    ) in run.stdout, run.stdout
    assert (
        "openrouter-models/provider.py::_request_json::fetch no longer takes its policy from its "
        "caller; remove it"
    ) in run.stdout, run.stdout


def test_a_known_app_that_stops_showing_a_layered_request_reds(tmp_path):
    """The vacuity floor: an app named in KNOWN_LAYERED must be seen making a layered request."""
    apps = _known()
    apps["brave-search"] = {"provider.py": "import json\n"}
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 1, run.stdout
    assert (
        "vacuity floor: brave-search was not seen making a request under the owner's network "
        "settings"
    ) in run.stdout, run.stdout


def test_a_file_the_rail_cannot_parse_reds(tmp_path):
    """A file that does not parse hides whatever requests it makes, so it is not read as clean."""
    run = _with(tmp_path, "broken-app", "def (:\n")
    assert run.returncode == 1, run.stdout
    assert "cannot read broken-app/provider.py" in run.stdout, run.stdout


def test_a_failed_check_that_lets_the_request_go_ahead_reds(tmp_path):
    """The planted defect: a synchronous surface asks the guard, swallows its failure and makes
    its own request anyway, which the check never judged."""
    source = (
        "import urllib.request\n\n"
        "from personalclaw.sdk.net import CONNECTOR, egress_policy_for, evaluate\n\n\n"
        "class Market:\n"
        "    def get(self, url):\n"
        "        try:\n"
        "            decision = evaluate(url, egress_policy_for(CONNECTOR))\n"
        "            if not decision.allow:\n"
        "                raise RuntimeError(decision.reason)\n"
        "        except RuntimeError:\n"
        "            raise\n"
        "        except Exception:\n"
        "            pass\n"
        "        return urllib.request.urlopen(url)\n"
    )
    run = _with(tmp_path, "sync-app", source)
    assert run.returncode == 1, run.stdout
    assert (
        "sync-app/provider.py:14: a failure of the egress check at line 9 is caught here and the "
        "code goes on, so the request goes ahead unchecked "
        "(sync-app/provider.py::Market.get::evaluate)"
    ) in run.stdout, run.stdout
    assert "provider.py:12:" not in run.stdout, run.stdout


def test_a_failed_check_that_refuses_is_green(tmp_path):
    """A handler that raises, or returns without the request, refuses as a denied host is."""
    source = (
        "import urllib.request\n\n"
        "from personalclaw.sdk.net import CONNECTOR, egress_policy_for, evaluate\n\n\n"
        "def get(url):\n"
        "    try:\n"
        "        decision = evaluate(url, egress_policy_for(CONNECTOR))\n"
        "    except Exception as exc:\n"
        "        raise RuntimeError(f'{url} was not reached: {exc}') from exc\n"
        "    if not decision.allow:\n"
        "        raise RuntimeError(decision.reason)\n"
        "    return urllib.request.urlopen(url)\n\n\n"
        "def connected(url):\n"
        "    try:\n"
        "        if not evaluate(url, egress_policy_for(CONNECTOR)).allow:\n"
        "            return False\n"
        "    except Exception:\n"
        "        return False\n"
        "    return True\n"
    )
    apps = _known()
    apps["refusing-app"] = {"provider.py": source}
    run = _check(_seeded(tmp_path, apps))
    assert run.returncode == 0, run.stdout
    assert run.stdout.splitlines() == [_CLEAN.replace("OK: 8 ", "OK: 10 ")], run.stdout
