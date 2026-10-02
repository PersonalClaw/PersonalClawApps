#!/usr/bin/env python3
"""Repo rail: every program an app's code starts is declared under ``launches``.

Install consent's "What it runs on this machine" names each program an app starts outside
PersonalClaw, what the app uses it for and what of the owner's it runs with, from the manifest's
``launches``: an ``npx`` entry also names the npm package npx downloads and runs, and an entry names
the hosts its program reaches. So an app whose code starts a program its manifest leaves out shows
the owner a review without it, and only the security scan's warning that some code "runs an external
program" says anything runs at all. This rail fails that app by name, with the program and where it
starts.

What counts as starting a program, read on the AST of each bundle's own code (its tests,
``conftest.py`` and hidden folders left out): ``subprocess.run``, ``Popen``, ``call``,
``check_output`` and ``check_call``, ``asyncio.create_subprocess_exec`` and ``_shell``,
``os.system``, ``os.popen``, the ``os.exec*``, ``os.spawn*`` and ``os.posix_spawn*`` calls and
``pty.spawn``. Each is recognised by what it is through the file's imports and assignments, whatever
name it was imported or bound as, by the network rail's own reader (``check_network_declared.py``),
so the two rails agree on what a spawn is. One handed on as a callable (``run_in_executor(None,
subprocess.run, argv)``, ``to_thread``, ``submit``) is read with the arguments after it.

The program a spawn starts is read from what it is handed:

* a string: a program's name, or a path read by its basename (``/usr/bin/open`` is ``open``); a
  command line for a shell is read command by command, as the network rail reads it;
* a list or tuple: its first item, past ``VAR=value`` and prefixes such as ``env``, as on a shell
  line (``sudo`` counts as a program of its own); ``[*prefix, …]``, ``*argv``, ``argv + […]``;
* ``git_argv(…)`` (``personalclaw.sdk.git``): ``git``; ``find_ffmpeg()`` and ``find_ffprobe()``:
  ``ffmpeg`` and ``ffprobe``; ``shutil.which("x")``: ``x``; ``list(x)``, ``str(x)``,
  ``os.path.expanduser(x)`` and the like: what ``x`` names; ``shlex.split(line)``: the program
  of that one command line; and a subprocess call's ``executable=``, in place of its argv's;
* a name: every value the enclosing function, the class (``self.x``) or the module binds it to,
  including a row of a constant table a ``for`` walks; each side of ``x or y`` and of
  ``a if c else b``; for a parameter of the enclosing function, its default and what each call of
  that function in the same file passes for it.

A spawn whose program cannot be read that way fails by name, unless ``READ_BY_HAND`` says which
programs it starts and why. ``*`` there is the programs the owner names for an app (a runbook
action they wrote), which the manifest declares as a ``launches`` entry whose program is ``*``; it
covers no program the app names itself. Each program a hand entry names must be a string in that
file, and a hand entry that matches no spawn, or one the reader can now read, fails as stale.

For ``npx`` the package is read as well, the first word past its options, and it must be the
``npmPackage`` of the app's ``npx`` entry exactly: that entry's consent says npx fetches the newest
version of that package each time, so a pinned, different or unreadable package fails.

**Vacuity floor.** A rail that matches nothing reads as clean, so the reader is checked against
every shape it tells apart before anything is read, and each app in ``KNOWN_LAUNCHES`` must be seen
starting the program it is named for: one app per way of reading a program.

Run locally exactly as CI does:

    python .github/scripts/check_launches_declared.py
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import check_network_declared as net  # noqa: E402 — the network rail's spawn and shell readers

#: The program of a ``launches`` entry for the programs the owner names for an app.
YOU_NAME = "*"
#: The program that downloads an npm package and runs it, which its entry's ``npmPackage`` names.
NPX = "npx"

#: Spawns handed an argv: a list whose first item is the program, or a program alone as a string.
ARGV_FIRST = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_output",
        "subprocess.check_call",
        "pty.spawn",
    }
)

#: Spawns handed the program and then its arguments, by where the program is.
PROGRAM_AT: dict[str, int] = {
    **{name: 0 for name in net.EXEC_SPAWNS},
    **{
        f"os.{name}": 0
        for name in (
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "posix_spawn",
            "posix_spawnp",
        )
    },
    **{
        f"os.{name}": 1
        for name in (
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
        )
    },
}

#: Every spawn. The shell ones (and any with ``shell=True``) take a command line for a shell.
SPAWNS = ARGV_FIRST | frozenset(PROGRAM_AT) | net.SHELL_SPAWNS

#: Calls, by their last name, whose result is the argv of a program, or the path of one.
ARGV_BUILDERS = {"git_argv": "git"}
PROGRAM_FINDERS = {"find_ffmpeg": "ffmpeg", "find_ffprobe": "ffprobe"}
WHICH = "shutil.which"

#: Calls that hand back what they are handed, as far as which program it names goes.
PASS_THROUGH = frozenset(
    {
        "list",
        "tuple",
        "str",
        "os.fspath",
        "os.path.expanduser",
        "os.path.expandvars",
        "os.path.abspath",
        "os.path.realpath",
        "pathlib.Path",
        "pathlib.PurePath",
        "pathlib.PosixPath",
    }
)
#: A call that splits one command line into its argv, which is read like a shell line's command.
SPLIT = "shlex.split"
#: A call that binds a spawn to a name rather than starting it.
PARTIAL = "functools.partial"

#: The one prefix on a command line that is a program in its own right: the owner should know a
#: program runs as someone else.
SUDO = "sudo"

#: Spawns the reader cannot read, by ``bundle/file::qualname::spawn``: the programs each starts,
#: and why the code cannot say so where it starts it.
READ_BY_HAND: dict[str, tuple[tuple[str, ...], str]] = {
    "lima-sandbox/provider.py::LimaSandboxHandle.exec::asyncio.create_subprocess_exec": (
        ("limactl",),
        "the `limactl shell` launch the provider's wrap() builds and hands the handle",
    ),
    "menu-bar-companion/menubar_companion/notify.py::_default_runner::subprocess.run": (
        ("osascript",),
        "the runner the notifier hands its osascript argv to",
    ),
    "ops/runbooks.py::run_action::subprocess.run": (
        (YOU_NAME,),
        "a runbook action's own argv, which the operator's runbook declares",
    ),
    "piper-tts/provider.py::_synthesize_piper_chunk::asyncio.create_subprocess_exec": (
        ("piper",),
        "the piper _piper_command() finds, wrapped in the host sandbox by sandbox_wrap_argv",
    ),
}

#: The vacuity floor: one app per way of reading a program, each of which must be seen with it.
KNOWN_LAUNCHES = {
    "menu-bar-companion": "open",  # a path, read by its basename
    "code-review": "gh",  # a name bound to a list, handed on starred
    "diarization-onnx": "ffmpeg",  # find_ffmpeg()
    "git-repo": "git",  # git_argv(…)
    "skills-sh": NPX,  # shutil.which("npx"), and the package it fetches
    "lima-sandbox": "limactl",  # a module constant
    "rsync-sync": "rsync",  # an attribute set from a parameter's default
    "issue-radar": "glab",  # a parameter, read from the calls that pass it, and a constant table
    "ops": YOU_NAME,  # read by hand
}


@dataclass(frozen=True)
class Launch:
    """One program a spawn site starts. ``package`` is what an ``npx`` fetches: ``""`` for any
    other program, and ``None`` when it cannot be read."""

    program: str
    package: str | None = ""


#: A function, and a value with the function it is evaluated in (``None`` at module level).
FuncNode = ast.FunctionDef | ast.AsyncFunctionDef
Bound = tuple[ast.AST, FuncNode | None]


class Reader:
    """What the spawns of one file start."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.aliases = net._aliases(tree)
        self.parent: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parent[id(child)] = node

    # ── where a node is ──────────────────────────────────────────────────────────────────────

    def enclosing(self, node: ast.AST, kinds: tuple[type, ...]) -> ast.AST | None:
        here = self.parent.get(id(node))
        while here is not None and not isinstance(here, kinds):
            here = self.parent.get(id(here))
        return here

    def function_of(self, node: ast.AST):
        return self.enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))

    def qualname(self, node: ast.AST) -> str:
        names: list[str] = []
        here = self.parent.get(id(node))
        while here is not None:
            if isinstance(here, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(here.name)
            here = self.parent.get(id(here))
        return ".".join(reversed(names)) or "<module>"

    def resolved(self, call: ast.Call) -> str:
        return net._resolve(net._callee(call), self.aliases)

    # ── what a name is bound to ──────────────────────────────────────────────────────────────

    def _scope_nodes(self, scope: ast.AST) -> Iterator[ast.AST]:
        """Every node of *scope*: a function's whole body, or the module's own statements, not
        what its functions and classes hold."""
        if not isinstance(scope, ast.Module):
            yield from ast.walk(scope)
            return
        stack: list[ast.AST] = list(scope.body)
        while stack:
            node = stack.pop()
            yield node
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack.extend(ast.iter_child_nodes(node))

    @staticmethod
    def _binds(target: ast.AST, name: str) -> bool:
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(Reader._binds(t, name) for t in target.elts)
        if isinstance(target, ast.Starred):
            return Reader._binds(target.value, name)
        return net._dotted(target) == name

    def bindings(self, scope: ast.AST, name: str) -> tuple[list[Bound], bool]:
        """``(values, unreadable)``: what *scope* binds *name* (``x`` or ``self.x``) to, each with
        the function it is evaluated in, and whether it also binds it some way the rail cannot
        follow (a tuple unpacked from a call, ``with … as``, a ``for`` over something that is not a
        constant table)."""
        values: list[Bound] = []
        unreadable = False
        for node in self._scope_nodes(scope):
            func = self.function_of(node) if not isinstance(scope, ast.Module) else None
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    pairs = [v for n, v in net._pairs(target, node.value) if n == name]
                    if pairs:
                        values += [(v, func) for v in pairs]
                    elif self._binds(target, name):
                        unreadable = True
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if net._dotted(node.target) == name:
                    values.append((node.value, func))
            elif isinstance(node, ast.NamedExpr) and node.target.id == name:
                values.append((node.value, func))
            elif isinstance(node, (ast.For, ast.AsyncFor)) and self._binds(node.target, name):
                rows = self._table_rows(node, name, func)
                if rows is None:
                    unreadable = True
                else:
                    values += rows
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None and self._binds(item.optional_vars, name):
                        unreadable = True
        return values, unreadable

    def _table_rows(self, loop: ast.For | ast.AsyncFor, name: str, func) -> list[Bound] | None:
        """For ``for a, b in TABLE`` over a constant table: what each row gives *name*."""
        target = loop.target
        index = None
        if isinstance(target, (ast.Tuple, ast.List)):
            index = next((i for i, t in enumerate(target.elts) if net._dotted(t) == name), None)
            if index is None:
                return None
        tables = self._constant(loop.iter, func)
        if tables is None:
            return None
        rows: list[Bound] = []
        for table, table_func in tables:
            for row in table.elts:
                if index is None:
                    rows.append((row, table_func))
                elif isinstance(row, (ast.Tuple, ast.List)) and len(row.elts) == len(target.elts):
                    rows.append((row.elts[index], table_func))
                else:
                    return None
        return rows

    def _constant(
        self, node: ast.AST, func: FuncNode | None
    ) -> list[tuple[ast.List | ast.Tuple, FuncNode | None]] | None:
        """The list or tuple literals *node* is, through the names it is bound to."""
        if isinstance(node, (ast.List, ast.Tuple)):
            return [(node, func)]
        name = net._dotted(node)
        if not name or "." in name:
            return None
        scope = func if func is not None and self.bindings(func, name)[0] else self.tree
        values, unreadable = self.bindings(scope, name)
        if unreadable or not values:
            return None
        out: list[tuple[ast.List | ast.Tuple, FuncNode | None]] = []
        for value, value_func in values:
            found = self._constant(value, value_func)
            if found is None:
                return None
            out += found
        return out

    # ── what a value names ───────────────────────────────────────────────────────────────────

    def programs(self, node: ast.AST, func, seen: frozenset = frozenset()) -> set[str] | None:
        """The programs an argv, or a program on its own, names; ``None`` when it cannot be read."""
        if isinstance(node, ast.Starred):
            return self.programs(node.value, func, seen)
        if isinstance(node, (ast.List, ast.Tuple)):
            return self._argv_programs(node.elts, func, seen)
        text = net._text(node)
        if text is not None:
            return {net._basename(text)} if text.strip() else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.programs(node.left, func, seen)
        if isinstance(node, ast.BoolOp):
            return self._union((self.programs(v, func, seen) for v in node.values))
        if isinstance(node, ast.IfExp):
            return self._union(
                (self.programs(node.body, func, seen), self.programs(node.orelse, func, seen))
            )
        if isinstance(node, ast.Call):
            return self._call_programs(node, func, seen)
        name = net._dotted(node)
        if not name:
            return None
        key = (id(func), name)
        if key in seen:
            return None
        seen = seen | {key}
        if name.startswith(("self.", "cls.")) and name.count(".") == 1:
            return self._attribute_programs(node, name, seen)
        if "." in name:
            return None
        return self._name_programs(name, func, seen)

    def _argv_programs(self, words: list[ast.expr], func, seen) -> set[str] | None:
        if not words:
            return None
        if isinstance(words[0], ast.Starred):
            return self.programs(words[0].value, func, seen)
        texts = [net._text(w) or net.UNKNOWN_WORD for w in words]
        at = net._program_at(texts)
        if at is None:
            return None
        found = self.programs(words[at], func, seen)
        if found is None:
            return None
        return found | ({SUDO} if SUDO in map(net._basename, texts[:at]) else set())

    def _call_programs(self, call: ast.Call, func, seen) -> set[str] | None:
        callee = self.resolved(call)
        last = callee.rsplit(".", 1)[-1]
        if last in ARGV_BUILDERS:
            return {ARGV_BUILDERS[last]}
        if last in PROGRAM_FINDERS:
            return {PROGRAM_FINDERS[last]}
        if callee == WHICH:
            text = net._text(net._argument(call, 0, "cmd"))
            return {net._basename(text)} if text else None
        if callee in PASS_THROUGH and call.args:
            return self.programs(call.args[0], func, seen)
        if callee == SPLIT and call.args:
            found = self.shell_launches(call.args[0], func)
            return {launch.program for launch in found} if found else None
        return None

    def _name_programs(self, name: str, func, seen) -> set[str] | None:
        if func is not None:
            values, unreadable = self.bindings(func, name)
            if unreadable:
                return None
            if values:
                return self._union((self.programs(v, f, seen) for v, f in values))
            if name in self._parameters(func):
                return self._parameter_programs(func, name, seen)
            outer = self.function_of(func)
            if outer is not None:
                return self._name_programs(name, outer, seen)
        values, unreadable = self.bindings(self.tree, name)
        if unreadable or not values:
            return None
        return self._union((self.programs(v, f, seen) for v, f in values))

    def _attribute_programs(self, node: ast.AST, name: str, seen) -> set[str] | None:
        cls = self.enclosing(node, (ast.ClassDef,))
        if cls is None:
            return None
        values, unreadable = self.bindings(cls, name)
        if unreadable or not values:
            return None
        return self._union((self.programs(v, f, seen) for v, f in values))

    @staticmethod
    def _parameters(func) -> list[str]:
        a = func.args
        return [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]

    def _parameter_programs(self, func, name: str, seen) -> set[str] | None:
        """A parameter: its default, and what each call of the function in this file passes."""
        a = func.args
        positional = [*a.posonlyargs, *a.args]
        with_defaults = positional[len(positional) - len(a.defaults) :]
        defaults = dict(zip([p.arg for p in with_defaults], a.defaults))
        defaults.update({p.arg: d for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None})
        method = isinstance(self.parent.get(id(func)), ast.ClassDef) and not any(
            net._dotted(d) == "staticmethod" for d in func.decorator_list
        )
        names = [p.arg for p in positional]
        index = names.index(name) - (1 if method else 0) if name in names else None
        found: list[set[str] | None] = []
        if name in defaults:
            found.append(self.programs(defaults[name], self.function_of(func), seen))
        cls = self.parent.get(id(func))
        init = func.name == "__init__" and isinstance(cls, ast.ClassDef)
        called_as = cls.name if init else func.name
        for call in ast.walk(self.tree):
            if not isinstance(call, ast.Call) or net._callee(call).rsplit(".", 1)[-1] != called_as:
                continue
            given = next((k.value for k in call.keywords if k.arg == name), None)
            if given is None and index is not None and 0 <= index < len(call.args):
                given = call.args[index]
            if given is None:
                if name not in defaults:
                    return None
                continue
            found.append(self.programs(given, self.function_of(call), seen))
        if not found:
            return None
        return self._union(found)

    @staticmethod
    def _union(parts: Iterable[set[str] | None]) -> set[str] | None:
        out: set[str] = set()
        for part in parts:
            if part is None:
                return None
            out |= part
        return out or None

    # ── a command line for a shell ───────────────────────────────────────────────────────────

    def shell_launches(self, node: ast.AST, func) -> list[Launch] | None:
        texts: list[str] = []
        text = net._shell_text(node)
        if text is not None:
            texts.append(text)
        else:
            name = net._dotted(node)
            if not name or "." in name:
                return None
            scope = func if func is not None and self.bindings(func, name)[0] else self.tree
            values, unreadable = self.bindings(scope, name)
            if unreadable or not values:
                return None
            for value, _ in values:
                value_text = net._shell_text(value)
                if value_text is None:
                    return None
                texts.append(value_text)
        out: list[Launch] = []
        for line in texts:
            for words in net._shell_commands(line):
                found = _command_launches(words)
                if found is None:
                    return None
                out += found
        return out


def _package_after(words: list[str]) -> str | None:
    """What ``npx`` fetches: the first of its words that is not an option, or the value of
    ``-p``/``--package``; ``None`` when that word is only known at run time."""
    takes_value = False
    for word in words:
        if word == net.UNKNOWN_WORD:
            return None
        if takes_value:
            return word
        if word in ("-p", "--package"):
            takes_value = True
        elif word.startswith("--package="):
            return word.split("=", 1)[1]
        elif not word.startswith("-"):
            return word
    return None


def _command_launches(words: list[str], nested: bool = False) -> list[Launch] | None:
    """What one command of a shell line starts, its program read by basename; a shell's ``-c``
    script is read one level down."""
    at = net._program_at(words)
    if at is None:
        return []
    program = net._basename(words[at])
    if program == net.UNKNOWN_WORD:
        return None
    out = [Launch(SUDO)] if SUDO in map(net._basename, words[:at]) else []
    script = net._dash_c(words[at + 1 :]) if program in net.SHELLS and not nested else None
    if script is not None:
        for command in net._shell_commands(script):
            found = _command_launches(command, nested=True)
            if found is None:
                return None
            out += found
        return out
    package = _package_after(words[at + 1 :]) if program == NPX else ""
    return [*out, Launch(program, package)]


def _words(nodes: list[ast.expr]) -> list[str]:
    return [net._text(n) or net.UNKNOWN_WORD for n in nodes]


def site_launches(
    reader: Reader, call: ast.Call, spawn: str, args: list[ast.expr], keywords: list[ast.keyword]
) -> list[Launch] | None:
    """What one spawn starts, given the arguments it is handed; ``None`` when it cannot be read."""
    func = reader.function_of(call)
    shell = spawn in net.SHELL_SPAWNS or any(
        k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
        for k in keywords
    )
    first = args[0] if args else next((k.value for k in keywords if k.arg == "args"), None)
    if shell:
        return reader.shell_launches(first, func) if first is not None else None
    if spawn in PROGRAM_AT:
        at = PROGRAM_AT[spawn]
        if len(args) <= at:
            return None
        program, rest = args[at], args[at + 1 :]
        argv = None if isinstance(program, ast.Starred) else [program, *rest]
        source = program.value if isinstance(program, ast.Starred) else program
    else:
        if first is None:
            return None
        argv, source = None, first
        executable = next((k.value for k in keywords if k.arg == "executable"), None)
        if executable is not None:
            # subprocess runs `executable` and hands it the argv: that is the program it starts.
            argv, source = None, executable
    programs = reader.programs(source, func)
    if programs is None:
        return None
    if NPX not in programs:
        return [Launch(p) for p in sorted(programs)]
    words = argv if argv is not None else _literal_argv(reader, source, func)
    package = None
    if words is not None:
        texts = _words(words)
        at = net._program_at(texts) or 0
        package = _package_after(texts[at + 1 :])
    return [Launch(p, package if p == NPX else "") for p in sorted(programs)]


def _literal_argv(reader: Reader, node: ast.AST, func) -> list[ast.expr] | None:
    """The words of an argv given as a list literal, directly or through the one name it is bound
    to, so the package an ``npx`` fetches can be read; ``None`` otherwise."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return None if any(isinstance(e, ast.Starred) for e in node.elts) else list(node.elts)
    if isinstance(node, ast.Call) and reader.resolved(node) in PASS_THROUGH and node.args:
        return _literal_argv(reader, node.args[0], func)
    name = net._dotted(node)
    if func is None or not name or "." in name:
        return None
    values, unreadable = reader.bindings(func, name)
    if unreadable or len(values) != 1:
        return None
    return _literal_argv(reader, values[0][0], values[0][1])


def spawn_sites(source: str) -> list[tuple[str, int, list[Launch] | None, str]]:
    """``(qualname::spawn, line, launches or None, spawn)`` for every spawn in *source*."""
    tree = ast.parse(source)
    reader = Reader(tree)
    out: list[tuple[str, int, list[Launch] | None, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        spawn = reader.resolved(node)
        if spawn in SPAWNS:
            found = site_launches(reader, node, spawn, list(node.args), node.keywords)
            out.append((f"{reader.qualname(node)}::{spawn}", node.lineno, found, spawn))
            continue
        # A spawn handed on as a callable, with the arguments that follow it. A partial of one is
        # not a start: the calls through the name it is bound to are read as the spawn they are.
        if spawn == PARTIAL:
            continue
        for i, arg in enumerate(node.args):
            handed = net._resolve(net._dotted(arg), reader.aliases) if net._dotted(arg) else ""
            if handed in SPAWNS:
                rest = list(node.args[i + 1 :])
                found = site_launches(reader, node, handed, rest, node.keywords)
                out.append((f"{reader.qualname(node)}::{handed}", node.lineno, found, handed))
                break
    return out


def census(root: pathlib.Path = ROOT):
    """``(app → [(site key, "file:line", launches or None)], unreadable files)`` over every
    bundle's own code."""
    sites: dict[str, list[tuple[str, str, list[Launch] | None]]] = {}
    unreadable: list[str] = []
    for manifest in sorted(root.glob("*/app.json")):
        bundle = manifest.parent
        for path in sorted(bundle.rglob("*.py")):
            if not net._is_bundle_code(path, bundle):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                found = spawn_sites(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                unreadable.append(f"{rel}: {exc}")
                continue
            for key, line, launches, _spawn in found:
                sites.setdefault(bundle.name, []).append(
                    (f"{rel}::{key}", f"{rel}:{line}", launches)
                )
    return sites, unreadable


def declared(manifest: pathlib.Path) -> dict[str, dict]:
    """The manifest's ``launches`` entries, by program."""
    raw = json.loads(manifest.read_text(encoding="utf-8")).get("launches")
    entries = raw if isinstance(raw, list) else []
    return {str(e.get("program", "")): e for e in entries if isinstance(e, dict)}


def _string_in(path: pathlib.Path, text: str) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    return any(net._text(node) == text for node in ast.walk(tree))


def _reader_problems() -> list[str]:
    """The positive control: the reader on every shape it has to tell apart."""
    cases: list[tuple[str, list[tuple[str, str | None]] | None]] = [
        # a string, a path read by its basename, a list, a tuple
        ("import subprocess\nsubprocess.run('ls')\n", [("ls", "")]),
        ("import subprocess\nsubprocess.run(['/usr/bin/open', url])\n", [("open", "")]),
        ("import subprocess\nsubprocess.Popen(('ffmpeg', '-i', p))\n", [("ffmpeg", "")]),
        # every spawn, by any name it is imported or bound as
        ("from subprocess import run as r\nr(['gh', 'pr'])\n", [("gh", "")]),
        ("import asyncio\nasyncio.create_subprocess_exec('glab', 'issue')\n", [("glab", "")]),
        ("import os\nos.execvp('vim', ['vim', path])\n", [("vim", "")]),
        ("import os\nos.spawnlp(os.P_WAIT, 'tar', 'tar', 'x')\n", [("tar", "")]),
        ("import os\nos.posix_spawn('/bin/cat', ['cat'], env)\n", [("cat", "")]),
        ("import pty\npty.spawn(['bash'])\n", [("bash", "")]),
        ("import subprocess\nrun = subprocess.run\nrun(['rsync', a, b])\n", [("rsync", "")]),
        (
            "import functools, subprocess\nrun = functools.partial(subprocess.run, check=True)\n"
            "run(['scp', a, b])\n",
            [("scp", "")],
        ),
        (
            "import subprocess\nloop.run_in_executor(None, subprocess.run, ['ssh', h])\n",
            [("ssh", "")],
        ),
        # a command line for a shell, command by command, one sh -c down
        ("import os\nos.system('make && scp a b')\n", [("make", ""), ("scp", "")]),
        ("import subprocess\nsubprocess.run(\"sh -c 'git push'\", shell=True)\n", [("git", "")]),
        (
            "import subprocess\nsubprocess.run('sudo -u x rsync a b', shell=True)\n",
            [("rsync", ""), ("sudo", "")],
        ),
        ("import subprocess\nsubprocess.run(f'{tool} --x', shell=True)\n", None),
        # past env and assignments; sudo is a program of its own
        ("import subprocess\nsubprocess.run(['env', 'A=1', 'scp', a])\n", [("scp", "")]),
        (
            "import subprocess\nsubprocess.run(['sudo', 'apt', 'update'])\n",
            [("apt", ""), ("sudo", "")],
        ),
        # the SDK's builders and finders, and shutil.which
        ("import subprocess\nsubprocess.run(git_argv(['-C', r, 'log']))\n", [("git", "")]),
        (
            "import subprocess\ndef f():\n    x = find_ffmpeg()\n"
            "    subprocess.run([x, '-i', p])\n",
            [("ffmpeg", "")],
        ),
        (
            "import shutil, subprocess\ndef f():\n    g = shutil.which('git')\n"
            "    subprocess.run([g, 'clone', u])\n",
            [("git", "")],
        ),
        ("import subprocess\nsubprocess.run(list(['gh', 'auth']))\n", [("gh", "")]),
        ("import shlex, subprocess\nsubprocess.run(shlex.split('git log -1'))\n", [("git", "")]),
        (
            "import subprocess\nsubprocess.run(['x', 'y'], executable='/usr/bin/zip')\n",
            [("zip", "")],
        ),
        # a name, through the function, the class and the module
        (
            "import subprocess\ndef f():\n    argv = ['gh', 'pr']\n    subprocess.run(argv)\n",
            [("gh", "")],
        ),
        (
            "import subprocess\nTOOL = 'limactl'\ndef f(a):\n    subprocess.run([TOOL, *a])\n",
            [("limactl", "")],
        ),
        (
            "import subprocess\nclass C:\n    def __init__(self, b='rsync'):\n"
            "        self._b = b or 'rsync'\n"
            "    def go(self):\n        subprocess.run([self._b, 'x'])\nC()\n",
            [("rsync", "")],
        ),
        (
            "import subprocess\ndef f(ok):\n    subprocess.run(['a' if ok else 'b'])\n",
            [("a", ""), ("b", "")],
        ),
        # a parameter: its default and what each call in the file passes
        (
            "import asyncio\nclass C:\n    async def run(self, argv):\n"
            "        await asyncio.create_subprocess_exec(*argv)\n"
            "    async def a(self):\n        await self.run(['gh', 'x'])\n"
            "    async def b(self):\n        argv = ['glab', 'y']\n        await self.run(argv)\n",
            [("glab", ""), ("gh", "")],
        ),
        ("import subprocess\ndef f(argv):\n    subprocess.run(argv)\n", None),
        # a row of a constant table a for walks
        (
            "import subprocess\nT = (('a', ('gh', 'auth')), ('b', ('glab', 'auth')))\n"
            "def probe(p):\n    subprocess.run(list(p))\n"
            "def doctor():\n    for name, p in T:\n        probe(p)\n",
            [("gh", ""), ("glab", "")],
        ),
        # what cannot be read
        ("import subprocess\ndef f(a):\n    subprocess.run(list(a.argv))\n", None),
        ("import subprocess\ndef f(c):\n    c, x = wrap(c)\n    subprocess.run(c)\n", None),
        ("import subprocess\nsubprocess.run(f'{x}')\n", None),
        # npx: the package it fetches, past its options, or unknown
        (
            "import subprocess\nsubprocess.run(['npx', '-y', 'skills', 'find', q])\n",
            [("npx", "skills")],
        ),
        ("import subprocess\nsubprocess.run(['npx', '--package', 'a', 'b'])\n", [("npx", "a")]),
        ("import subprocess\nsubprocess.run(['npx', '-y', pkg])\n", [("npx", None)]),
        (
            "import subprocess\nsubprocess.run('npx -y left-pad', shell=True)\n",
            [("npx", "left-pad")],
        ),
        (
            "import shutil, subprocess\ndef f(q):\n    n = shutil.which('npx')\n"
            "    subprocess.run([n, '-y', 'skills', 'find', q])\n",
            [("npx", "skills")],
        ),
        # not a spawn, and a spawn named but not called
        ("def run(argv):\n    pass\nrun(['ssh', h])\n", []),
        ("import subprocess\nERR = subprocess.CalledProcessError\n", []),
    ]
    problems = []
    for source, want in cases:
        sites = spawn_sites(source)
        got = (
            None
            if any(s[2] is None for s in sites)
            else sorted((x.program, x.package) for s in sites for x in (s[2] or []))
        )
        if want is not None:
            want = sorted(want)
        if got != want:
            problems.append(f"the reader read {source!r} as {got}, expected {want}")
    return problems


def problems(root: pathlib.Path = ROOT) -> list[str]:
    found = _reader_problems()
    sites, unreadable = census(root)
    found += [f"cannot read {entry}, so what it starts is unknown" for entry in unreadable]
    manifests = {m.parent.name: m for m in root.glob("*/app.json")}
    keys = {key for entries in sites.values() for key, _where, _l in entries}
    seen: dict[str, set[str]] = {}
    for app, entries in sorted(sites.items()):
        launches = declared(manifests[app])
        for key, where, read in entries:
            if read is None:
                hand = READ_BY_HAND.get(key)
                if hand is None:
                    found.append(
                        f"{app}: {where} starts a program the rail cannot read ({key}). Start it "
                        "by name (a literal argv, shutil.which('<name>'), git_argv, find_ffmpeg), "
                        "or add it to READ_BY_HAND with the programs it starts and why"
                    )
                    continue
                read = [Launch(p, None if p == NPX else "") for p in hand[0]]
            elif key in READ_BY_HAND:
                found.append(f"{key} is read by hand, but the rail reads it now; remove it")
            for launch in read:
                seen.setdefault(app, set()).add(launch.program)
                entry = launches.get(launch.program)
                if entry is None:
                    what = (
                        "programs the owner names"
                        if launch.program == YOU_NAME
                        else f"the {launch.program} program"
                    )
                    found.append(
                        f"{app}: {where} starts {what}, which {app}/app.json does not declare "
                        f'under launches. Declare {{"program": "{launch.program}"}} with why it '
                        "runs and what of the owner's it runs with"
                    )
                    continue
                if launch.program != NPX:
                    continue
                named = str(entry.get("npmPackage") or "")
                if launch.package is None:
                    found.append(
                        f"{app}: {where} runs npx, and the rail cannot read the package it "
                        "fetches. Name the package in the argv, as its launches entry's npmPackage "
                        "names it"
                    )
                elif launch.package != named:
                    found.append(
                        f"{app}: {where} runs npx {launch.package}, but its launches entry's "
                        f"npmPackage is {named!r}. Its consent says npx fetches the newest version "
                        "of that package, so the two must be the same name"
                    )
    for key, (programs, _why) in sorted(READ_BY_HAND.items()):
        if key not in keys:
            found.append(f"{key} is read by hand, but no such spawn exists now; remove it")
            continue
        path = root / key.split("::", 1)[0]
        for program in programs:
            if program != YOU_NAME and not _string_in(path, program):
                found.append(
                    f"{key} is read by hand as starting {program}, which that file never names"
                )
    for app, program in sorted(KNOWN_LAUNCHES.items()):
        if program not in seen.get(app, set()):
            found.append(
                f"vacuity floor: {app} was not seen starting {program!r}; either the reader "
                "stopped reading real code, or the app changed (update KNOWN_LAUNCHES in the same "
                "change)"
            )
    return found


def main() -> int:
    found = problems()
    if found:
        print("A program an app's code starts must be declared under launches:")
        for line in found:
            print(f"  {line}")
        return 1
    sites, _unreadable = census()
    count = sum(len(entries) for entries in sites.values())
    print(
        f"OK: {len(sites)} app(s) start programs from {count} spawn site(s), and every program "
        "is declared"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
