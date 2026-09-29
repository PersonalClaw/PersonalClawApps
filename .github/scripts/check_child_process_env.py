#!/usr/bin/env python3
"""Repo rail: every process an app's code starts says where its environment comes from.

An app's provider module runs INSIDE the gateway, and the gateway's environment holds every
secret saved in PersonalClaw (core exports them for its own children), plus whatever the shell
that started it had. So a child an app's provider starts with ``env`` left out inherits all of
it. Core cannot build that environment for an in-process provider; the SDK gives the provider
what to pass: ``personalclaw.sdk.util.child_process_env()`` (the child allowlist: PATH, the home,
locale, proxy and certificate settings, and what the owner passed through by name),
``app_packages_env()`` (the same, with the app packages on PYTHONPATH) and, for a git,
``personalclaw.sdk.git.git_env()`` (the same, with the SSH agent for a command that talks to a
remote).

A git runs inside a repository an agent's shell can write, so a git an app starts also builds
its argv with ``personalclaw.sdk.git.git_argv``: the settings that stop the repository's own
configuration (a hook, a file-system monitor, an ssh command) from running a program. Every spawn
whose argv starts with the word ``git`` is held to that, checked on the AST.

So every spawn site in a bundle (``subprocess.run/Popen/call/check_output/check_call``,
``asyncio.create_subprocess_exec/shell``) is classified here, by ``bundle/file::qualname::callee``:

* ``BUILT``: a child that runs a program or package someone else wrote. It must pass
  ``env=`` from one of the two SDK builders, checked on the AST: a direct call, a conditional
  between two of them, or a name the enclosing function binds to one.
* ``OWN_ENVIRONMENT``: a child that keeps the environment of the process that starts it, and
  why: the owner's own authenticated tool with a fixed command (it signs in from that
  environment), a ``personalclaw doctor`` step, a companion's own process.
* ``PASS_THROUGH``: a seam that spawns with the environment its caller chose.

A new spawn site fails this rail by name until it is classified; a stale entry fails it too.

**Vacuity floor.** A rail that matches nothing reads as clean, so at least as many sites as are
classified must be found, and the resolver is checked against the shapes it tells apart before
anything is read.

Run locally exactly as CI does:

    python .github/scripts/check_child_process_env.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The SDK's answers for a child's environment (``sdk.util`` and ``sdk.git``).
BUILDERS = frozenset({"child_process_env", "app_packages_env", "git_env"})

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

BUILT = {
    "piper-tts/provider.py::_synthesize_piper_chunk::asyncio.create_subprocess_exec": (
        "piper, someone else's program reading a voice someone else trained"
    ),
    "diarization-onnx/provider.py::_decode::subprocess.run": (
        "ffmpeg, someone else's program decoding a recording someone uploaded"
    ),
    "skills-sh/provider.py::SkillsShMarketplace._search_via_cli::subprocess.run": (
        "`npx -y skills`, a package someone else publishes, fetched and run"
    ),
    "skills-sh/provider.py::SkillsShMarketplace._fetch_via_cli::subprocess.run": (
        "a clone of someone else's repository"
    ),
    "git-sync/provider.py::GitSyncProvider._run::subprocess.run": (
        "git in a working clone an agent's shell can write, to the owner's own sync remote"
    ),
    "git-repo/provider.py::GitRepoSourceProvider._git::subprocess.run": (
        "read-only git plumbing in the owner's clone, which an agent's shell can write"
    ),
    "notes/notebook.py::Notebook._run::subprocess.run": (
        "git in the notebook folder, which an agent's shell can write"
    ),
    "spec-builder/specs.py::SpecStore._git::subprocess.run": (
        "read-only git in the owner's source repository, which an agent's shell can write"
    ),
    "rsync-sync/rsync_sync_transport.py::RsyncSyncProvider._run::subprocess.run": (
        "rsync over ssh to the owner's own host: the allowlist and the SSH agent socket only"
    ),
}

OWN_ENVIRONMENT = {
    "code-review/provider.py::CodeReviewProvider._gh_diff::asyncio.create_subprocess_exec": (
        "the owner's `gh`, a fixed validated argv; it signs in from GH_TOKEN or its own config"
    ),
    "issue-radar/provider.py::IssueRadarProvider._run_json::asyncio.create_subprocess_exec": (
        "the owner's `gh` / `glab`, a fixed validated argv; they sign in from the environment"
    ),
    "ops/runbooks.py::run_action::subprocess.run": (
        "the operator's own runbook action, which reads their cloud and cluster sign-in"
    ),
    "lima-sandbox/provider.py::LimaSandboxProvider._run_limactl::subprocess.run": (
        "limactl controlling the owner's own VM, a fixed argv"
    ),
    "code-review/app_cli.py::doctor::subprocess.run": "a `personalclaw doctor` step",
    "issue-radar/app_cli.py::_tracker_line::subprocess.run": "a `personalclaw doctor` step",
    "notes/app_cli.py::_git_version::subprocess.run": "a `personalclaw doctor` step",
    "spec-builder/app_cli.py::_git_version::subprocess.run": "a `personalclaw doctor` step",
    "menu-bar-companion/menubar_companion/notify.py::_default_runner::subprocess.run": (
        "the companion's own process, not the gateway"
    ),
    "menu-bar-companion/run.py::_open_url::subprocess.run": (
        "the companion's own process, not the gateway"
    ),
}

PASS_THROUGH = {
    "lima-sandbox/provider.py::LimaSandboxHandle.exec::asyncio.create_subprocess_exec": (
        "the sandbox provider seam: core's spawn passes the env it built in **kwargs"
    ),
}

#: Spawns of a literal ``["git", …]`` that need no ``git_argv``, and why.
GIT_ARGV_EXEMPT = {
    "notes/app_cli.py::_git_version::subprocess.run": "`git --version`: no repository is read",
    "spec-builder/app_cli.py::_git_version::subprocess.run": (
        "`git --version`: no repository is read"
    ),
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


def _is_builder_call(node: ast.AST) -> bool:
    if isinstance(node, ast.IfExp):
        return _is_builder_call(node.body) and _is_builder_call(node.orelse)
    return isinstance(node, ast.Call) and _callee(node).split(".")[-1] in BUILDERS


def env_source(call: ast.Call, func: ast.AST | None) -> str:
    """``built``, ``own`` (inherited, or a copy of the environment) or ``passthrough``."""
    env = [k for k in call.keywords if k.arg == "env"]
    if not env:
        return "passthrough" if any(k.arg is None for k in call.keywords) else "own"
    value = env[0].value
    if _is_builder_call(value):
        return "built"
    if isinstance(value, ast.Name) and func is not None:
        bound = [
            node.value
            for node in ast.walk(func)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == value.id for t in node.targets)
        ]
        if bound and all(_is_builder_call(b) for b in bound):
            return "built"
    return "own"


def _argv_kind(node: ast.AST, bound: dict[str, str]) -> str:
    """``neutral`` for an argv ``git_argv`` built, ``raw`` for one that starts with the word
    ``git``, ``""`` for anything else. *bound* maps the names a function set to one of those."""
    if isinstance(node, ast.Starred):
        return _argv_kind(node.value, bound)
    if isinstance(node, ast.Call) and _callee(node).split(".")[-1] == "git_argv":
        return "neutral"
    if isinstance(node, ast.Name):
        return bound.get(node.id, "")
    if isinstance(node, ast.BinOp):  # ["git", ...] + args
        return _argv_kind(node.left, bound)
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _argv_kind(node.elts[0], bound)
    if isinstance(node, ast.Constant) and node.value == "git":
        return "raw"
    return ""


def argv_kind(call: ast.Call, func: ast.AST | None) -> str:
    """How a spawn builds a git argv: ``neutral``, ``raw``, or ``""`` when it runs no git."""
    if not call.args:
        return ""
    bound: dict[str, str] = {}
    for node in ast.walk(func) if func is not None else ():
        if isinstance(node, ast.Assign):
            kind = _argv_kind(node.value, {})
            for target in node.targets:
                if kind and isinstance(target, ast.Name):
                    bound[target.id] = kind
    return _argv_kind(call.args[0], bound)


def _is_bundle_code(path: pathlib.Path, bundle: pathlib.Path) -> bool:
    rel = path.relative_to(bundle)
    return not (
        path.name.startswith("test_")
        or path.name == "conftest.py"
        or "tests" in rel.parts
        or any(part.startswith(".") for part in rel.parts)
    )


def census(root: pathlib.Path = ROOT) -> dict[str, list[tuple[int, str, str]]]:
    """``bundle/file::qualname::callee`` → ``[(line, env source, git argv kind), …]`` for every
    spawn site."""
    out: dict[str, list[tuple[int, str, str]]] = {}
    for manifest in sorted(root.glob("*/app.json")):
        bundle = manifest.parent
        for path in sorted(bundle.rglob("*.py")):
            if not _is_bundle_code(path, bundle):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            rel = path.relative_to(root).as_posix()

            class V(ast.NodeVisitor):
                def __init__(self) -> None:
                    self.q: list[str] = []
                    self.funcs: list[ast.AST] = []

                def visit_FunctionDef(self, n: ast.AST) -> None:
                    self.q.append(n.name)  # type: ignore[attr-defined]
                    self.funcs.append(n)
                    self.generic_visit(n)
                    self.funcs.pop()
                    self.q.pop()

                visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

                def visit_ClassDef(self, n: ast.AST) -> None:
                    self.q.append(n.name)  # type: ignore[attr-defined]
                    self.generic_visit(n)
                    self.q.pop()

                def visit_Call(self, n: ast.Call) -> None:
                    callee = _callee(n)
                    tail = ".".join(callee.split(".")[-2:])
                    if tail in SPAWNS:
                        key = f"{rel}::{'.'.join(self.q) or '<module>'}::{tail}"
                        func = self.funcs[-1] if self.funcs else None
                        spot = (n.lineno, env_source(n, func), argv_kind(n, func))
                        out.setdefault(key, []).append(spot)
                    self.generic_visit(n)

            V().visit(tree)
    return out


def _resolver_problems() -> list[str]:
    """The positive control: the resolver on every shape it has to tell apart."""
    src = (
        "def f(kw, flag):\n"
        "    subprocess.run(['x'])\n"
        "    subprocess.run(['x'], env={**os.environ})\n"
        "    subprocess.run(['x'], env=child_process_env())\n"
        "    subprocess.run(['x'], env=app_packages_env() if flag else child_process_env())\n"
        "    subprocess.run(['x'], env=app_packages_env() if flag else dict(os.environ))\n"
        "    subprocess.run(['x'], **kw)\n"
        "    built = child_process_env({'A': '1'})\n"
        "    subprocess.run(['x'], env=built)\n"
    )
    func = ast.parse(src).body[0]
    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call) and _callee(n) == "subprocess.run"]
    got = [env_source(c, func) for c in calls]
    want = ["own", "own", "built", "built", "own", "passthrough", "built"]
    found = [] if got == want else [f"the resolver read {got}, expected {want}"]
    src = (
        "def g(args):\n"
        "    subprocess.run(['git', '-C', repo, *args])\n"
        "    subprocess.run(git_argv(['-C', repo, *args]))\n"
        "    cmd = ['git', *args]\n"
        "    subprocess.run(cmd)\n"
        "    subprocess.run(['rsync', '-a'])\n"
    )
    func = ast.parse(src).body[0]
    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call) and _callee(n) == "subprocess.run"]
    got = [argv_kind(c, func) for c in calls]
    want = ["raw", "neutral", "raw", ""]
    return found + ([] if got == want else [f"the git argv matcher read {got}, expected {want}"])


def problems(root: pathlib.Path = ROOT) -> list[str]:
    found = _resolver_problems()
    sites = census(root)
    classified = set(BUILT) | set(OWN_ENVIRONMENT) | set(PASS_THROUGH)
    if len(sites) < len(classified):
        found.append(f"only {len(sites)} spawn sites found for {len(classified)} classified")
    for key in sorted(set(sites) - classified):
        lines = ", ".join(str(spot[0]) for spot in sites[key])
        found.append(
            f"{key} (line {lines}) is not classified: pass env=child_process_env() (or "
            "app_packages_env()) and add it to BUILT, or say in OWN_ENVIRONMENT why it keeps "
            "the environment it runs in"
        )
    for key in sorted(classified - set(sites)):
        found.append(f"{key} is classified but no longer exists; remove it")
    raw_git = {key for key, spots in sites.items() if any(spot[2] == "raw" for spot in spots)}
    for key in sorted(raw_git - set(GIT_ARGV_EXEMPT)):
        found.append(
            f"{key} builds its git argv by hand: use personalclaw.sdk.git.git_argv(args), so a "
            "repository's own configuration cannot run a program, or say in GIT_ARGV_EXEMPT why "
            "it reads no repository"
        )
    for key in sorted(set(GIT_ARGV_EXEMPT) - raw_git):
        found.append(f"{key} is exempt from git_argv but runs no hand-built git now; remove it")
    for key, spots in sorted(sites.items()):
        for line, source, _kind in spots:
            where = f"{key.split('::')[0]}:{line}"
            if key in BUILT and source != "built":
                found.append(f"{where} must pass env= from the SDK's child environment: {key}")
            elif key in OWN_ENVIRONMENT and source == "built":
                found.append(f"{where} builds its env now; move it to BUILT: {key}")
            elif key in PASS_THROUGH and source != "passthrough":
                found.append(f"{where} no longer passes its caller's env through: {key}")
    return found


def main() -> int:
    found = problems()
    if found:
        print("A child an app's code starts must say where its environment comes from:")
        for line in found:
            print(f"  {line}")
        return 1
    print(f"OK: {len(census())} spawn site(s), {len(BUILT)} on the child allowlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
