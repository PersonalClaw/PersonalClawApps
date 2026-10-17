"""The bundle lock: every bundle's declared Python dependencies, resolved once and hashed.

``scripts/test_bundles.py`` builds each bundle's environment from this lock, never from the ranges
an ``app.json`` declares, so a run installs exactly what the lock names, every file checked
against its sha256. The lock is a generated uv project per platform under ``.github/bundle-lock/``
(``linux-x86_64`` for CI, ``darwin-arm64`` for a maintainer's machine), each a ``pyproject.toml``
and its ``uv.lock``:

- its dependencies are the core commit ``core.lock`` pins and the pytest pins CI installs;
- it has one extra per bundle that declares ``dependencies.pythonDependencies``, named after the
  bundle's folder, holding exactly what the bundle declares;
- it resolves for one platform and one exact Python. A lock listing both platforms dropped
  ``sherpa-onnx-core`` (which ``sherpa-onnx`` requires) from ``diarization-onnx``'s export, and a
  hashed install of that export is refused; one platform per lock keeps it.

Every bundle shares one resolution, so two bundles that declare the same package get the same
version of it. When two bundles' ranges conflict, name the pair in ``CONFLICTS`` (uv's conflicting
extras), which resolves each side on its own; never leave a bundle out of the lock.

    python scripts/bundle_lock.py write   # regenerate both platforms' pyproject.toml and uv.lock
    python scripts/bundle_lock.py check   # fail when either is stale (the static bundle-lock row)

``write`` resolves with ``--refresh``, from what the index publishes now rather than from uv's
cache: a lock written from cached metadata once recorded ``sherpa-onnx`` with no dependencies,
so ``sherpa-onnx-core`` was missing and the hashed install refused it. ``check`` reads each lock
offline and refuses one in which a package names a dependency the lock holds no entry for, and
says which, and refuses one whose own root entry no longer depends on what an extra (or the base)
asks for, which ``uv lock --check`` accepts, before it asks ``uv lock --check``. Every uv call runs
without the uv settings ``UV_SETTINGS`` names, which uv reads from the environment even under
``--no-config``, so none of them can make a lock written or checked locally differ from the one
CI checks; a local uv of another version than CI's still can. Neither catches a lock whose cached
metadata left an edge out altogether: that lock is self-consistent, and only re-resolving shows
it.

Run ``write`` after changing any bundle's ``pythonDependencies`` or ``core.lock``, and commit the
result. Nothing refreshes the lock on a schedule: it moves when a bundle or core.lock does.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ROOT / ".github" / "bundle-lock"

#: Each platform a lock is generated for, and the marker that limits its resolution to it.
PLATFORMS = {
    "linux-x86_64": "sys_platform == 'linux' and platform_machine == 'x86_64'",
    "darwin-arm64": "sys_platform == 'darwin' and platform_machine == 'arm64'",
}
#: The one Python every environment is built with, as CI pins it.
PYTHON = "3.12.15"
#: What every environment holds beside core: the pins CI's own pytest installs use.
BASE = ["pytest==9.1.1", "pytest-asyncio==1.4.0", "pytest-xdist==3.8.0"]
#: The one index every lock resolves from, and every environment installs from.
INDEX = "https://pypi.org/simple"
#: Pairs of bundles whose declared ranges cannot share one resolution (none today).
CONFLICTS: list[tuple[str, str]] = []


def declared(root: Path = ROOT) -> dict[str, list[str]]:
    """Each bundle folder that declares ``pythonDependencies``, and what it declares."""
    out: dict[str, list[str]] = {}
    for manifest in sorted(root.glob("*/app.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        dependencies = (data.get("dependencies") or {}).get("pythonDependencies") or []
        if not isinstance(dependencies, list) or not all(
            isinstance(d, str) and d.strip() for d in dependencies
        ):
            raise ValueError(
                f"{manifest}: dependencies.pythonDependencies must be a list of non-empty strings"
            )
        if dependencies:
            out[manifest.parent.name] = list(dependencies)
    return out


def _core_spec(root: Path) -> str:
    spec = importlib.util.spec_from_file_location("_bundle_lock_core_lock",
                                                  root / "scripts" / "core_lock.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.spec(module.read_lock(root / "core.lock"))


def _array(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def pyproject(root: Path = ROOT, *, marker: str, python: str = PYTHON,
              base: list[str] | None = None, index: str = INDEX,
              core: str | None = "core.lock") -> str:
    """The generated ``pyproject.toml`` for one platform. *core* is an install spec, or
    ``"core.lock"`` for the commit it pins, or None for a lock with no core in it."""
    if core == "core.lock":
        core = _core_spec(root)
    dependencies = ([core] if core else []) + list(BASE if base is None else base)
    lines = [
        "# Generated by scripts/bundle_lock.py; do not edit. Run `python scripts/bundle_lock.py",
        "# write` after changing a bundle's pythonDependencies or core.lock.",
        "[project]",
        'name = "apps-bundle-lock"',
        'version = "0"',
        f'requires-python = "=={python}"',
        f"dependencies = {_array(dependencies)}",
        "",
        "[project.optional-dependencies]",
    ]
    lines += [f"{name} = {_array(deps)}" for name, deps in declared(root).items()]
    lines += [
        "",
        "[tool.uv]",
        "package = false",
        f"environments = {_array([marker])}",
    ]
    if CONFLICTS:
        pairs = ", ".join("[" + ", ".join('{ extra = "%s" }' % name for name in pair) + "]"
                          for pair in CONFLICTS)
        lines.append(f"conflicts = [{pairs}]")
    lines += ["", "[[tool.uv.index]]", f"url = {json.dumps(index)}", "default = true", ""]
    return "\n".join(lines)


def host_platform() -> str | None:
    """This machine's lock, or None when no lock is generated for it."""
    name = f"{platform.system().lower()}-{platform.machine().lower()}"
    return {"linux-x86_64": "linux-x86_64", "linux-amd64": "linux-x86_64",
            "darwin-arm64": "darwin-arm64"}.get(name)


def host_marker() -> str:
    return f"sys_platform == '{sys.platform}' and platform_machine == '{platform.machine()}'"


def project_index(directory: Path) -> str:
    """The index a generated project names. ``--no-config`` keeps a user's uv settings out, and
    it keeps the project's own ``[[tool.uv.index]]`` out too, so every uv call passes it."""
    project = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    indexes = ((project.get("tool") or {}).get("uv") or {}).get("index") or []
    return next((i["url"] for i in indexes if i.get("default")), INDEX)


#: Settings uv reads from the environment even under ``--no-config``. ``UV_INDEX``,
#: ``UV_EXTRA_INDEX_URL`` and ``UV_FIND_LINKS`` put a source ahead of ``--default-index``, and the
#: resolver ones are written into the lock's ``[options]``. ``UV_CONFIG_FILE`` names a uv.toml that
#: ``--no-config`` still loads, which can hold any of them, and ``UV_PREVIEW`` (or
#: ``UV_PREVIEW_FEATURES``) can leave the root's ``[package.metadata]`` out. ``UV_FROZEN`` turns
#: ``write``'s relock into a no-op. A lock written or checked with any of them set differs from
#: CI's, so every uv call here runs without them.
UV_SETTINGS = ("UV_INDEX", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL",
               "UV_FIND_LINKS", "UV_NO_INDEX", "UV_RESOLUTION", "UV_PRERELEASE",
               "UV_INDEX_STRATEGY", "UV_FORK_STRATEGY", "UV_EXCLUDE_NEWER", "UV_CONFIG_FILE",
               "UV_PREVIEW", "UV_PREVIEW_FEATURES", "UV_FROZEN", "UV_LOCKED")


def uv_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in UV_SETTINGS}


def uv_lock(directory: Path, *extra: str) -> int:
    return subprocess.run(["uv", "lock", "--no-config", "-q", "--project", str(directory),
                           "--default-index", project_index(directory), *extra],
                          cwd=ROOT, check=False, env=uv_env()).returncode


def lock_graph(text: str) -> dict:
    """What a ``uv.lock`` records, read offline: every package it holds an entry for, and the
    names each entry depends on (its dependencies and every extra's)."""
    data = tomllib.loads(text)
    packages: set[str] = set()
    dependencies: dict[str, set[str]] = {}
    for entry in data.get("package") or []:
        name = entry["name"]
        packages.add(name)
        needs = dependencies.setdefault(name, set())
        groups = [entry.get("dependencies") or []]
        for table in ("optional-dependencies", "dev-dependencies"):
            groups += list((entry.get(table) or {}).values())
        for group in groups:
            needs.update(dep["name"] for dep in group)
    return {"packages": packages, "dependencies": dependencies}


def missing_dependencies(text: str) -> list[str]:
    """Each dependency a package in the lock names that the lock holds no entry for. Such a lock
    exports a requirements list that a hashed install refuses; this names each broken edge from
    the lock's own records, offline."""
    graph = lock_graph(text)
    return [
        f"{name} needs {dep}, which the lock holds no entry for"
        for name, needs in sorted(graph["dependencies"].items())
        for dep in sorted(needs - graph["packages"])
    ]


def canonical(name: str) -> str:
    """A distribution name as a lock records it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return canonical(match.group(1)) if match else requirement.strip()


def requirement_extras(requirement: str) -> set[str]:
    """The extras a requirement asks of its package (``pkg[a,b]``), as a lock records them."""
    match = re.match(r"\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*\[([^\]]*)\]", requirement)
    return {canonical(e.strip()) for e in match.group(1).split(",") if e.strip()} if match else set()


def lock_environment(project_text: str) -> dict[str, str]:
    """The marker variables a generated project's one platform and one Python fix."""
    data = tomllib.loads(project_text)
    uv = (data.get("tool") or {}).get("uv") or {}
    fixed = dict(re.findall(r"(\w+) == '([^']*)'", " and ".join(uv.get("environments") or [])))
    system = {"linux": "Linux", "darwin": "Darwin", "win32": "Windows"}.get(
        fixed.get("sys_platform", ""))
    if system:
        fixed.setdefault("platform_system", system)
        fixed.setdefault("os_name", "nt" if system == "Windows" else "posix")
    python = str((data.get("project") or {}).get("requires-python") or "").removeprefix("==")
    if python:
        fixed.update(python_full_version=python, python_version=".".join(python.split(".")[:2]))
    return fixed


def applies(requirement: str, fixed: dict[str, str], extra: str = "") -> bool:
    """Whether *requirement* holds where *fixed* puts a lock: uv records no edge, and exports
    nothing, for one whose marker is false there. One that cannot be read or evaluated is asked
    for (fail closed)."""
    if ";" not in requirement:
        return True
    from packaging.markers import UndefinedComparison, UndefinedEnvironmentName
    from packaging.requirements import InvalidRequirement, Requirement
    try:
        marker = Requirement(requirement).marker
        return marker is None or marker.evaluate({**fixed, "extra": extra})
    except (InvalidRequirement, UndefinedComparison, UndefinedEnvironmentName):
        return True


def _shown(edges: dict[str, set[str]]) -> list[str]:
    return sorted(n + (f"[{','.join(sorted(x))}]" if x else "") for n, x in edges.items())


def edge_problems(lock_text: str, project_text: str) -> list[str]:
    """Each place the lock's own root entry no longer depends on what its pyproject.toml asks
    for: an extra whose edges were lost, or that lost an extra it asks of a package (a
    hand-resolved merge conflict, say), exports less than its bundle declares. A requirement
    whose marker is false on the lock's platform and Python needs no edge. Read offline."""
    project = tomllib.loads(project_text).get("project") or {}
    entries = tomllib.loads(lock_text).get("package") or []
    root = next((e for e in entries if e.get("source") == {"virtual": "."}), None)
    if root is None:
        return ["the lock holds no entry for the generated project itself"]
    fixed = lock_environment(project_text)
    # uv leaves an extra off an edge when it adds nothing on the lock's platform, and the
    # package's own entry then records no such extra: only extras an entry records are asked.
    recorded: dict[str, set[str]] = {}
    for entry in entries:
        recorded.setdefault(entry["name"], set()).update(entry.get("optional-dependencies") or {})

    def wanted(requirements, extra=""):
        out: dict[str, set[str]] = {}
        for r in requirements:
            if applies(r, fixed, extra):
                name = requirement_name(r)
                out.setdefault(name, set()).update(requirement_extras(r) & recorded.get(name, set()))
        return out

    def held(group):
        out: dict[str, set[str]] = {}
        for dep in group or []:
            out.setdefault(dep["name"], set()).update(canonical(x) for x in dep.get("extra") or [])
        return out

    def allowed(requirements):
        return {requirement_name(r) for r in requirements}

    def fits(want, have, names):
        return all(n in have and x <= have[n] for n, x in want.items()) and set(have) <= names

    problems = []
    base = project.get("dependencies") or []
    want, have = wanted(base), held(root.get("dependencies"))
    if not fits(want, have, allowed(base)):
        problems.append(f"the lock's base depends on {_shown(have)}, not {_shown(want)}")
    edges = root.get("optional-dependencies") or {}
    for extra, requirements in sorted((project.get("optional-dependencies") or {}).items()):
        want = wanted(requirements, canonical(extra))
        have = held(edges.get(canonical(extra)))
        if not fits(want, have, allowed(requirements)):
            problems.append(f"the lock's {extra} extra depends on {_shown(have) or 'nothing'}, "
                            f"not {_shown(want)}")
    return problems


def write(root: Path = ROOT) -> int:
    for name, marker in PLATFORMS.items():
        directory = root / ".github" / "bundle-lock" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "pyproject.toml").write_text(pyproject(root, marker=marker), encoding="utf-8")
        # --refresh: resolve from what the index publishes now. A lock written from uv's cache
        # once recorded sherpa-onnx with no dependencies, leaving sherpa-onnx-core out.
        if uv_lock(directory, "--refresh"):
            print(f"bundle-lock: uv lock failed for {name}", file=sys.stderr)
            return 1
        print(f"bundle-lock: wrote {directory.relative_to(root).as_posix()}")
    return 0


def check(root: Path = ROOT) -> list[str]:
    """Why the committed lock is stale, one line each; empty when it is current."""
    problems = []
    for name, marker in PLATFORMS.items():
        directory = root / ".github" / "bundle-lock" / name
        project = directory / "pyproject.toml"
        if not project.is_file() or not (directory / "uv.lock").is_file():
            problems.append(f"{name}: no committed lock in {directory.relative_to(root)}")
            continue
        if project.read_text(encoding="utf-8") != pyproject(root, marker=marker):
            problems.append(f"{name}: pyproject.toml differs from what the bundles' app.json "
                            "files and core.lock generate")
            continue
        text = (directory / "uv.lock").read_text(encoding="utf-8")
        missing = missing_dependencies(text) + edge_problems(text, project.read_text(
            encoding="utf-8"))
        if missing:
            problems += [f"{name}: {line}" for line in missing]
        elif uv_lock(directory, "--check"):
            problems.append(f"{name}: uv.lock does not match its pyproject.toml")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/bundle_lock.py",
                                     description="Generate or check the bundle lock.")
    parser.add_argument("command", choices=("write", "check"))
    args = parser.parse_args(argv)
    if args.command == "write":
        return write()
    problems = check()
    for problem in problems:
        print(f"bundle-lock: {problem}")
    if problems:
        print("Run `python scripts/bundle_lock.py write` and commit both platforms' files.")
        return 1
    print(f"OK: the bundle lock is current for {', '.join(PLATFORMS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
