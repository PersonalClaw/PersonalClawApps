"""What an ACP app's CLI is handed, measured with a stub CLI that records what it gets.

An agent CLI gets no variable of the gateway's it is not handed: the child allowlist, what its
app computed (``env``), the variables its app declares it reads to pick a provider, a region or a
model (``env_passthrough``), and what the owner passed through by name. So each ACP app proves
that what it declares is what its CLI receives, and that no credential comes along, by spawning a
stub in the real CLI's place from the entry the app registers, through core's real transport
(``personalclaw.sdk.testing.launch_acp_entry``).

And an app that declares per-session options for its CLI (``session_meta``) proves the session
the CLI is asked for carries them, with :func:`acp_stub_command` and :func:`opened_sessions`: a
stub that answers ACP and records each frame, started from the app's entry by core's runtime.
No real agent CLI is launched.
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


_ACP_STUB = textwrap.dedent("""
    import json, os, sys
    record = open(sys.argv[1], "a")
    # It starts each session in a mode that approves edits itself, as an adapter does when the
    # settings it reads say so, so a test sees whether the host takes that back.
    modes = {"currentModeId": "acceptEdits", "availableModes": []}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            msg["handedEnv"] = dict(os.environ)
        record.write(json.dumps(msg) + "\\n")
        record.flush()
        if "id" not in msg or "method" not in msg:
            continue
        method = msg["method"]
        if method == "initialize":
            result = {"protocolVersion": 1, "agentCapabilities": {}}
        elif method == "session/new":
            result = {"sessionId": "stub-session", "modes": modes}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\\n")
        sys.stdout.flush()
    """)


def acp_stub_command(tmp_path: Path) -> tuple[list[str], Path]:
    """A launch argv that runs a stub ACP agent in the CLI's place, and the file it records each
    frame it is sent to (``initialize`` with the environment it was handed)."""
    stub = tmp_path / "stub_acp_agent.py"
    stub.write_text(_ACP_STUB, encoding="utf-8")
    record = tmp_path / "frames.jsonl"
    return [sys.executable, str(stub), str(record)], record


def opened_sessions(runtime: str, record: Path) -> tuple[dict[str, str], list[dict], list[str]]:
    """Start the registered *runtime* (``acp:<cli>``) as a chat would, against the stub from
    :func:`acp_stub_command`, and return what its process was handed, the params of each
    ``session/new`` it was sent, and each permission mode the host then set on a session
    (``session/set_config_option`` with ``configId: "mode"``)."""
    from personalclaw.llm.registry import get_default_registry

    async def _start() -> None:
        provider = get_default_registry().build(runtime)
        try:
            await asyncio.wait_for(provider.start(), timeout=60)
        finally:
            await provider.shutdown()

    asyncio.run(_start())
    frames = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]
    [init] = [f for f in frames if f.get("method") == "initialize"]
    sessions = [f["params"] for f in frames if f.get("method") == "session/new"]
    modes = [
        f["params"]["value"]
        for f in frames
        if f.get("method") == "session/set_config_option" and f["params"].get("configId") == "mode"
    ]
    return init["handedEnv"], sessions, modes
