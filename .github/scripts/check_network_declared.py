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
  weights. ``aiohttp.web`` alone is a server, and does not count. An import by name at run time
  (``importlib.import_module``, ``__import__``) counts the same, a relative name resolved
  against its package;
* anything from ``personalclaw.sdk.net`` but its two sentence helpers;
* a model provider on the SDK's OpenAI or Anthropic wire, or registered as a branded app: core
  makes those calls, and they go to the vendor the app names;
* an ACP agent entry: the agent's CLI reaches its vendor, and an adapter ``npx`` fetches;
* in a file that starts a program, a program that talks to another machine (``rsync``,
  ``ssh``, ``scp``, ``sftp``, ``curl``, ``wget``, a forge's CLI, ``npx``), or git with
  ``clone``, ``fetch``, ``pull``, ``push`` or ``ls-remote``; or a command line for a shell
  (``os.system``, ``shell=True``) whose first word is one. A spawn is recognised by what it is,
  resolved through the file's imports, whatever name it was imported as.

An app that shows any of those and does not declare ``network: true`` fails by name, with the
signals the rail saw, unless ``EXEMPT`` says why it reaches no network after all. A stale
exemption fails too, and so does a file the rail cannot parse, since its network use is then
unknown. So does an import whose module name is only known at run time, in an app that does not
declare network, unless ``EXEMPT`` says why.

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

#: Calls that import a module named at run time.
RUNTIME_IMPORTS = frozenset(
    {"importlib.import_module", "importlib.__import__", "__import__", "builtins.__import__"}
)

#: What an import whose module name is not a constant says: what it reaches is unknown.
RUNTIME_NAME = "imports a module named only at run time"

#: Spawns that take one command line for a shell, whose first word names the program.
SHELL_SPAWNS = frozenset(
    {
        "os.system",
        "os.popen",
        "asyncio.create_subprocess_shell",
        "asyncio.subprocess.create_subprocess_shell",
    }
)

#: Every spawn, by what it is once the file's imports are resolved (``sp.run`` after
#: ``import subprocess as sp``, or a bare ``run`` after ``from subprocess import run``).
SPAWNS = SHELL_SPAWNS | frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_output",
        "subprocess.check_call",
        "asyncio.create_subprocess_exec",
        "asyncio.subprocess.create_subprocess_exec",
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


def _aliases(tree: ast.AST) -> dict[str, str]:
    """What each name a file binds by import stands for: ``import subprocess as sp`` binds
    ``sp`` to ``subprocess``, ``from subprocess import run as r`` binds ``r`` to
    ``subprocess.run``, and ``import a.b`` binds ``a``."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                out[alias.asname or head] = alias.name if alias.asname else head
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return out


def _resolve(callee: str, aliases: dict[str, str]) -> str:
    """*callee* with its first name replaced by what the file imported under it."""
    head, _, rest = callee.partition(".")
    target = aliases.get(head)
    if target is None:
        return callee
    return f"{target}.{rest}" if rest else target


def _relative(name: str, package: str) -> str:
    """A name with leading dots, resolved against *package* as ``importlib`` does."""
    level = len(name) - len(name.lstrip("."))
    parts = package.split(".") if package else []
    parts = parts[: len(parts) - (level - 1)] if level > 1 else parts
    rest = name[level:]
    return ".".join([*parts, rest] if rest else parts)


def _argument(call: ast.Call, position: int, keyword: str) -> ast.AST | None:
    if len(call.args) > position:
        return call.args[position]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def _text(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _runtime_names(call: ast.Call, callee: str, package: str) -> list[str] | None:
    """The modules a run-time import brings in, or ``None`` when its name is not a constant.

    ``import_module(".x", package="p")`` is resolved against ``p``, and ``__import__`` with a
    ``level`` against the file's own package. ``__import__("a", fromlist=["b"])`` also
    imports ``a.b``."""
    name = _text(_argument(call, 0, "name"))
    if name is None:
        return None
    if callee == "importlib.import_module":
        if name.startswith("."):
            name = _relative(name, _text(_argument(call, 1, "package")) or package)
        return [name]
    level = _argument(call, 4, "level")
    if isinstance(level, ast.Constant) and isinstance(level.value, int) and level.value > 0:
        name = _relative("." * level.value + name, package)
    names = [name]
    fromlist = _argument(call, 3, "fromlist")
    if isinstance(fromlist, (ast.List, ast.Tuple)):
        names += [f"{name}.{item}" for item in map(_text, fromlist.elts) if item]
    return names


def _shell_signals(line: int, command: ast.AST) -> list[tuple[int, str]]:
    """The signal a command line for a shell shows, read from its first words: a constant, or
    the constant head of an f-string or of a ``+`` concatenation."""
    while isinstance(command, ast.BinOp) and isinstance(command.op, ast.Add):
        command = command.left
    if isinstance(command, ast.JoinedStr) and command.values:
        command = command.values[0]
    words = (_text(command) or "").split()
    if words and words[0] in REMOTE_PROGRAMS:
        return [(line, f"starts {words[0]}")]
    if words and words[0] == "git":
        verb = next((word for word in words[1:] if word in GIT_REMOTE_VERBS), "")
        return [(line, f"runs git {verb}")] if verb else []
    return []


def signals(source: str, package: str = "") -> list[tuple[int, str]]:
    """``(line, signal)`` for every sign in *source* that its code reaches the network.
    *package* is the file's own package, for a relative name imported at run time."""
    tree = ast.parse(source)
    aliases = _aliases(tree)
    found: list[tuple[int, str]] = []
    spawns = mentions_git = False
    constants: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        for dotted in _imported(node):
            signal = _import_signal(dotted)
            if signal:
                found.append((node.lineno, signal))
        if isinstance(node, ast.Call):
            callee = _resolve(_callee(node), aliases)
            if callee in SPAWNS:
                spawns = True
                shell = callee in SHELL_SPAWNS or any(
                    k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in node.keywords
                )
                if shell and node.args:
                    found += _shell_signals(node.lineno, node.args[0])
            if callee.split(".")[-1] == "git_argv":
                spawns = mentions_git = True
            if callee in RUNTIME_IMPORTS:
                names = _runtime_names(node, callee, package)
                if names is None:
                    found.append((node.lineno, RUNTIME_NAME))
                else:
                    found += [(node.lineno, s) for s in map(_import_signal, names) if s]
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


def census(
    root: pathlib.Path = ROOT,
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """``app → ["file:line: signal", …]`` for every app whose code shows a signal,
    ``app → ["file:line", …]`` for every import whose module name is only known at run time,
    and the files that could not be read."""
    out: dict[str, list[str]] = {}
    unknown: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for manifest in sorted(root.glob("*/app.json")):
        bundle = manifest.parent
        for path in sorted(bundle.rglob("*.py")):
            if not _is_bundle_code(path, bundle):
                continue
            rel = path.relative_to(root).as_posix()
            package = ".".join(path.relative_to(bundle).parent.parts)
            try:
                found = signals(path.read_text(encoding="utf-8"), package)
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                unreadable.append(f"{rel}: {exc}")
                continue
            for line, signal in found:
                if signal == RUNTIME_NAME:
                    unknown.setdefault(bundle.name, []).append(f"{rel}:{line}")
                else:
                    out.setdefault(bundle.name, []).append(f"{rel}:{line}: {signal}")
    return out, unknown, unreadable


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
        # imports by name at run time
        (
            "import importlib\nimportlib.import_module('httpx')\n",
            ["imports httpx (an HTTP client)"],
        ),
        (
            "from importlib import import_module\nimport_module('socket')\n",
            ["imports socket (sockets)"],
        ),
        ("__import__('smtplib')\n", ["imports smtplib (an SMTP client)"]),
        ("import importlib\nimportlib.__import__('boto3')\n", ["imports boto3 (the AWS SDK)"]),
        (
            "__import__('urllib', fromlist=['request'])\n",
            ["imports urllib.request (an HTTP client)"],
        ),
        (
            "import importlib\nimportlib.import_module('.request', package='urllib')\n",
            ["imports urllib.request (an HTTP client)"],
        ),
        ("import importlib\nimportlib.import_module('.transport')\n", []),
        ("import importlib\nimportlib.import_module('json')\n", []),
        ("import importlib\nimportlib.import_module(name)\n", [RUNTIME_NAME]),
        ("def import_module(name):\n    pass\nimport_module(name)\n", []),
        # spawns by any name, resolved through the file's imports
        ("from subprocess import run\nrun(['ssh', host])\n", ["starts ssh"]),
        ("import subprocess as sp\nsp.run(['rsync', src, dst])\n", ["starts rsync"]),
        ("from subprocess import run as r\nr(['git', 'push'])\n", ["runs git push"]),
        (
            "from asyncio import create_subprocess_exec\ncreate_subprocess_exec('gh', 'pr')\n",
            ["starts gh"],
        ),
        ("def run(argv):\n    pass\nrun(['ssh', host])\n", []),
        # command lines for a shell
        ("import os\nos.system('ssh host uptime')\n", ["starts ssh"]),
        ("subprocess.run('git fetch origin', shell=True)\n", ["runs git fetch"]),
        ("subprocess.run(f'curl {url}', shell=True)\n", ["starts curl"]),
        ("import os\nos.popen('scp ' + src + ' ' + dst)\n", ["starts scp"]),
        ("import os\nos.system('ls -la')\n", []),
    ]
    problems = []
    for source, want in cases:
        got = [signal for _line, signal in signals(source, "pkg")]
        if got != want:
            problems.append(f"the detectors read {source!r} as {got}, expected {want}")
    return problems


def problems(root: pathlib.Path = ROOT) -> list[str]:
    found = _detector_problems()
    seen, unknown, unreadable = census(root)
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
    # An app that declares network is not made wrong by an import the rail cannot name.
    for app, sites in sorted(unknown.items()):
        if app in EXEMPT or declares_network(manifests[app]):
            continue
        found.append(
            f"{app}: {', '.join(sites)} imports a module named only at run time, so whether "
            f"{app} reaches the network is unknown. Name the module, declare "
            '"network": true, or say in EXEMPT why it reaches no network'
        )
    for app in sorted(EXEMPT):
        if app not in seen and app not in unknown:
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
    seen, _unknown, _unreadable = census()
    print(f"OK: {len(seen)} app(s) reach the network, and every one declares it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
