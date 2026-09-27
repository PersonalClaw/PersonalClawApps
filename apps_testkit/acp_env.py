"""What an ACP app's CLI is handed, measured with a stub CLI that records its environment.

An agent CLI gets no variable of the gateway's it is not handed: the child allowlist, what its
app computed (``env``), the variables its app declares it reads to pick a provider, a region or a
model (``env_passthrough``), and what the owner passed through by name. So each ACP app proves
that what it declares is what its CLI receives, and that no credential comes along, by spawning a
stub in the real CLI's place from the entry the app registers, through core's real transport
(``personalclaw.sdk.testing.launch_acp_entry``). No real agent CLI is launched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

#: Credentials the gateway's environment may hold, which no ACP CLI may see.
PLANTED_SECRETS: dict[str, str] = {
    "AWS_ACCESS_KEY_ID": "planted-access-key-id",
    "AWS_SECRET_ACCESS_KEY": "planted-secret",
    "AWS_SESSION_TOKEN": "planted-session",
    "AWS_BEARER_TOKEN_BEDROCK": "planted-bearer",
    "ANTHROPIC_API_KEY": "planted-anthropic-key",
    "OPENAI_API_KEY": "planted-openai-key",
    "GEMINI_API_KEY": "planted-gemini",
    "GOOGLE_API_KEY": "planted-google",
}

_STUB = textwrap.dedent("""
    import json, os, sys
    with open(sys.argv[1], "w") as f:
        json.dump(dict(os.environ), f)
    """)


def stub_command(tmp_path: Path) -> tuple[list[str], Path]:
    """A launch argv that runs a stub in the CLI's place, and the file it records its env to."""
    stub = tmp_path / "stub_cli.py"
    stub.write_text(_STUB, encoding="utf-8")
    record = tmp_path / "handed-env.json"
    return [sys.executable, str(stub), str(record)], record


def handed_env(entry, record: Path, work_dir: Path) -> dict[str, str]:
    """Launch *entry*'s command as a spawn from the entry would, and return what it was handed."""
    from personalclaw.sdk.testing import launch_acp_entry

    assert asyncio.run(launch_acp_entry(entry.options, work_dir)) == 0
    return json.loads(record.read_text(encoding="utf-8"))
