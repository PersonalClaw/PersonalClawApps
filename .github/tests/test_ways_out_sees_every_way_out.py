"""``apps_testkit.ways_out`` sees every way out a module adds, however it is written.

The census tests of the tool apps that hold their tools' ``consequences`` to what they do
(code-review, issue-radar, design-critique, web-tools) pin what :func:`ways_out` reads in their
modules today, so a module that adds a way out fails them only if the reading sees it. Each case
below plants one way out beside a module's own use of a watched module, and the reading must
name it: a new import, a deeper name, an alias, a module handed on whole, a star import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # for apps_testkit

from apps_testkit.ways_out import ways_out  # noqa: E402

#: What the planted module reads before anything is added: a command started through asyncio.
BASE = (
    "import asyncio\n"
    "from personalclaw.sdk.net import web_fetch\n"
    "async def run():\n"
    "    await asyncio.create_subprocess_exec('gh', stdout=asyncio.subprocess.PIPE)\n"
    "    await web_fetch('https://example.test')\n"
)
WATCHED = ("asyncio", "os", "personalclaw.sdk.net")
BASE_REACHED = {
    "asyncio.create_subprocess_exec", "asyncio.subprocess.PIPE", "personalclaw.sdk.net.web_fetch",
}


def _read(tmp_path: Path, source: str) -> tuple[set[str], set[str]]:
    module = tmp_path / "provider.py"
    module.write_text(source, encoding="utf-8")
    return ways_out([module], WATCHED)


def test_the_base_reads_as_its_own_uses(tmp_path):
    imports, reached = _read(tmp_path, BASE)
    assert imports == {"asyncio", "personalclaw.sdk.net"}
    assert reached == BASE_REACHED


@pytest.mark.parametrize(
    ("added", "seen"),
    [
        ("import aiohttp\n", "import aiohttp"),
        ("from personalclaw.sdk.net import fetch\nfetch('u', method='POST')\n",
         "personalclaw.sdk.net.fetch"),
        ("asyncio.subprocess.create_subprocess_shell('x')\n",
         "asyncio.subprocess.create_subprocess_shell"),
        ("asyncio.create_subprocess_shell('x')\n", "asyncio.create_subprocess_shell"),
        ("from asyncio import subprocess as sp\nsp.create_subprocess_shell('x')\n",
         "asyncio.subprocess.create_subprocess_shell"),
        ("import os as _o\n_o.system('x')\n", "os.system"),
        ("import asyncio as _a\n_a.open_connection('h', 1)\n", "asyncio.open_connection"),
        ("getattr(asyncio, 'create_subprocess_shell')('x')\n", "asyncio"),
        ("sub = asyncio.subprocess\nsub.create_subprocess_shell('x')\n", "asyncio.subprocess"),
        ("from asyncio import *\ncreate_subprocess_shell('x')\n", "asyncio.*"),
        ("import personalclaw.sdk.net as n\nn.http_session()\n", "personalclaw.sdk.net.http_session"),
        ("import personalclaw.sdk.net\npersonalclaw.sdk.net.open_url('u')\n",
         "personalclaw.sdk.net.open_url"),
        ("from personalclaw.sdk import net\nnet.open_url('u')\n", "personalclaw.sdk.net.open_url"),
        ("from personalclaw.sdk.net import *\n", "personalclaw.sdk.net.*"),
    ],
)
def test_a_way_out_added_beside_the_base_is_seen(tmp_path, added, seen):
    imports, reached = _read(tmp_path, BASE + added)
    if seen.startswith("import "):
        assert seen.removeprefix("import ") in imports - {"asyncio", "personalclaw.sdk.net"}
    else:
        assert seen in reached - BASE_REACHED, reached


def test_a_module_that_is_not_watched_is_not_read(tmp_path):
    imports, reached = _read(tmp_path, BASE + "import json\njson.dumps({})\n")
    assert "json" in imports
    assert reached == BASE_REACHED
