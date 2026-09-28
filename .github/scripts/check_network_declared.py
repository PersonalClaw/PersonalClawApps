#!/usr/bin/env python3
"""Repo rail: every app whose code reaches the network declares ``"network": true``.

Install consent reads an app's ``permissions.network``: "Network access: declared" when it is
true, and "not declared" when the manifest leaves it out. Core acts on it too: a backend without
it runs with outbound traffic off where its tier can isolate it, and an action is judged by it to
leave this machine or not. So an app whose code reaches the network without saying so shows the
owner something untrue at install, and has its actions judged as staying on this machine.

The manifest has no list of hosts (``network`` is one boolean), so each app's README names the
host it reaches, or the setting that names it.

What counts as reaching the network, read on the AST from each bundle's own code (its tests,
``conftest.py`` and hidden folders left out):

* an import of something that opens connections itself: an HTTP, socket or WebSocket client, a
  mail or cloud SDK, a vendor's model or chat SDK, or a library that downloads its model
  weights. ``aiohttp.web`` alone is a server, and does not count;
* anything from ``personalclaw.sdk.net`` but its two sentence helpers;
* a model provider on the SDK's OpenAI or Anthropic wire, or registered as a branded app: core
  makes those calls, and they go to the vendor the app names;
* an ACP agent entry: the agent's CLI reaches its vendor, and an adapter ``npx`` fetches;
* in a file that starts a program, a program that talks to another machine (``rsync``,
  ``ssh``, ``scp``, ``sftp``, ``curl``, ``wget``, a forge's CLI, ``npx``), or git with
  ``clone``, ``fetch``, ``pull``, ``push`` or ``ls-remote``.

An app that shows any of those and does not declare ``network: true`` fails by name, with the
signals the rail saw, unless ``EXEMPT`` says why it reaches no network after all. A stale
exemption fails too, and so does a file the rail cannot parse, since its network use is then
unknown.

**Vacuity floor.** A rail that matches nothing reads as clean, so the detectors are checked
against every shape they tell apart before anything is read, and each app in
``KNOWN_SIGNALLED`` must be seen with the signal it is named for: one per kind of signal, so a
detector that stops matching real code turns this red.

Run locally exactly as CI does:

    python .github/scripts/check_network_declared.py
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Modules that open connections themselves. An entry matches that module and anything under it.
CLIENT_MODULES = {
    "aiohttp": "an HTTP client",
    "httpx": "an HTTP client",
    "requests": "an HTTP client",
    "urllib3": "an HTTP client",
    "urllib.request": "an HTTP client",
    "http.client": "an HTTP client",
    "socket": "sockets",
    "websockets": "a WebSocket client",
    "smtplib": "an SMTP client",
    "imaplib": "an IMAP client",
    "poplib": "a POP3 client",
    "ftplib": "an FTP client",
    "boto3": "the AWS SDK",
    "botocore": "the AWS SDK",
    "qdrant_client": "a Qdrant client",
    "huggingface_hub": "the Hugging Face Hub client",
    "faster_whisper": "a model library that downloads its weights",
    "sentence_transformers": "a model library that downloads its weights",
    "transformers": "a model library that downloads its weights",
    "diffusers": "a model library that downloads its weights",
    "pyannote.audio": "a model library that downloads its weights",
    "openai": "OpenAI's SDK",
    "anthropic": "Anthropic's SDK",
    "slack_sdk": "Slack's SDK",
    "slack_bolt": "Slack's SDK",
    "discord": "a Discord client",
    "telegram": "a Telegram client",
}

#: ``aiohttp``'s server framework, which listens rather than connects.
AIOHTTP_SERVER = "aiohttp.web"

#: The SDK's egress module, and the only names in it that make no connection.
SDK_NET = "personalclaw.sdk.net"
SDK_NET_SENTENCES = frozenset({"sentence_with_detail", "relayed_failure_copy"})

#: Names that make a model provider whose calls core makes, to the vendor the app names.
SDK_MODEL = "personalclaw.sdk.model"
MODEL_WIRE = frozenset(
    {
        "register_branded_app",
        "OpenAIProvider",
        "AnthropicProvider",
        "openai_compatible_list_models",
        "openai_compatible_discover_models",
    }
)

#: Names that make an ACP agent: its CLI reaches its vendor, and an adapter npx fetches.
SDK_ACP = "personalclaw.sdk.acp"
ACP_ENTRY = frozenset({"register_acp_cli_entry", "provision_acp_adapter"})

SPAWNS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_output",
        "subprocess.check_call",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
)

#: Programs that talk to another machine when an app's code starts them.
REMOTE_PROGRAMS = frozenset({"rsync", "ssh", "scp", "sftp", "curl", "wget", "gh", "glab", "npx"})

#: Git's verbs that talk to a remote.
GIT_REMOTE_VERBS = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})

#: Apps whose code shows a signal but reaches no network, and why.
EXEMPT: dict[str, str] = {}

#: The vacuity floor: one app per kind of signal, each of which must be seen with it.
KNOWN_SIGNALLED = {
    "tavily-search": "imports personalclaw.sdk.net.fetch",
    "email-channel": "imports smtplib",
    "bedrock-models": "imports boto3",
    "groq-models": "register_branded_app",
    "claude-code-agent": "register_acp_cli_entry",
    "git-sync": "runs git push",
    "rsync-sync": "starts ssh",
    "code-review": "starts gh",
}


def _callee(call: ast.Call) -> str:
    parts: list[str] = []
    node = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _imported(node: ast.AST) -> list[str]:
    """The dotted names an import brings in: ``import a.b`` gives ``a.b``, and
    ``from a import b, c`` gives ``a.b`` and ``a.c``."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [f"{node.module}.{alias.name}" for alias in node.names]
    return []


def _import_signal(dotted: str) -> str:
    """What importing *dotted* says about the network, or ``""`` when it says nothing."""
    if dotted == AIOHTTP_SERVER or dotted.startswith(AIOHTTP_SERVER + "."):
        return ""
    if dotted == SDK_NET:
        return f"imports {SDK_NET}"
    for package, names, what in (
        (SDK_NET, None, "the SDK's guarded egress"),
        (SDK_MODEL, MODEL_WIRE, "a model provider whose calls go to its vendor"),
        (SDK_ACP, ACP_ENTRY, "an ACP agent whose CLI reaches its vendor"),
    ):
        if dotted.startswith(package + "."):
            name = dotted[len(package) + 1 :]
            if names is None:
                return "" if name in SDK_NET_SENTENCES else f"imports {dotted} ({what})"
            return f"uses {name} ({what})" if name in names else ""
    for module, what in CLIENT_MODULES.items():
        if dotted == module or dotted.startswith(module + "."):
            return f"imports {module} ({what})"
    return ""


def signals(source: str) -> list[tuple[int, str]]:
    """``(line, signal)`` for every sign in *source* that its code reaches the network."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    spawns = mentions_git = False
    constants: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        for dotted in _imported(node):
            signal = _import_signal(dotted)
            if signal:
                found.append((node.lineno, signal))
        if isinstance(node, ast.Call):
            callee = _callee(node)
            if ".".join(callee.split(".")[-2:]) in SPAWNS:
                spawns = True
            if callee.split(".")[-1] == "git_argv":
                spawns = mentions_git = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            constants.append((node.lineno, node.value))
            if node.value == "git":
                mentions_git = True
    if spawns:
        for line, value in constants:
            if value in REMOTE_PROGRAMS:
                found.append((line, f"starts {value}"))
            elif value in GIT_REMOTE_VERBS and mentions_git:
                found.append((line, f"runs git {value}"))
    return sorted(found)


def _is_bundle_code(path: pathlib.Path, bundle: pathlib.Path) -> bool:
    rel = path.relative_to(bundle)
    return not (
        path.name.startswith("test_")
        or path.name == "conftest.py"
        or "tests" in rel.parts
        or "node_modules" in rel.parts
        or any(part.startswith(".") for part in rel.parts)
    )


def declares_network(manifest: pathlib.Path) -> bool:
    permissions = json.loads(manifest.read_text(encoding="utf-8")).get("permissions")
    return isinstance(permissions, dict) and permissions.get("network") is True


def census(root: pathlib.Path = ROOT) -> tuple[dict[str, list[str]], list[str]]:
    """``app → ["file:line: signal", …]`` for every app whose code shows a signal, and the
    files that could not be read."""
    out: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for manifest in sorted(root.glob("*/app.json")):
        bundle = manifest.parent
        for path in sorted(bundle.rglob("*.py")):
            if not _is_bundle_code(path, bundle):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                found = signals(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                unreadable.append(f"{rel}: {exc}")
                continue
            for line, signal in found:
                out.setdefault(bundle.name, []).append(f"{rel}:{line}: {signal}")
    return out, unreadable


def _detector_problems() -> list[str]:
    """The positive control: the detectors on every shape they have to tell apart."""
    cases = [
        ("import aiohttp\n", ["imports aiohttp (an HTTP client)"]),
        ("from aiohttp import web\n", []),
        ("from aiohttp import ClientSession, web\n", ["imports aiohttp (an HTTP client)"]),
        ("from urllib import request\n", ["imports urllib.request (an HTTP client)"]),
        ("from urllib.parse import urlsplit\n", []),
        ("from http import HTTPStatus\n", []),
        ("from personalclaw.sdk.net import sentence_with_detail\n", []),
        (
            "from personalclaw.sdk.net import fetch\n",
            ["imports personalclaw.sdk.net.fetch (the SDK's guarded egress)"],
        ),
        (
            "from personalclaw.sdk.model import register_branded_app, ModelInfo\n",
            ["uses register_branded_app (a model provider whose calls go to its vendor)"],
        ),
        (
            "from personalclaw.sdk.acp import register_acp_cli_entry\n",
            ["uses register_acp_cli_entry (an ACP agent whose CLI reaches its vendor)"],
        ),
        ("def f():\n    subprocess.run(['git', 'log'])\n", []),
        ("def f():\n    subprocess.run(['git', 'push'])\n", ["runs git push"]),
        ("def f():\n    subprocess.run(git_argv(['fetch', 'origin']))\n", ["runs git fetch"]),
        ("def f():\n    subprocess.run(['ssh', host])\n", ["starts ssh"]),
        ("ACTIONS = ['push', 'ssh']\n", []),
    ]
    problems = []
    for source, want in cases:
        got = [signal for _line, signal in signals(source)]
        if got != want:
            problems.append(f"the detectors read {source!r} as {got}, expected {want}")
    return problems


def problems(root: pathlib.Path = ROOT) -> list[str]:
    found = _detector_problems()
    seen, unreadable = census(root)
    found += [f"cannot read {entry}, so its network use is unknown" for entry in unreadable]
    for app, want in sorted(KNOWN_SIGNALLED.items()):
        if not any(want in signal for signal in seen.get(app, [])):
            found.append(
                f"vacuity floor: {app} was not seen with {want!r}; either a detector stopped "
                "matching real code, or the app changed (update KNOWN_SIGNALLED in the same "
                "change)"
            )
    manifests = {m.parent.name: m for m in root.glob("*/app.json")}
    for app, entries in sorted(seen.items()):
        if app in EXEMPT or declares_network(manifests[app]):
            continue
        more = f"; and {len(entries) - 3} more" if len(entries) > 3 else ""
        shown = "; ".join(entries[:3]) + more
        found.append(
            f"{app}: its code reaches the network ({shown}), but {app}/app.json does not declare "
            '"network": true under permissions. Declare it, and name the host in the README; or '
            "say in EXEMPT why it reaches no network"
        )
    for app in sorted(EXEMPT):
        if app not in seen:
            found.append(f"{app} is exempt, but its code shows no network signal now; remove it")
        elif app in manifests and declares_network(manifests[app]):
            found.append(f"{app} is exempt, but it declares network now; remove the exemption")
    return found


def main() -> int:
    found = problems()
    if found:
        print("An app whose code reaches the network must declare it:")
        for line in found:
            print(f"  {line}")
        return 1
    seen, _ = census()
    print(f"OK: {len(seen)} app(s) reach the network, and every one declares it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
