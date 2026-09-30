#!/usr/bin/env python3
"""Repo rail: every request an app makes through the SDK's egress guard honours the owner's
network settings.

Settings → Security → Network egress is where the owner says what PersonalClaw may reach: a host
on Denied hosts is never reached, a host on Allowed hosts is reached even when it resolves to this
machine or a private network, and "Allow all private networks" opens every private address. Core
layers those settings onto a policy profile in ``personalclaw.sdk.net.egress_policy_for(profile)``.
A request made under the bare profile (``fetch(url, policy=CONNECTOR)``), under a policy the app
builds itself, or under no policy at all (the guard's default) is judged by that policy alone, so
a host the owner denied is reached anyway.

So every call in a bundle's code to the SDK's ``fetch``, or to ``evaluate`` (the synchronous guard
an app with a synchronous surface asks before its own request), is read here, on the AST, for
where its policy comes from:

* **layered**: a call to one of :data:`LAYERED` (``egress_policy_for``, and the two builders core
  makes on it), a ``.with_overrides(...)`` or ``dataclasses.replace(...)`` of one that leaves the
  owner's settings alone, a name the enclosing function binds only to those, or a function or
  method of the same file whose every ``return`` is one;
* **bare**: a profile constant, a policy the app builds itself, no policy at all, or a layered
  policy with the owner's settings overridden. It fails the rail by name;
* **passthrough**: the enclosing function's own parameter, a seam whose caller chooses. Each is
  named in :data:`PASS_THROUGH` with where that caller's policy comes from, and a new one fails
  the rail until it is; a stale entry fails it too;
* anything the reader cannot place fails the rail by name.

**Vacuity floor.** A rail that matches nothing reads as clean, so the reader is checked against the
shapes it tells apart before anything is read, and each app in :data:`KNOWN_LAYERED` must be seen
making a layered request.

Run locally exactly as CI does:

    python .github/scripts/check_egress_policy.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The SDK modules the guarded calls, the policy profiles and their builders come from
#: (``sdk.sync`` re-exports the sync transport's profile and builder).
EGRESS_MODULES = frozenset({"personalclaw.sdk.net", "personalclaw.sdk.sync"})

#: The SDK's calls that take an egress policy: ``fetch`` by keyword, ``evaluate`` second.
GUARDED = frozenset({"fetch", "evaluate"})

#: The SDK's builders whose policy carries the owner's settings: ``egress_policy_for`` itself,
#: ``sync_egress_policy`` (the sync profile layered, then pinned to the configured endpoint) and
#: ``a2a_outbound_policy`` (the outbound A2A profile layered).
LAYERED = frozenset({"egress_policy_for", "sync_egress_policy", "a2a_outbound_policy"})

#: The fields of a policy that hold the owner's settings. A ``.with_overrides`` (or a
#: ``dataclasses.replace``) that sets one after layering puts back what the owner said.
OWNER_FIELDS = frozenset({"allow_hosts", "deny_hosts", "allow_private"})

#: Seams that take their policy from their caller, and where the caller's policy comes from.
PASS_THROUGH = {
    "git-repo/provider.py::GitRepoSourceProvider._fetch_json::fetch": (
        "the watched-source engine hands egress_policy_for(SOURCE); a direct call without one "
        "layers CONNECTOR"
    ),
    "openrouter-models/provider.py::_request_json::fetch": (
        "its callers pass _long_policy(...), egress_policy_for(CONNECTOR) with longer caps; a "
        "call without one layers CONNECTOR"
    ),
}

#: Apps that must be seen making a layered request, or the reader has stopped seeing what it
#: exists for: one per builder, and the search apps.
KNOWN_LAYERED = (
    "searxng-search",
    "brave-search",
    "tavily-search",
    "webhook-action",
    "s3-sync",
    "a2a-action",
)

LAYERED_KIND, PASS_THROUGH_KIND, UNKNOWN_KIND, BARE_KIND = (
    "layered", "passthrough", "unknown", "bare",
)
_RANK = {LAYERED_KIND: 0, PASS_THROUGH_KIND: 1, UNKNOWN_KIND: 2, BARE_KIND: 3}
_UNKNOWN = (UNKNOWN_KIND, "")


def _worst(*sources: tuple[str, str] | None) -> tuple[str, str]:
    """The source that says least for the owner: a bare branch outranks a layered one. ``None``
    is a name read again while it is being read (``policy = replace(policy, ...)``), which adds
    nothing to what its other values say."""
    known = [source for source in sources if source is not None]
    return max(known, key=lambda source: _RANK[source[0]]) if known else _UNKNOWN


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _parameters(func: ast.AST) -> set[str]:
    args = func.args  # type: ignore[attr-defined]
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    names.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
    return names


def _bound(func: ast.AST, name: str) -> list[ast.expr]:
    """What the function assigns to ``name``."""
    values: list[ast.expr] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            values.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            values.append(node.value)
    return values


class _Reader(ast.NodeVisitor):
    """Every guarded call in one file, and where its policy comes from."""

    def __init__(self, tree: ast.Module, rel: str) -> None:
        self.rel = rel
        self.names: dict[str, str] = {}  # a local name → the SDK name it was imported as
        self.modules: set[str] = set()  # local names bound to one of the SDK modules itself
        self.replace: set[str] = set()  # local names bound to dataclasses.replace
        self.functions: dict[str, ast.AST] = {}
        self.methods: dict[tuple[str, str], ast.AST] = {}
        self.module_bound: dict[str, list[ast.expr]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    if node.module in EGRESS_MODULES:
                        self.names[alias.asname or alias.name] = alias.name
                    elif f"{node.module}.{alias.name}" in EGRESS_MODULES:
                        self.modules.add(alias.asname or alias.name)
                    elif node.module == "dataclasses" and alias.name == "replace":
                        self.replace.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in EGRESS_MODULES and alias.asname:
                        self.modules.add(alias.asname)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.methods[(node.name, item.name)] = item
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.module_bound.setdefault(target.id, []).append(node.value)
        self.sites: list[tuple[str, int, str, str]] = []
        self._qualname: list[str] = []
        self._funcs: list[ast.AST] = []
        self._classes: list[str] = []
        self.visit(tree)

    def sdk_name(self, node: ast.AST) -> str | None:
        """The SDK name ``node`` refers to, or ``None``."""
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        if isinstance(node, ast.Attribute):
            owner = node.value
            if isinstance(owner, ast.Name) and owner.id in self.modules:
                return node.attr
            if _dotted(owner) in EGRESS_MODULES:
                return node.attr
        return None

    def _copied(self, call: ast.Call) -> ast.expr | None:
        """The policy a ``.with_overrides(...)`` or ``dataclasses.replace(...)`` copies, or
        ``None`` when ``call`` is neither."""
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "with_overrides":
            return func.value
        is_replace = (isinstance(func, ast.Name) and func.id in self.replace) or (
            _dotted(func) == "dataclasses.replace"
        )
        return call.args[0] if is_replace and call.args else None

    def _function_called(self, func: ast.AST, cls: str | None) -> ast.AST | None:
        if isinstance(func, ast.Name):
            return self.functions.get(func.id)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
            and cls is not None
        ):
            return self.methods.get((cls, func.attr))
        return None

    def source(
        self,
        node: ast.AST,
        func: ast.AST | None,
        cls: str | None,
        depth: int = 0,
        reading: frozenset[tuple[int, str]] = frozenset(),
    ) -> tuple[str, str] | None:
        """``(kind, what it is)`` for a policy expression, read in ``func`` of class ``cls``.
        ``reading`` holds the names being read already, and one of them read again is ``None``."""
        if depth > 8:
            return _UNKNOWN

        def inner(value: ast.AST, where: ast.AST | None = func, names=reading):
            return self.source(value, where, cls, depth + 1, names)

        if isinstance(node, ast.IfExp):
            return _worst(inner(node.body), inner(node.orelse))
        if isinstance(node, ast.Call):
            called = self.sdk_name(node.func)
            if called in LAYERED:
                return LAYERED_KIND, f"{called}(...)"
            if called == "EgressPolicy":
                return BARE_KIND, "a policy the app builds itself"
            copied = self._copied(node)
            if copied is not None:
                touched = [
                    k.arg or "**" for k in node.keywords if k.arg is None or k.arg in OWNER_FIELDS
                ]
                if touched:
                    return BARE_KIND, (
                        "a policy with the owner's settings overridden (" + ", ".join(touched) + ")"
                    )
                return inner(copied)
            target = self._function_called(node.func, cls)
            if target is not None:
                returns = [
                    r.value for r in ast.walk(target) if isinstance(r, ast.Return) and r.value
                ]
                return _worst(*(inner(r, target, frozenset()) for r in returns))
            return _UNKNOWN
        if isinstance(node, ast.Name):
            imported = self.names.get(node.id)
            if imported is not None:
                if imported in LAYERED:
                    return _UNKNOWN  # the builder itself, not a policy it built
                return BARE_KIND, f"the bare {imported} profile"
            if func is not None:
                if node.id in _parameters(func):
                    return PASS_THROUGH_KIND, f"its parameter {node.id!r}"
                bound = _bound(func, node.id)
                if bound:
                    key = (id(func), node.id)
                    if key in reading:
                        return None
                    return _worst(*(inner(v, func, reading | {key}) for v in bound))
            bound = self.module_bound.get(node.id, [])
            if bound:
                key = (0, node.id)
                if key in reading:
                    return None
                return _worst(*(inner(v, None, reading | {key}) for v in bound))
            return _UNKNOWN
        if isinstance(node, ast.Attribute):
            named = self.sdk_name(node)
            if named is not None:
                return BARE_KIND, f"the bare {named} profile"
        return _UNKNOWN

    def _policy_of(self, call: ast.Call, called: str) -> tuple[str, str]:
        func = self._funcs[-1] if self._funcs else None
        cls = self._classes[-1] if self._classes else None
        given = [k.value for k in call.keywords if k.arg == "policy"]
        if called == "evaluate" and len(call.args) > 1:
            given.append(call.args[1])
        if given:
            return _worst(self.source(given[0], func, cls))
        if any(k.arg is None for k in call.keywords):
            return PASS_THROUGH_KIND, "its caller's keyword arguments"
        if called == "fetch":
            return BARE_KIND, "no policy, so the guard's default"
        return _UNKNOWN

    def visit_FunctionDef(self, node: ast.AST) -> None:  # noqa: N802 — the ast hook name
        self._qualname.append(node.name)  # type: ignore[attr-defined]
        self._funcs.append(node)
        self.generic_visit(node)
        self._funcs.pop()
        self._qualname.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._qualname.append(node.name)
        self._classes.append(node.name)
        self.generic_visit(node)
        self._classes.pop()
        self._qualname.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        called = self.sdk_name(node.func)
        if called in GUARDED:
            kind, what = self._policy_of(node, called)
            key = f"{self.rel}::{'.'.join(self._qualname) or '<module>'}::{called}"
            self.sites.append((key, node.lineno, kind, what))
        self.generic_visit(node)


def _is_bundle_code(path: pathlib.Path, bundle: pathlib.Path) -> bool:
    rel = path.relative_to(bundle)
    return not (
        path.name.startswith("test_")
        or path.name == "conftest.py"
        or "tests" in rel.parts
        or any(part.startswith(".") for part in rel.parts)
    )


def census(root: pathlib.Path = ROOT) -> tuple[list[tuple[str, int, str, str]], list[str]]:
    """Every guarded call in a bundle's code as ``(key, line, kind, what)``, and the files that
    could not be read."""
    sites: list[tuple[str, int, str, str]] = []
    unreadable: list[str] = []
    for manifest in sorted(root.glob("*/app.json")):
        bundle = manifest.parent
        for path in sorted(bundle.rglob("*.py")):
            if not _is_bundle_code(path, bundle):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError) as error:
                unreadable.append(f"cannot read {rel}: {error}")
                continue
            sites.extend(_Reader(tree, rel).sites)
    return sites, unreadable


#: The positive control: every shape the reader tells apart, and what it must read.
_CONTROL = '''
from dataclasses import replace

from personalclaw.sdk import net
from personalclaw.sdk.net import CONNECTOR, EgressPolicy, egress_policy_for, evaluate, fetch
from personalclaw.sdk.net import fetch as net_fetch
from personalclaw.sdk.sync import sync_egress_policy

_OWN = EgressPolicy(name="own")


def _layered():
    return egress_policy_for(CONNECTOR).with_overrides(timeout_s=5.0)


class Client:
    def _policy(self):
        return egress_policy_for(CONNECTOR)

    async def calls(self, url, policy, flag, **kw):
        await fetch(url, policy=CONNECTOR)
        await fetch(url)
        await net_fetch(url, policy=_OWN)
        await net.fetch(url, policy=net.CONNECTOR)
        await fetch(url, policy=egress_policy_for(CONNECTOR).with_overrides(deny_hosts=()))
        evaluate(url, CONNECTOR)
        await fetch(url, policy=CONNECTOR if flag else egress_policy_for(CONNECTOR))
        await fetch(url, policy=replace(egress_policy_for(CONNECTOR), allow_private=True))
        await fetch(url, policy=egress_policy_for(CONNECTOR))
        await net.fetch(url, policy=net.egress_policy_for(net.CONNECTOR))
        await fetch(url, policy=_layered())
        await fetch(url, policy=self._policy())
        chosen = egress_policy_for(CONNECTOR)
        if flag:
            chosen = replace(chosen, timeout_s=5.0)
        await fetch(url, policy=chosen)
        await fetch(url, policy=sync_egress_policy(url))
        evaluate(url, egress_policy_for(CONNECTOR))
        await fetch(url, policy=policy)
        await fetch(url, policy=policy if flag else egress_policy_for(CONNECTOR))
        await fetch(url, **kw)
        await fetch(url, policy=elsewhere())
'''
_CONTROL_READS = [BARE_KIND] * 8 + [LAYERED_KIND] * 7 + [PASS_THROUGH_KIND] * 3 + [UNKNOWN_KIND]


def _reader_problems() -> list[str]:
    sites = _Reader(ast.parse(_CONTROL), "control.py").sites
    got = [kind for _key, _line, kind, _what in sorted(sites, key=lambda site: site[1])]
    if got == _CONTROL_READS:
        return []
    return [f"the reader read {got}, expected {_CONTROL_READS}"]


def problems(root: pathlib.Path = ROOT) -> list[str]:
    found = _reader_problems()
    sites, unreadable = census(root)
    found.extend(unreadable)
    seen = {key for key, _line, _kind, _what in sites}
    for key, line, kind, what in sites:
        where = f"{key.split('::')[0]}:{line}"
        if kind == BARE_KIND:
            found.append(
                f"{where} makes its request under {what}, which leaves out the owner's "
                "Settings → Security → Network egress: pass egress_policy_for(<profile>) "
                f"({key})"
            )
        elif kind == UNKNOWN_KIND:
            found.append(
                f"{where}: where this request's policy comes from cannot be read ({key}). Build "
                "it with egress_policy_for(<profile>) in the function that makes the request, or "
                "in a function of the same file that returns it"
            )
        elif kind == PASS_THROUGH_KIND and key not in PASS_THROUGH:
            found.append(
                f"{where} takes its policy from {what} ({key}): name it in PASS_THROUGH with "
                "where its callers' policy comes from"
            )
        elif kind == LAYERED_KIND and key in PASS_THROUGH:
            found.append(f"{key} no longer takes its policy from its caller; remove it")
    for key in sorted(set(PASS_THROUGH) - seen):
        found.append(f"{key} is in PASS_THROUGH but no longer exists; remove it")
    layered_apps = {key.split("/")[0] for key, _line, kind, _what in sites if kind == LAYERED_KIND}
    for app in KNOWN_LAYERED:
        if app not in layered_apps:
            found.append(f"vacuity floor: {app} was not seen making a request under the owner's "
                         "network settings")
    return found


def main() -> int:
    found = problems()
    if found:
        print("A request an app makes must honour the owner's network settings:")
        for line in found:
            print(f"  {line}")
        return 1
    sites, _unreadable = census()
    passed = sum(1 for _key, _line, kind, _what in sites if kind == PASS_THROUGH_KIND)
    print(
        f"OK: {len(sites)} request site(s) through the SDK's egress guard, every one under the "
        f"owner's network settings ({passed} passed through from a caller)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
