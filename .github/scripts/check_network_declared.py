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
  (``importlib.import_module``, ``__import__``, ``runpy.run_module``, or a ``find_spec`` whose
  spec is executed) counts the same, a relative name resolved against its package. A bare
  ``find_spec`` only asks whether a module is there, and does not count;
* anything from ``personalclaw.sdk.net`` but its two sentence helpers;
* a model provider on the SDK's OpenAI or Anthropic wire, or registered as a branded app: core
  makes those calls, and they go to the vendor the app names;
* an ACP agent entry: the agent's CLI reaches its vendor, and an adapter ``npx`` fetches;
* in a file that starts a program, a program that talks to another machine (``rsync``,
  ``ssh``, ``scp``, ``sftp``, ``curl``, ``wget``, a forge's CLI, ``npx``), or git with
  ``clone``, ``fetch``, ``pull``, ``push`` or ``ls-remote``, one given by path read by its
  basename (``/usr/bin/ssh``); or a command line for a shell (``os.system``, ``shell=True``)
  any of whose commands runs one, read past ``VAR=value``, simple prefixes (``env``, ``sudo``,
  ``nohup`` …) and one level of ``sh -c``, and into its command substitutions (``$(…)``,
  backticks, ``<(…)``). A spawn is recognised by what it is, resolved through the file's
  imports and assignments, whatever name it was imported or bound as; one passed as a callable
  (``run_in_executor``, ``partial``) marks the file as starting programs too;
* the code in a constant handed to ``exec`` or ``eval``, read like the rest of the file.

An app that shows any of those and does not declare ``network: true`` fails by name, with the
signals the rail saw, unless ``EXEMPT`` says why it reaches no network after all. A stale
exemption fails too, and so does a file the rail cannot parse, since its network use is then
unknown. So does, in an app that does not declare network, unless ``EXEMPT`` says why, an import
whose module name is only known at run time, or code loaded from a path that is not the app's
own (``spec_from_file_location``, ``SourceFileLoader``, ``runpy.run_path``, or ``exec`` of a
file's text).

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
import re
import shlex
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

#: Spawns that take one command line for a shell, whose commands the rail reads.
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

#: Spawns handed their argv as separate arguments, the program first.
EXEC_SPAWNS = frozenset(
    {"asyncio.create_subprocess_exec", "asyncio.subprocess.create_subprocess_exec"}
)

#: Programs that talk to another machine when an app's code starts them.
REMOTE_PROGRAMS = frozenset({"rsync", "ssh", "scp", "sftp", "curl", "wget", "gh", "glab", "npx"})

#: Git's verbs that talk to a remote.
GIT_REMOTE_VERBS = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})

#: On a shell line: the prefixes a command can carry and those of their options that take the
#: next word as their value, the shells whose ``-c`` script is read one level down, a
#: ``VAR=value`` assignment, git's options that take the next word as their value, and the one
#: word a part of the line only known at run time is read as.
SHELL_PREFIXES = frozenset({"env", "exec", "sudo", "nohup", "command", "time"})
PREFIX_VALUE_OPTIONS = {
    "sudo": frozenset({"-C", "-D", "-g", "-h", "-p", "-R", "-r", "-T", "-t", "-U", "-u"}),
    "env": frozenset({"-C", "-P", "-u"}),
    "exec": frozenset({"-a"}),
    "time": frozenset({"-f", "-o"}),
}
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
UNKNOWN_WORD = "…"

#: What a shell line is split on, and which of it ends a command: ``&&``, ``||``, ``;``, ``|``,
#: ``&``, and a newline that is neither quoted nor escaped.
PUNCTUATION = "();<>|&\n"
SEPARATORS = frozenset("|&;\n")

#: What code loaded from a path that is not the app's own says: what it runs is unknown.
RUNTIME_PATH = "loads code from a path only known at run time"

#: Each kind of unknown, and what naming it takes, so the rail can read what it reaches.
UNKNOWN_STEPS = {
    RUNTIME_NAME: "Name the module",
    RUNTIME_PATH: "Load it from the app's own folder",
}

#: Loaders that run code from a file, and where the path is: ``(position, keyword)``. A path
#: built on ``__file__`` is the app's own code, which the rail reads anyway.
PATH_LOADERS = {
    "importlib.util.spec_from_file_location": (1, "location"),
    "importlib.machinery.SourceFileLoader": (1, "path"),
    "importlib.machinery.SourcelessFileLoader": (1, "path"),
    "importlib.machinery.ExtensionFileLoader": (1, "path"),
    "runpy.run_path": (0, "path_name"),
}
FIND_SPEC = "importlib.util.find_spec"
MODULE_FROM_SPEC = "importlib.util.module_from_spec"
RUN_MODULE = "runpy.run_module"
#: Calls that make a spec whose origin the rail judges where it is made.
SPEC_MAKERS = frozenset({FIND_SPEC, "importlib.util.spec_from_file_location"})

#: Calls that run the code they are handed as text, the call that compiles it first, and the
#: calls that open a file whose text can be read.
EXECS = frozenset({"exec", "eval", "builtins.exec", "builtins.eval"})
COMPILES = frozenset({"compile", "builtins.compile"})
OPENS = frozenset({"open", "builtins.open", "io.open", "codecs.open"})

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


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for a name or an attribute chain rooted in a name, else ``""``: the callee of
    ``foo().run()`` is no name the file bound."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _callee(call: ast.Call) -> str:
    return _dotted(call.func)


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


def _pairs(target: ast.AST, value: ast.AST) -> list[tuple[str, ast.AST]]:
    """``(bound name, value)`` for each name or ``x.y`` target an assignment binds, a tuple
    target paired element by element."""
    if isinstance(target, (ast.Tuple, ast.List)):
        if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts):
            return [pair for t, v in zip(target.elts, value.elts) for pair in _pairs(t, v)]
        return []
    name = _dotted(target)
    return [(name, value)] if name else []


def _assigned(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """Every ``(bound name, value)`` the file's assignments make, at any scope."""
    out: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                out += _pairs(target, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            out += _pairs(node.target, node.value)
    return out


def _lookup(dotted: str, aliases: dict[str, str]) -> str | None:
    """What *dotted* stands for through the longest part of it the file bound, or ``None``."""
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        target = aliases.get(".".join(parts[:end]))
        if target is not None:
            return ".".join([target, *parts[end:]])
    return None


def _bound(value: ast.AST, aliases: dict[str, str]) -> str | None:
    """What an assigned *value* stands for: a name the file bound, or a ``functools.partial``
    of one."""
    if isinstance(value, ast.Call) and value.args:
        if _lookup(_callee(value), aliases) == "functools.partial":
            return _bound(value.args[0], aliases)
        return None
    name = _dotted(value)
    return _lookup(name, aliases) if name else None


def _aliases(tree: ast.AST) -> dict[str, str]:
    """What each name a file binds stands for: ``import subprocess as sp`` binds ``sp`` to
    ``subprocess``, ``from subprocess import run as r`` binds ``r`` to ``subprocess.run``, and
    ``import a.b`` binds ``a``. An assignment of one of those, at any scope (``run =
    subprocess.run``, ``self._run = sp.run``, ``run = partial(subprocess.run, …)``), binds its
    target too, followed to a fixed point so a chain of them resolves."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                out[alias.asname or head] = alias.name if alias.asname else head
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    pairs = _assigned(tree)
    for _ in range(len(pairs)):
        changed = False
        for name, value in pairs:
            target = _bound(value, out)
            if target is not None and out.get(name) != target:
                out[name] = target
                changed = True
        if not changed:
            break
    return out


def _resolve(callee: str, aliases: dict[str, str]) -> str:
    """*callee* with the longest part of it the file bound replaced by what it stands for."""
    return _lookup(callee, aliases) or callee


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


def _shell_text(node: ast.AST) -> str | None:
    """A command line given as a string (a constant, an f-string or a ``+`` concatenation),
    each part only known at run time read as one unknown word; ``None`` when none of it is a
    constant."""
    parts: list[ast.AST] = []
    stack = [node]
    while stack:
        part = stack.pop()
        if isinstance(part, ast.BinOp) and isinstance(part.op, ast.Add):
            stack += [part.right, part.left]
        elif isinstance(part, ast.JoinedStr):
            stack += reversed(part.values)
        else:
            parts.append(part)
    texts = [_text(part) for part in parts]
    if all(text is None for text in texts):
        return None
    return "".join(UNKNOWN_WORD if text is None else text for text in texts)


def _closing(line: str, start: int) -> int:
    """Where the parenthesis open just before *start* closes, quotes honoured; the line's end
    when it never does."""
    depth, quote, at = 1, "", start
    while at < len(line):
        char = line[at]
        if char == "\\" and quote != "'":
            at += 2
            continue
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in "()":
            depth += 1 if char == "(" else -1
            if not depth:
                return at
        at += 1
    return len(line)


def _substitutions(line: str) -> tuple[str, list[str]]:
    """*line* with each command substitution the shell runs read as one unknown word of its
    command, and the command lines substituted: ``$(…)`` and backticks, which double quotes do
    not stop, and ``<(…)`` and ``>(…)``. Inside single quotes nothing is substituted."""
    outer: list[str] = []
    inner: list[str] = []
    quote, at = "", 0
    while at < len(line):
        char = line[at]
        if quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\":
            outer.append(line[at : at + 2])
            at += 2
            continue
        elif char in "'\"" and quote in ("", char):
            quote = "" if quote else char
        elif char == "`":
            end = at + 1
            while end < len(line) and line[end] != "`":
                end += 2 if line[end] == "\\" else 1
            inner.append(line[at + 1 : end])
            outer.append(UNKNOWN_WORD)
            at = end + 1
            continue
        elif line.startswith("$(", at) or (not quote and line.startswith(("<(", ">("), at)):
            end = _closing(line, at + 2)
            inner.append(line[at + 2 : end])
            outer.append(UNKNOWN_WORD)
            at = end + 1
            continue
        outer.append(char)
        at += 1
    return "".join(outer), inner


def _shell_commands(line: str) -> list[list[str]]:
    """The words of each command on a shell line, split where the shell splits them (``&&``,
    ``||``, ``;``, ``|``, ``&``, a newline), its quoting honoured: a newline inside quotes, or
    escaped by a backslash, ends no command. The commands a substitution runs are read too."""
    line, substituted = _substitutions(line.replace("\\\n", ""))
    lexer = shlex.shlex(line, posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace = " \t\r"  # a newline is punctuation here, not space
    lexer.commenters = ""  # a comment would swallow the newline that ends it
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # an unclosed quote: read it word by word, a line at a time
        tokens = [word for piece in line.splitlines() for word in [*piece.split(), "\n"]]
    commands: list[list[str]] = []
    words: list[str] = []
    for token in tokens:
        if set(token) <= set(PUNCTUATION) and set(token) & SEPARATORS:
            commands += [words] if words else []
            words = []
        elif token.strip("()"):  # a subshell's parentheses are not words
            words.append(token)
    commands += [words] if words else []
    for inner in substituted:
        commands += _shell_commands(inner)
    return commands


def _git_subcommand(args: list[str]) -> str:
    """Git's subcommand: its first word past the options (``-C path``, ``-c key=value`` …)."""
    takes_value = False
    for word in args:
        if takes_value:
            takes_value = False
        elif word in GIT_VALUE_OPTIONS:
            takes_value = True
        elif not word.startswith("-"):
            return word
    return ""


def _dash_c(args: list[str]) -> str | None:
    """The script a shell is handed with ``-c`` (``-c``, ``-lc``, ``-ec`` …): the first word
    past its options, ``-o pipefail`` and the like skipped with their value; or ``None``."""
    dash_c = takes_value = False
    for word in args:
        if takes_value:
            takes_value = False
        elif word.startswith("-") and word != "--":
            short = not word.startswith("--")
            dash_c = dash_c or (short and "c" in word[1:])
            takes_value = short and word[-1] in "oO"
        elif word != "--":
            return word if dash_c else None
    return None


def _basename(word: str) -> str:
    """A program's name however it is given: ``/usr/bin/ssh`` and ``./bin/ssh`` are ``ssh``."""
    return word.rsplit("/", 1)[-1]


def _program_at(words: list[str]) -> int | None:
    """Where a command's program is among its *words*: past ``VAR=value`` assignments and
    simple prefixes (``env``, ``sudo -u deploy`` …) with their options; ``None`` when it runs
    none."""
    at = 0
    while at < len(words):
        if ASSIGNMENT.match(words[at]):
            at += 1
            continue
        prefix = _basename(words[at])
        if prefix not in SHELL_PREFIXES:
            return at
        at += 1
        if prefix == "command" and words[at : at + 1] in (["-v"], ["-V"]):
            return None  # `command -v x` asks where x is, and runs nothing
        while at < len(words) and (words[at].startswith("-") or ASSIGNMENT.match(words[at])):
            # an option's value is not the program either
            at += 2 if words[at] in PREFIX_VALUE_OPTIONS.get(prefix, ()) else 1
    return None


def _command_signals(words: list[str], nested: bool = False) -> list[str]:
    """What one command runs, its program read by basename; a shell's ``-c`` script is read one
    level down."""
    at = _program_at(words)
    if at is None:
        return []
    program = _basename(words[at])
    if program in REMOTE_PROGRAMS:
        return [f"starts {program}"]
    if program == "git":
        verb = _git_subcommand(words[at + 1 :])
        return [f"runs git {verb}"] if verb in GIT_REMOTE_VERBS else []
    script = _dash_c(words[at + 1 :]) if program in SHELLS and not nested else None
    found: list[str] = []
    for command in _shell_commands(script) if script is not None else []:
        found += _command_signals(command, nested=True)
    return found


def _shell_signals(line: int, command: ast.AST) -> list[tuple[int, str]]:
    """The signals a command line for a shell shows, from every command on it."""
    text = _shell_text(command)
    found: list[tuple[int, str]] = []
    for words in _shell_commands(text) if text is not None else []:
        found += [(line, signal) for signal in _command_signals(words)]
    return found


def _anchored(node: ast.AST, own: set[str]) -> bool:
    """Whether a path is built on ``__file__``, directly or through a name bound to one."""
    return any(
        isinstance(part, (ast.Name, ast.Attribute))
        and (_dotted(part) == "__file__" or _dotted(part) in own)
        for part in ast.walk(node)
    )


def _own_paths(tree: ast.AST) -> set[str]:
    """The names bound to a path built on ``__file__`` (``HERE = Path(__file__).parent``,
    ``PLUGIN = HERE / "x.py"``): the app's own files, followed to a fixed point."""
    own: set[str] = set()
    pairs = _assigned(tree)
    changed = True
    while changed:
        changed = False
        for name, value in pairs:
            if name not in own and _anchored(value, own):
                own.add(name)
                changed = True
    return own


#: How a file's module specs are used: each spec-making call's bound names (by the call's
#: ``id``), the names whose spec is executed, and the spec-making calls executed in place.
Specs = tuple[dict[int, list[str]], set[str], set[int]]


def _specs(tree: ast.AST, aliases: dict[str, str]) -> Specs:
    """A spec is executed by ``module_from_spec(spec)`` or ``spec.loader.exec_module(…)``
    (``load_module``), by the name it is bound to or in place."""
    made: dict[int, list[str]] = {}
    executed: set[str] = set()
    inline: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _resolve(_callee(node.value), aliases) in SPEC_MAKERS:
                pairs = [pair for target in node.targets for pair in _pairs(target, node.value)]
                made[id(node.value)] = [name for name, _value in pairs]
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node)
        if _resolve(callee, aliases) == MODULE_FROM_SPEC and node.args:
            spec = node.args[0]
            if isinstance(spec, ast.Call):
                inline.add(id(spec))
            elif _dotted(spec):
                executed.add(_dotted(spec))
        for suffix in (".loader.exec_module", ".loader.load_module"):
            if callee.endswith(suffix):
                executed.add(callee[: -len(suffix)])
    return made, executed, inline


def _loader_signals(
    node: ast.Call, callee: str, package: str, aliases: dict[str, str], own: set[str],
    specs: Specs,
) -> list[str]:
    """What an ``importlib.util`` / ``importlib.machinery`` / ``runpy`` load shows.

    A module named by a constant is judged like an import; code from a path not built on
    ``__file__`` is unknown. A ``find_spec`` whose spec is never executed only asks whether a
    module is there, and says nothing; a ``module_from_spec`` of a spec made elsewhere loads a
    module only known at run time."""
    made, executed, inline = specs
    if callee in PATH_LOADERS:
        position, keyword = PATH_LOADERS[callee]
        path = _argument(node, position, keyword)
        return [] if path is not None and _anchored(path, own) else [RUNTIME_PATH]
    if callee == MODULE_FROM_SPEC and node.args:
        spec = node.args[0]
        if isinstance(spec, ast.Call):
            here = _resolve(_callee(spec), aliases) in SPEC_MAKERS
        else:
            here = any(_dotted(spec) in names for names in made.values())
        return [] if here else [RUNTIME_NAME]
    if callee == FIND_SPEC:
        if id(node) not in inline and not any(n in executed for n in made.get(id(node), [])):
            return []
        name = _text(_argument(node, 0, "name"))
        if name is not None and name.startswith("."):
            name = _relative(name, _text(_argument(node, 1, "package")) or package)
    elif callee == RUN_MODULE:
        name = _text(_argument(node, 0, "mod_name"))
    else:
        return []
    if name is None:
        return [RUNTIME_NAME]
    signal = _import_signal(name)
    return [signal] if signal else []


#: Every value each name in a file is bound to.
Bindings = dict[str, list[ast.AST]]


def _bindings(tree: ast.AST) -> Bindings:
    """Every value each name is bound to, by an assignment or a ``with … as``, at any scope."""
    pairs = _assigned(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    pairs += _pairs(item.optional_vars, item.context_expr)
    out: Bindings = {}
    for name, value in pairs:
        out.setdefault(name, []).append(value)
    return out


def _read_path(node: ast.AST, aliases: dict[str, str], bindings: Bindings) -> ast.AST | None:
    """The path whose text *node* reads (``open(p).read()``, ``Path(p).read_text()``, or
    ``f.read()`` after ``with open(p) as f``), or ``None`` when it reads no file."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    receiver, method = node.func.value, node.func.attr
    if method in ("read_text", "read_bytes"):
        return receiver
    if method != "read":
        return None
    name = _dotted(receiver)
    for opened in [receiver, *(bindings.get(name, []) if name else [])]:
        if not isinstance(opened, ast.Call):
            continue
        if _resolve(_callee(opened), aliases) in OPENS:
            return _argument(opened, 0, "file") or opened
        if isinstance(opened.func, ast.Attribute) and opened.func.attr == "open":
            return opened.func.value  # Path(p).open()
    return None


def _source_signals(
    node: ast.AST, package: str, aliases: dict[str, str], own: set[str], bindings: Bindings,
    seen: frozenset[str] = frozenset(),
) -> list[str]:
    """What running the code *node* holds shows (``exec``, ``eval``), ``compile`` looked through
    and a name followed to what it is bound to: a constant is read as code like the rest of the
    file, and a file's text is code from a path only known at run time unless that path is
    built on ``__file__``."""
    if isinstance(node, ast.Call) and node.args:
        if _resolve(_callee(node), aliases) in COMPILES:
            return _source_signals(node.args[0], package, aliases, own, bindings, seen)
    text = _text(node)
    if text is not None:
        try:
            return [signal for _line, signal in signals(text, package)]
        except (SyntaxError, ValueError):
            return []  # exec raises before it runs any of it
    name = _dotted(node)
    if name:
        found: list[str] = []
        for value in bindings.get(name, []) if name not in seen else []:
            found += _source_signals(value, package, aliases, own, bindings, seen | {name})
        return list(dict.fromkeys(found))
    path = _read_path(node, aliases, bindings)
    if path is None:
        return []
    return [] if _anchored(path, own) else [RUNTIME_PATH]


def signals(source: str, package: str = "") -> list[tuple[int, str]]:
    """``(line, signal)`` for every sign in *source* that its code reaches the network.
    *package* is the file's own package, for a relative name imported at run time."""
    tree = ast.parse(source)
    aliases = _aliases(tree)
    own = _own_paths(tree)
    specs = _specs(tree, aliases)
    bindings: Bindings | None = None  # read only for a file that runs code with exec
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    # A function or class the module defines under a builtin's name is not that builtin.
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    found: list[tuple[int, str]] = []
    spawns = mentions_git = False
    constants: list[tuple[int, str]] = []
    argvs: list[list[ast.expr]] = []
    for node in ast.walk(tree):
        for dotted in _imported(node):
            signal = _import_signal(dotted)
            if signal:
                found.append((node.lineno, signal))
        if isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in called:
            # A spawn handed on as a callable (run_in_executor, to_thread, partial, submit)
            # starts programs as surely as one called by name.
            if _resolve(_dotted(node), aliases) in SPAWNS:
                spawns = True
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            argvs.append(node.elts)
        if isinstance(node, ast.Call):
            callee = _resolve(_callee(node), aliases)
            loaded = _loader_signals(node, callee, package, aliases, own, specs)
            found += [(node.lineno, signal) for signal in loaded]
            if callee in EXECS and callee not in defined and node.args:
                bindings = _bindings(tree) if bindings is None else bindings
                ran = _source_signals(node.args[0], package, aliases, own, bindings)
                found += [(node.lineno, signal) for signal in ran]
            if callee in SPAWNS:
                spawns = True
                shell = callee in SHELL_SPAWNS or any(
                    k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in node.keywords
                )
                if shell and node.args:
                    found += _shell_signals(node.lineno, node.args[0])
                elif callee in EXEC_SPAWNS:
                    argvs.append(node.args)
                elif node.args and _text(node.args[0]) is not None:
                    argvs.append(node.args[:1])  # a program given alone, as a string
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
        # An argv's program given by path is read by its basename, as on a shell line; one
        # named bare is read with the file's constants below.
        for elts in argvs:
            words = [_text(elt) or UNKNOWN_WORD for elt in elts]
            at = _program_at(words)
            if at is None or "/" not in words[at]:
                continue
            program = _basename(words[at])
            mentions_git = mentions_git or program == "git"
            if program in REMOTE_PROGRAMS:
                found.append((elts[at].lineno, f"starts {program}"))
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
) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, str]]], list[str]]:
    """``app → ["file:line: signal", …]`` for every app whose code shows a signal,
    ``app → [("file:line", what), …]`` for every import whose module name, or load whose path,
    is only known at run time, and the files that could not be read."""
    out: dict[str, list[str]] = {}
    unknown: dict[str, list[tuple[str, str]]] = {}
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
                if signal in UNKNOWN_STEPS:
                    unknown.setdefault(bundle.name, []).append((f"{rel}:{line}", signal))
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
        # every command of a shell line, past assignments, prefixes and one `sh -c`
        ("subprocess.run('cd repo && git push', shell=True)\n", ["runs git push"]),
        ("subprocess.run('make; ssh host deploy', shell=True)\n", ["starts ssh"]),
        ("subprocess.run('tar cz . | curl -T - x', shell=True)\n", ["starts curl"]),
        ("subprocess.run('build &\\nwget x', shell=True)\n", ["starts wget"]),
        ("subprocess.run('FOO=1 nohup rsync -a a b', shell=True)\n", ["starts rsync"]),
        ("subprocess.run('env -i A=1 /usr/bin/ssh h', shell=True)\n", ["starts ssh"]),
        ("subprocess.run(\"sh -c 'git fetch origin'\", shell=True)\n", ["runs git fetch"]),
        ("subprocess.run(f'(cd {repo} && git -C {repo} push)', shell=True)\n", ["runs git push"]),
        ("subprocess.run('echo \"git push\"', shell=True)\n", []),
        ("subprocess.run('git commit -m push', shell=True)\n", []),
        ("subprocess.run('command -v ssh', shell=True)\n", []),
        # a quoted or escaped newline ends no command, and an option's value is not the program
        ("subprocess.run(\"sh -c 'cd repo\\ngit push'\", shell=True)\n", ["runs git push"]),
        ("subprocess.run(\"echo 'built\\nssh host'\", shell=True)\n", []),
        (r"subprocess.run('git \\\n  push origin', shell=True)", ["runs git push"]),
        ("subprocess.run('sudo -u deploy rsync -a a b', shell=True)\n", ["starts rsync"]),
        ("subprocess.run('env -C /srv/ssh make', shell=True)\n", []),
        ("subprocess.run(\"bash -euo pipefail -c 'git push'\", shell=True)\n", ["runs git push"]),
        ("subprocess.run('bash -o pipefail deploy.sh', shell=True)\n", []),
        # a command substitution is one word of its command, and its commands are read too
        ("subprocess.run('echo $(ssh host uptime)', shell=True)\n", ["starts ssh"]),
        ("subprocess.run('x=`git fetch`', shell=True)\n", ["runs git fetch"]),
        ("subprocess.run('echo \"$(curl -s x)\"', shell=True)\n", ["starts curl"]),
        ("subprocess.run('git -C $(pwd) push', shell=True)\n", ["runs git push"]),
        ("subprocess.run('diff <(ssh h cat f) f', shell=True)\n", ["starts ssh"]),
        ("subprocess.run(\"echo '$(ssh host)' '`ssh host`'\", shell=True)\n", []),
        (r"subprocess.run('echo \\`ssh host\\`', shell=True)", []),
        ("subprocess.run('echo \"<(ssh h)\"', shell=True)\n", []),
        # an argv's program given by path is read by its basename, and only its program
        ("import subprocess\nsubprocess.run(['/usr/bin/ssh', host])\n", ["starts ssh"]),
        ("import subprocess\nsubprocess.run(['./bin/rsync', '-a', a, b])\n", ["starts rsync"]),
        (
            "import asyncio\nasyncio.create_subprocess_exec('/opt/homebrew/bin/gh', 'pr')\n",
            ["starts gh"],
        ),
        ("import subprocess\nsubprocess.Popen('/usr/local/bin/wget')\n", ["starts wget"]),
        ("import subprocess\nsubprocess.run(['/usr/bin/git', 'push'])\n", ["runs git push"]),
        (
            "import subprocess\nsubprocess.run(['/usr/bin/env', 'A=1', '/usr/bin/scp', a, b])\n",
            ["starts scp"],
        ),
        ("import subprocess\nsubprocess.run(['/srv/ssh-backup/tool'])\n", []),
        ("import subprocess\nsubprocess.run(['cp', '/usr/bin/ssh', dst])\n", []),
        ("import subprocess\nsubprocess.run(['ls', '/etc/ssh'])\nDATA = '/srv/rsync'\n", []),
        ("KNOWN = ['/usr/bin/ssh']\n", []),
        # a spawn bound by assignment, at any scope, and called through the name
        ("import subprocess\nrun = subprocess.run\nrun(['ssh', host])\n", ["starts ssh"]),
        (
            "import subprocess\nclass C:\n    def __init__(self):\n"
            "        self._run = subprocess.run\n"
            "    def go(self, host):\n        self._run(['rsync', host])\n",
            ["starts rsync"],
        ),
        ("import os\nsh = os.system\nsh('ssh host uptime')\n", ["starts ssh"]),
        (
            "import functools, subprocess\nrun = functools.partial(subprocess.run, check=True)\n"
            "run(['scp', a, b])\n",
            ["starts scp"],
        ),
        ("import subprocess as sp\nr = sp.run\nrun = r\nrun(['ssh', host])\n", ["starts ssh"]),
        ("run = self.run\nrun(['ssh', host])\n", []),
        # a spawn handed on as a callable
        (
            "import subprocess\nloop.run_in_executor(None, subprocess.run, ['ssh', host])\n",
            ["starts ssh"],
        ),
        (
            "import asyncio, subprocess\nasyncio.to_thread(subprocess.run, ['gh', 'pr'])\n",
            ["starts gh"],
        ),
        (
            "import subprocess\npool.submit(subprocess.check_output, ['curl', url])\n",
            ["starts curl"],
        ),
        ("import subprocess\nERROR = subprocess.CalledProcessError\nARGV = ['ssh']\n", []),
        # importlib.util, importlib.machinery and runpy loads
        (
            "import importlib.util\nspec = importlib.util.find_spec('httpx')\n"
            "mod = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(mod)\n",
            ["imports httpx (an HTTP client)"],
        ),
        (
            "from importlib.util import find_spec, module_from_spec\n"
            "module_from_spec(find_spec('socket'))\n",
            ["imports socket (sockets)"],
        ),
        ("import importlib.util\nimportlib.util.find_spec('httpx')\n", []),
        (
            "import importlib.util\nspec = importlib.util.find_spec('x')\nok = spec is not None\n",
            [],
        ),
        (
            "import importlib.util\nspec = importlib.util.spec_from_file_location('p', path)\n",
            [RUNTIME_PATH],
        ),
        (
            "import importlib.util\nfrom pathlib import Path\nHERE = Path(__file__).parent\n"
            "spec = importlib.util.spec_from_file_location('p', HERE / 'plugin.py')\n",
            [],
        ),
        (
            "import importlib.machinery\n"
            "importlib.machinery.SourceFileLoader('p', path).load_module()\n",
            [RUNTIME_PATH],
        ),
        ("import runpy\nrunpy.run_module('httpx')\n", ["imports httpx (an HTTP client)"]),
        ("import runpy\nrunpy.run_module(name)\n", [RUNTIME_NAME]),
        ("import runpy\nrunpy.run_path(path)\n", [RUNTIME_PATH]),
        ("import importlib.util\nmodule_from_spec = importlib.util.module_from_spec\n"
         "module_from_spec(handed_in)\n", [RUNTIME_NAME]),
        # code run by exec or eval: a constant is read as code, and a file's text is code from a
        # path only known at run time, unless the path is built on __file__
        ("exec(open(path).read())\n", [RUNTIME_PATH]),
        ("exec(compile(open(path).read(), path, 'exec'))\n", [RUNTIME_PATH]),
        ("from pathlib import Path\nexec(Path(path).read_text())\n", [RUNTIME_PATH]),
        ("with open(path) as f:\n    exec(f.read(), {})\n", [RUNTIME_PATH]),
        (
            "src = Path(path).read_text()\ncode = compile(src, path, 'exec')\neval(code)\n",
            [RUNTIME_PATH],
        ),
        ("exec('import socket')\n", ["imports socket (sockets)"]),
        (
            "from pathlib import Path\nHERE = Path(__file__).parent\n"
            "exec((HERE / 'plugin.py').read_text())\n",
            [],
        ),
        ("exec(open(os.path.join(os.path.dirname(__file__), 'x.py')).read())\n", []),
        ("exec('x = 1')\nexec('def (')\n", []),
        ("open(path).read()\n", []),
        ("sandbox.exec(open(path).read())\n", []),
        ("def exec(command):\n    pass\nexec(open(path).read())\n", []),
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
    # An app that declares network is not made wrong by code the rail cannot name.
    for app, sites in sorted(unknown.items()):
        if app in EXEMPT or declares_network(manifests[app]):
            continue
        for what, step in UNKNOWN_STEPS.items():
            where = [site for site, kind in sites if kind == what]
            if where:
                found.append(
                    f"{app}: {', '.join(where)} {what}, so whether {app} reaches the network is "
                    f"unknown. {step}, declare \"network\": true, or say in EXEMPT why it reaches "
                    "no network"
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
