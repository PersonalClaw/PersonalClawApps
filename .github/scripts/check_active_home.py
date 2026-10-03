#!/usr/bin/env python3
"""Repo rail: a path an app keeps in PersonalClaw's home comes from the SDK.

PersonalClaw runs on the home ``PERSONALCLAW_HOME`` names (a second instance, a test's, a dev
gateway's), else ``~/.personalclaw``, and core works out which in one place. An app asks it, when
the path is used, through ``personalclaw.sdk.util``: ``config_dir()`` for the home, and
``app_data_dir(<name>)`` for the app's own folder in it. An app that works the home out itself
uses the account's ``~/.personalclaw`` whatever home PersonalClaw runs on: Rsync Sync kept a
second home's mirror there, Git Sync its working clone, and the Growth and Minutes servers their
databases when started by hand, each in a folder made outside the home in use and without the
private mode core makes a home with.

What counts, read on the AST from each bundle's own code (its tests, ``conftest.py``, ``tests/``
and hidden folders left out), and from every string in its ``app.json``:

* **reading** ``PERSONALCLAW_HOME``'s value (``os.environ.get``, ``os.getenv``, ``os.environ[…]``),
  by name or through a constant holding the name. Setting it for a child, or asking whether it is
  set, reads no value;
* **building** ``~/.personalclaw`` from the account's home: ``Path.home()``, ``expanduser(…)``,
  ``$HOME`` or ``"~"``, or a name bound to one of them, and the name ``.personalclaw`` in one
  expression;
* **spelling** a path under it: a string, not a docstring, naming ``~/.personalclaw``,
  ``$HOME/.personalclaw`` or ``${HOME}/.personalclaw``, which code then expands against the
  account's home or a sentence shows as where something is. A setting's ``default`` in
  ``app.json`` is one: the Configure page shows it as the setting's value and saves it. The one
  spelling a manifest may use is a client install's own resolution of the home,
  ``${PERSONALCLAW_HOME:-$HOME/.personalclaw}`` under ``platform.clientInstall``: that command
  runs in the owner's shell, where there is no SDK to ask.

A skip-list naming ``.personalclaw`` folders has the name without the account's home, and the
sync root's marker file (``.personalclaw-sync-root``) is another name: neither is the home.

**Vacuity floor.** A rail that matches nothing reads as clean, so the detectors are checked
against every shape they tell apart before anything is read, and each app in ``KNOWN_ASKS`` must
be seen asking for its home the way it is named for: one per way of asking, so a reader that stops
seeing real code turns this red.

Run locally exactly as CI does:

    python .github/scripts/check_active_home.py
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

_ENV = "PERSONALCLAW_HOME"
_HOME_NAME = ".personalclaw"

READS = f"reads ${_ENV}"
BUILDS = f"builds ~/{_HOME_NAME} from the account's home"
SPELLS = f"names a path under ~/{_HOME_NAME}"

#: A path under the default home, spelled from the account's home, as a whole path segment.
_SPELLED = re.compile(r"(?:(?<![\w~/.-])~|\$HOME(?!\w)|\$\{HOME\})/\.personalclaw(?![\w.-])")
#: The home's name leading a string, as the part joined onto the account's home.
_NAME_LEADS = re.compile(r"^/?\.personalclaw(?![\w.-])")
#: A client install's own resolution of the home: the variable, else the default.
_SHELL_RESOLUTION = re.compile(r"\$\{PERSONALCLAW_HOME:-(?:\$HOME|\$\{HOME\}|~)/\.personalclaw\}")
#: Where a manifest may resolve the home the shell's way.
_CLIENT_INSTALL = ".platform.clientInstall."

#: The SDK modules that answer where the home is, and what each asks.
_SDK_HOME = {
    "personalclaw.sdk.util": frozenset({"config_dir", "app_data_dir"}),
    "personalclaw.sdk.channel": frozenset({"config_dir"}),
}
SHELL_ASK = "the shell's resolution of the home"

#: Apps the vacuity floor must see asking for their home, one per way of asking: the SDK's
#: ``config_dir()``, its ``app_data_dir()``, and a client install's shell.
KNOWN_ASKS = {
    "rsync-sync": "config_dir",
    "growth": "app_data_dir",
    "browser-connector": SHELL_ASK,
}


def _text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _bound_name(target: ast.AST) -> str:
    """The name an assignment binds: ``x`` or ``self.x``'s ``x``."""
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _account_home(node: ast.AST, names: frozenset[str] = frozenset()) -> bool:
    """An expression answering the account's home: ``Path.home()``, ``expanduser(…)``, ``$HOME``,
    ``"~"``, or a name bound to one of those."""
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in ("home", "expanduser"):
            return True
        if name in ("get", "getenv") and node.args and _text(node.args[0]) == "HOME":
            return True
    if isinstance(node, ast.Subscript) and _text(node.slice) == "HOME":
        return True
    if _text(node) == "~":
        return True
    return isinstance(node, (ast.Name, ast.Attribute)) and _bound_name(node) in names


def _the_account_home_itself(node: ast.AST) -> bool:
    """The account's home and nothing else: ``Path.home()``, ``expanduser("~")``,
    ``Path("~").expanduser()``, ``$HOME``, or one of those in a one-argument wrapper
    (``str(…)``, ``Path(…)``, ``realpath(…)``). What a name must be bound to for the rail to read
    it as the account's home: ``root = expanduser(setting)`` is wherever the setting says."""
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        if _the_account_home_itself(node.args[0]):
            return True
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name == "home" and not node.args:
            return True
        if name == "expanduser":
            func = node.func
            target = node.args[0] if node.args else None
            if target is None and isinstance(func, ast.Attribute):
                inner = func.value
                target = inner.args[0] if isinstance(inner, ast.Call) and inner.args else None
            return target is not None and _text(target) == "~"
        if name in ("get", "getenv") and node.args and _text(node.args[0]) == "HOME":
            return True
    return isinstance(node, ast.Subscript) and _text(node.slice) == "HOME"


def _home_name(node: ast.AST, names: frozenset[str]) -> bool:
    """A string led by the home's name (``".personalclaw"``, ``".personalclaw/apps"``, an
    f-string's ``"/.personalclaw/…"``), or a name bound to it. A string that spells the home
    whole is reported by itself, and one naming a ``.personalclaw`` folder further down
    (``"~/work/.personalclaw"``) is some project's own."""
    text = _text(node)
    if text is not None:
        return bool(_NAME_LEADS.match(text))
    return isinstance(node, (ast.Name, ast.Attribute)) and _bound_name(node) in names


def _statement_strings(tree: ast.AST) -> set[int]:
    """The ids of strings that are statements of their own: docstrings, which explain and compute
    nothing."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and _text(node.value) is not None
    }


def _bindings(tree: ast.AST) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """The names the file binds to the variable's name, to the account's home, and to the home's
    name."""
    variable: set[str] = set()
    account: set[str] = set()
    home_name: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        bound = {name for name in map(_bound_name, targets) if name}
        if _text(value) == _ENV:
            variable |= bound
        elif _text(value) == _HOME_NAME:
            home_name |= bound
        elif _the_account_home_itself(value):
            account |= bound
    return frozenset(variable), frozenset(account), frozenset(home_name)


def _reads_the_variable(node: ast.AST, aliases: frozenset[str]) -> bool:
    def names_it(arg: ast.AST) -> bool:
        return _text(arg) == _ENV or (isinstance(arg, ast.Name) and arg.id in aliases)

    if isinstance(node, ast.Call) and _call_name(node) in ("get", "getenv") and node.args:
        return names_it(node.args[0])
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return names_it(node.slice)
    return False


def resolutions(source: str) -> list[tuple[int, str]]:
    """Every place ``source`` works PersonalClaw's home out itself, as ``(line, what)``."""
    tree = ast.parse(source)
    statements = _statement_strings(tree)
    variable, account, home_name = _bindings(tree)
    found: set[tuple[int, str]] = set()
    built: list[ast.AST] = []
    for node in ast.walk(tree):
        if _reads_the_variable(node, variable):
            found.add((node.lineno, READS))
        text = _text(node)
        if text is not None and id(node) not in statements and _SPELLED.search(text):
            found.add((node.lineno, SPELLS))
        if isinstance(node, (ast.BinOp, ast.Call, ast.JoinedStr)):
            inner = list(ast.walk(node))
            if any(_account_home(n, account) for n in inner) and any(
                _home_name(n, home_name) for n in inner
            ):
                built.append(node)
    # The innermost expression only: `str(Path.home() / ".personalclaw")` is one site, not two.
    for node in built:
        below = set(ast.walk(node)) - {node}
        if not any(other in below for other in built):
            found.add((node.lineno, BUILDS))
    return sorted(found)


def asks(source: str) -> list[tuple[int, str]]:
    """Every call ``source`` makes to the SDK for the home, as ``(line, function)``."""
    tree = ast.parse(source)
    local: dict[str, str] = {}
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in _SDK_HOME:
            for alias in node.names:
                if alias.name in _SDK_HOME[node.module]:
                    local[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _SDK_HOME and alias.asname:
                    modules[alias.asname] = alias.name
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in local:
            found.append((node.lineno, local[func.id]))
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
            and func.attr in _SDK_HOME[modules[func.value.id]]
        ):
            found.append((node.lineno, func.attr))
    return sorted(found)


def manifest_findings(data: object) -> tuple[list[str], int]:
    """The JSON paths of every string in a manifest that spells a path under the default home,
    and how many client-install strings resolve the home the shell's way."""
    found: list[str] = []
    shell = 0

    def walk(node: object, path: str) -> None:
        nonlocal shell
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for at, value in enumerate(node):
                walk(value, f"{path}[{at}]")
        elif isinstance(node, str):
            text = node
            if path.startswith(_CLIENT_INSTALL):
                text, resolved = _SHELL_RESOLUTION.subn("", text)
                shell += bool(resolved)
            if _SPELLED.search(text):
                found.append(path)

    walk(data, "")
    return found, shell


def _is_bundle_code(path: pathlib.Path, bundle: pathlib.Path) -> bool:
    rel = path.relative_to(bundle)
    return not (
        path.name.startswith("test_")
        or path.name == "conftest.py"
        or "tests" in rel.parts
        or "node_modules" in rel.parts
        or any(part.startswith(".") for part in rel.parts)
    )


class Census:
    """What the rail read: every site that works the home out, every ask, and what it couldn't
    read."""

    def __init__(self) -> None:
        self.sites: dict[str, list[str]] = {}
        self.asks: dict[str, set[str]] = {}
        self.unreadable: list[str] = []
        self.files = 0
        self.manifests = 0


def census(root: pathlib.Path = ROOT) -> Census:
    seen = Census()
    for manifest in sorted(root.glob("*/app.json")):
        bundle = manifest.parent
        app = bundle.name
        rel_manifest = manifest.relative_to(root).as_posix()
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            seen.unreadable.append(f"{rel_manifest}: {exc}")
        else:
            seen.manifests += 1
            spelled, shell = manifest_findings(data)
            for path in spelled:
                where = f"{rel_manifest} {path.lstrip('.')}"
                seen.sites.setdefault(app, []).append(f"{where}: {SPELLS}")
            if shell:
                seen.asks.setdefault(app, set()).add(SHELL_ASK)
        for path in sorted(bundle.rglob("*.py")):
            if not _is_bundle_code(path, bundle):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
                found, asked = resolutions(source), asks(source)
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                seen.unreadable.append(f"{rel}: {exc}")
                continue
            seen.files += 1
            for line, what in found:
                seen.sites.setdefault(app, []).append(f"{rel}:{line}: {what}")
            if asked:
                seen.asks.setdefault(app, set()).update(name for _line, name in asked)
    return seen


def _detector_problems() -> list[str]:
    """The positive control: the detectors on every shape they have to tell apart."""
    code_cases = [
        # reading the variable
        ('x = os.environ.get("PERSONALCLAW_HOME")\n', [READS]),
        ('x = os.getenv("PERSONALCLAW_HOME", "")\n', [READS]),
        ('x = os.environ["PERSONALCLAW_HOME"]\n', [READS]),
        ('VAR = "PERSONALCLAW_HOME"\nx = os.environ.get(VAR)\n', [READS]),
        ('ok = "PERSONALCLAW_HOME" in os.environ\n', []),
        ('env["PERSONALCLAW_HOME"] = str(home)\n', []),
        ('env = {**os.environ, "PERSONALCLAW_HOME": str(home)}\n', []),
        # building it from the account's home
        ('x = Path.home() / ".personalclaw"\n', [BUILDS]),
        ('x = str(Path.home() / ".personalclaw" / "sync")\n', [BUILDS]),
        ('x = os.path.join(os.path.expanduser("~"), ".personalclaw", "apps")\n', [BUILDS]),
        ('x = os.path.join("~", ".personalclaw")\n', [BUILDS]),
        ('x = Path(os.environ["HOME"]) / ".personalclaw"\n', [BUILDS]),
        ('x = Path(os.getenv("HOME")) / ".personalclaw/apps"\n', [BUILDS]),
        ('home = Path.home()\nx = home / ".personalclaw"\n', [BUILDS]),
        ('home = os.path.expanduser("~")\nx = os.path.join(home, ".personalclaw")\n', [BUILDS]),
        ('root = os.path.expanduser(setting)\nx = os.path.join(root, ".personalclaw")\n', []),
        ('NAME = ".personalclaw"\nx = Path.home() / NAME\n', [BUILDS]),
        (
            'class P:\n    def __init__(self):\n        self._home = os.path.expanduser("~")\n'
            '    def f(self):\n        return os.path.join(self._home, ".personalclaw")\n',
            [BUILDS],
        ),
        ('x = f"{Path.home()}/.personalclaw/sync"\n', [BUILDS]),
        ('x = Path.home() / ".claude" / "settings.json"\n', []),
        ('x = Path(project) / ".personalclaw" / "rules.json"\n', []),
        ('SKIP = frozenset({".git", "node_modules", ".personalclaw"})\n', []),
        ('MARKER = ".personalclaw-sync-root"\n', []),
        ('x = os.path.join(os.path.expanduser("~"), ".personalclaw-sync-root")\n', []),
        ('host = os.path.expanduser("~")\nx = host + "/work"\n', []),
        # spelling a path under it
        ('def f(d: str = "~/.personalclaw/sync/x"):\n    pass\n', [SPELLS]),
        ('x = cfg.get("dir", "") or "~/.personalclaw/sync/x"\n', [SPELLS]),
        ('x = os.path.expanduser("~/.personalclaw/apps/x/data")\n', [SPELLS]),
        ('x = Path("~/.personalclaw").expanduser()\n', [SPELLS]),
        ('x = os.path.expandvars("$HOME/.personalclaw/x")\n', [SPELLS]),
        ('x = "${HOME}/.personalclaw"\n', [SPELLS]),
        ('x = f"~/.personalclaw/{name}"\n', [SPELLS]),
        ('x = "${PERSONALCLAW_HOME:-$HOME/.personalclaw}/apps"\n', [SPELLS]),
        ('msg = "kept in ~/.personalclaw/sync on this machine"\n', [SPELLS]),
        ('x = "~/.personalclaw-sync-root"\n', []),
        ('x = "~/work/.personalclaw"\n', []),
        ('x = os.path.expanduser("~/work/.personalclaw")\n', []),
        ('x = "~/.personalclawish"\n', []),
        ('def f():\n    """Was ~/.personalclaw/sync/x, frozen at import."""\n', []),
        ('"""A module once at ~/.personalclaw/x."""\n', []),
        ('x = config_dir() / "sync" / "x"\n', []),
    ]
    problems = []
    for source, want in code_cases:
        got = [what for _line, what in resolutions(source)]
        if got != want:
            problems.append(f"the detectors read {source!r} as {got}, expected {want}")
    ask_cases = [
        ("from personalclaw.sdk.util import config_dir\nx = config_dir() / 'a'\n", ["config_dir"]),
        ("from personalclaw.sdk.util import app_data_dir as d\nx = d('a')\n", ["app_data_dir"]),
        ("from personalclaw.sdk.channel import config_dir\nconfig_dir()\n", ["config_dir"]),
        ("import personalclaw.sdk.util as u\nu.app_data_dir('a')\n", ["app_data_dir"]),
        ("from personalclaw.sdk.util import atomic_write\natomic_write(p, b'')\n", []),
        ("def config_dir():\n    pass\nconfig_dir()\n", []),
    ]
    for source, want in ask_cases:
        got = [name for _line, name in asks(source)]
        if got != want:
            problems.append(f"the ask reader read {source!r} as {got}, expected {want}")
    resolved = 'D="${PERSONALCLAW_HOME:-$HOME/.personalclaw}"'
    manifest_cases = [
        ({"provider": {"settingsSchema": {"properties": {"d": {"default": "~/.personalclaw/x"}}}}},
         [".provider.settingsSchema.properties.d.default"], 0),
        ({"provider": {"settingsSchema": {"properties": {"d": {"default": ""}}}}}, [], 0),
        ({"platform": {"clientInstall": {"shell": resolved}}}, [], 1),
        ({"platform": {"clientInstall": {"shell": 'D="$HOME/.personalclaw/apps"'}}},
         [".platform.clientInstall.shell"], 0),
        ({"setup": {"onInstall": f"cp x {resolved}"}}, [".setup.onInstall"], 0),
        ({"description": "Keeps a mirror in ~/.personalclaw/sync."}, [".description"], 0),
        ({"description": "Marks the root with .personalclaw-sync-root."}, [], 0),
    ]
    for data, want, want_shell in manifest_cases:
        got, shell = manifest_findings(data)
        if (got, shell) != (want, want_shell):
            problems.append(
                f"the manifest reader read {data!r} as {(got, shell)}, "
                f"expected {(want, want_shell)}"
            )
    return problems


def problems(root: pathlib.Path = ROOT) -> list[str]:
    found = _detector_problems()
    seen = census(root)
    found += [
        f"cannot read {entry}, so whether it works the home out is unknown"
        for entry in seen.unreadable
    ]
    for app, want in sorted(KNOWN_ASKS.items()):
        if want not in seen.asks.get(app, set()):
            found.append(
                f"vacuity floor: {app} was not seen asking for its home with {want!r}; either a "
                "reader stopped seeing real code, or the app changed (update KNOWN_ASKS in the "
                "same change)"
            )
    for app, sites in sorted(seen.sites.items()):
        found += [f"{app}: {site}" for site in sites]
    return found


def main() -> int:
    found = problems()
    if found:
        print("A path an app keeps in PersonalClaw's home must come from the SDK:")
        for line in found:
            print(f"  {line}")
        print(
            "\nAsk personalclaw.sdk.util when the path is used: config_dir() for the home in use, "
            "app_data_dir(<name>) for the app's own folder in it. A setting that names a folder "
            "there defaults to empty, and the code fills the empty value in from those."
        )
        return 1
    seen = census()
    print(
        f"OK: {seen.files} file(s) and {seen.manifests} manifest(s) read; no app works out "
        f"PersonalClaw's home itself, and {len(seen.asks)} app(s) ask for it"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
