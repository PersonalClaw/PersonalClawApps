"""Run app-bundle tests, each bundle in a locked environment of its own, several at once.

Each bundle runs in an environment that holds core, the pytest pins, and exactly the Python
dependencies its ``app.json`` declares, installed from the bundle lock (``scripts/bundle_lock.py``)
with every file checked against its hash. Bundles that declare the same dependencies share one
environment, and a bundle that declares none gets the base one, which holds no bundle's
dependencies. So a bundle sees only what it declares, whatever ran before it.

An environment lives at ``.bundle-venvs/<key>/.venv``. Its key is a digest of the core commit, the
exact requirements it installs, the Python version and ABI, the OS, the architecture, the uv
version, the index and whether the run is locked or floating, so a change to any of them builds a
new environment and a stale one is never reused. It is built under ``.bundle-venvs/<key>.lock`` in
a temporary folder that is renamed into place only once it is complete. A build that fails leaves
nothing; one that is killed part way leaves only a temporary folder, which the next build of that
key or the sweep deletes. Each build names the bundles waiting on it before it starts. An
environment is reused only while its receipt is complete, what it holds is still what its receipt
recorded at build time, and its interpreter is still the Python and ABI its key names (a patch
upgrade re-points the folder uv links it through); otherwise it is built again. Each bundle asks
again just before its pytest starts: one whose environment's interpreter moved to another Python
during the run (a ``uv python upgrade`` that landed after the reuse check) is not run and fails
with exit 2, naming both Pythons, and the environment is discarded at the end.

One checkout runs one bundle runner at a time: a run holds ``.bundle-venvs/.runner.lock``
exclusively, and so does every pytest it starts, until the last of them has exited. A second run
refuses at start and names the first. Each run removes the temporary folders no build holds and
the environments it does not plan for that no run has used for seven days; it removes only
folders named as the runner names them.

The runner records each environment's installed set before and after every bundle. A bundle
that had its environment to itself while the set changed fails the run, by name. When the set
changed while several bundles shared the environment, each of them counts as failed until it has
been run again alone, in the environment built afresh, and only the one whose run changes it is
named; if none does, all of them fail. The runs alone only diagnose: every bundle's result is its
first run's (a culprit's raised to a failure), and so is its JUnit.
A changed environment is discarded at the end. Each bundle's name is printed when it starts,
so one that hangs is the one that started and never printed its block.

A full run that finds no tested bundle exits 2; only ``--changed`` on a diff no bundle reads
selects none and passes.

``--jobs N`` runs N bundles at once (default: half the CPUs, at most 8). Each bundle still runs in
its own pytest process, and its output is printed as one block when it finishes.
``--changed BASE`` runs only the bundles whose folder differs from BASE (``git diff BASE...HEAD``
and the uncommitted work tree); a change outside every bundle that can change any bundle's run
selects every bundle, as does a diff git cannot read. It also always runs every bundle whose
``app.json`` claims ``quality.tested``, and says so in the selection's reason: core's quality
verifier judges that claim from this run's results, and a claimant the run left out has no result,
which fails the claim. ``--float`` resolves each bundle's declared ranges afresh instead of
installing the lock: the weekly canary's mode, never merge evidence.

``--results DIR`` writes ``DIR/results.json`` after every bundle, with each bundle's JUnit XML and
each environment's receipt beside it. Its ``apps`` list is the file core's quality verifier reads
(``python -m personalclaw.apps.quality TREE --results DIR``): one entry per bundle that finished,
``{"app": <folder>, "outcome": "pass" | "fail", "junit": <file in DIR>}``, ``pass`` only for a
bundle whose result is 0; ``junit`` is left out when pytest wrote none. A bundle still running when
the run stopped has no entry, so a claim on it fails rather than passes.

    python scripts/test_bundles.py [BUNDLE ...] [--jobs N] [--changed BASE] [--float]
                                   [--core-commit SHA] [--results DIR] [--list]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]

#: What an environment's key is a digest of.
KEY_FIELDS = ("core", "requirements", "python", "abi", "os", "arch", "uv", "index", "resolution")

#: Changed paths that no bundle's run reads, so they select no bundle.
INERT = re.compile(
    r"^(?:docs/.*|[^/]+\.md|LICENSE|\.gitignore|\.github/ISSUE_TEMPLATE/.*"
    r"|\.github/PULL_REQUEST_TEMPLATE\.md|\.github/CODEOWNERS)$"
)

#: Settings stripped from every uv call: the index ones, so the lock's own index is the only one,
#: and the Python-preference ones, so the runner's explicit ``--python-preference`` is the only
#: one. uv refuses ``--managed-python`` beside ``UV_PYTHON_PREFERENCE`` (CI sets that for every
#: step), and an empty ``UV_PYTHON_PREFERENCE`` is an error of its own.
_INDEX_VARIABLES = ("UV_INDEX", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL",
                    "UV_FIND_LINKS", "UV_NO_INDEX", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL",
                    "PIP_FIND_LINKS", "VIRTUAL_ENV", "UV_PYTHON_PREFERENCE",
                    "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON", "UV_CONFIG_FILE",
                    "UV_PREVIEW", "UV_PREVIEW_FEATURES")

_PROBE = r"""
import importlib.metadata as md, json, platform, sys, sysconfig
system = platform.system()
if system == "Linux":
    libc = platform.libc_ver()
    system = f"{system} {libc[0]} {libc[1]}"
elif system == "Darwin":
    system = f"{system} {platform.mac_ver()[0]}"
try:
    direct = json.loads(md.distribution("personalclaw").read_text("direct_url.json") or "{}")
    commit = (direct.get("vcs_info") or {}).get("commit_id")
except md.PackageNotFoundError:
    commit = "absent"
print(json.dumps({"python": platform.python_version(),
                  "abi": sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag,
                  "os": system, "arch": platform.machine(), "core_commit": commit}))
"""


class Refusal(Exception):
    """A run that cannot start: the lock does not hold what the bundles declare."""


def _has_tests(bundle: Path) -> bool:
    return any(bundle.glob("test_*.py")) or any(bundle.glob("tests/test_*.py"))


def discover_bundles(root: Path = ROOT) -> list[Path]:
    """Return every top-level app bundle that ships tests."""
    return [
        bundle
        for bundle in sorted(root.iterdir())
        if bundle.is_dir() and (bundle / "app.json").is_file() and _has_tests(bundle)
    ]


def declared_dependencies(bundle: Path) -> list[str]:
    """Load and validate ``dependencies.pythonDependencies`` from one manifest."""
    manifest = bundle / "app.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    dependencies = (data.get("dependencies") or {}).get("pythonDependencies") or []
    if not isinstance(dependencies, list) or not all(
        isinstance(dependency, str) and dependency.strip() for dependency in dependencies
    ):
        raise ValueError(
            f"{manifest}: dependencies.pythonDependencies must be a list of non-empty strings"
        )
    return dependencies


def claims_tested(bundle: Path) -> bool:
    """Whether the bundle's ``app.json`` claims ``quality.tested`` (exactly ``true``, as core's
    ``QualityDeclaration.claims()`` reads it). An unreadable manifest claims nothing here: the
    manifest-validate check and the verifier's own ``manifest`` violation report it."""
    try:
        data = json.loads((bundle / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    quality = data.get("quality") if isinstance(data, dict) else None
    return isinstance(quality, dict) and quality.get("tested") is True


def quality_results(rows: Sequence[Mapping]) -> list[dict]:
    """The ``apps`` list of ``results.json``, in the format core's quality verifier reads: one
    entry per finished bundle at the top of the tree, ``pass`` only for a result of 0."""
    entries = []
    for row in sorted(rows, key=lambda r: r["bundle"]):
        if "/" in row["bundle"]:
            continue  # not an app folder of the tree; the verifier would refuse the whole file
        entry = {"app": row["bundle"], "outcome": "pass" if row["returncode"] == 0 else "fail"}
        if row.get("junit"):
            entry["junit"] = row["junit"]
        entries.append(entry)
    return entries


def default_jobs() -> int:
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def _load(name: str, root: Path = ROOT):
    spec = importlib.util.spec_from_file_location(f"_test_bundles_{name}",
                                                  root / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _label(bundle: Path, root: Path = ROOT) -> str:
    try:
        return bundle.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(bundle)


# -- selection --------------------------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def select_changed(base: str | None, root: Path = ROOT) -> tuple[list[Path], str]:
    """The tested bundles a change since *base* can affect, and why. Fails safe: whenever the
    diff cannot be read, or a path outside every bundle can change any bundle's run, every
    bundle."""
    every = discover_bundles(root)

    def all_because(why: str) -> tuple[list[Path], str]:
        return every, f"{why}: every bundle"

    if not base:
        return all_because("no base named for --changed")
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode or inside.stdout.strip() != "true":
        detail = ((inside.stderr or "").strip().splitlines() or ["not a checkout"])[0]
        return all_because(f"git cannot read the diff ({detail})")
    if _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        return all_because("a shallow clone, whose history may not reach the base")
    if _git(root, "cat-file", "-e", f"{base}^{{commit}}").returncode:
        return all_because(f"unknown base {base}")
    paths: set[str] = set()
    # --no-renames: a rename is listed as its old path and its new one, so a file moved out of a
    # bundle or out of a shared path is classified like the deletion it also is.
    for argv in (["diff", "--name-only", "--no-renames", f"{base}...HEAD"],
                 ["diff", "--name-only", "--no-renames", "HEAD"],
                 ["ls-files", "--others", "--exclude-standard"]):
        run = _git(root, *argv)
        if run.returncode:
            return all_because(f"git cannot read the diff (git {' '.join(argv)}: "
                               f"{run.stderr.strip()})")
        paths.update(line for line in run.stdout.splitlines() if line)
    tested = {b.name: b for b in every}
    chosen: set[str] = set()
    deleted: set[str] = set()
    inert: list[str] = []
    for path in sorted(paths):
        top, _, rest = path.partition("/")
        if rest and (root / top / "app.json").is_file():
            if top in tested:
                chosen.add(top)
            continue
        if rest and not (root / top).exists() and _git(
                root, "cat-file", "-e", f"{base}:{top}/app.json").returncode == 0:
            deleted.add(top)
            continue
        if INERT.match(path):
            inert.append(path)
            continue
        return all_because(f"changed since {base}: {path} can change every bundle's run")
    reason = f"changed since {base}: " + (", ".join(sorted(chosen)) if chosen else "no bundle")
    if inert:
        reason += f" (and {len(inert)} path(s) no bundle reads)"
    if deleted:
        reason += f"; deleted, so nothing to run: {', '.join(sorted(deleted))}"
    # The quality verifier judges a `tested` claim from this run's results only, so every
    # claimant runs, touched or not: one left out would have no result, and its claim would fail.
    claimants = sorted(name for name, b in tested.items()
                       if name not in chosen and claims_tested(b))
    if claimants:
        chosen.update(claimants)
        reason += ("; plus every bundle claiming quality.tested, whose result the quality check "
                   f"reads: {', '.join(claimants)}")
    return [tested[name] for name in sorted(chosen)], reason


def select(named: Sequence[str | Path] = (), changed: str | None = None,
           use_changed: bool = False, root: Path = ROOT) -> tuple[list[Path], str]:
    if named:
        # Once each, however often or however it is spelled: a run tracks a bundle by its label.
        unique: dict[str, Path] = {}
        for p in named:
            bundle = Path(p) if Path(p).is_absolute() else root / p
            unique.setdefault(_label(bundle, root), bundle)
        return sorted(unique.values(), key=lambda b: b.name), "named on the command line"
    if use_changed:
        return select_changed(changed, root)
    return discover_bundles(root), "every app bundle that ships tests"


# -- the environment key ----------------------------------------------------------------------

def environment_key(inputs: Mapping[str, str]) -> str:
    canonical = json.dumps({field: inputs[field] for field in KEY_FIELDS}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _uv_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in _INDEX_VARIABLES:
        env.pop(name, None)
    return env


def _uv_version() -> str:
    out = subprocess.run(["uv", "--version"], capture_output=True, text=True, check=True).stdout
    return out.split()[1]


def _probe(python: str) -> dict:
    run = subprocess.run([python, "-c", _PROBE], capture_output=True, text=True, check=False,
                         env=_uv_env())
    if run.returncode:
        raise OSError(f"cannot run {python}: {run.stderr.strip()}")
    return json.loads(run.stdout)


def host_inputs(python: str) -> dict[str, str]:
    """The key inputs this machine gives an environment built from *python*."""
    probe = _probe(python)
    return {"python": probe["python"], "abi": probe["abi"], "os": probe["os"],
            "arch": probe["arch"], "uv": _uv_version()}


def core_identity(root: Path = ROOT, override: str | None = None) -> str:
    """The core an environment holds: *override*, else the commit ``core.lock`` pins."""
    if override:
        return override
    return _load("core_lock", root).read_lock(root / "core.lock")


def env_python(bundle_lock) -> str:
    """The interpreter environments are built from: uv's managed copy of the pinned Python."""
    override = os.environ.get("BUNDLE_RUNNER_ENV_PYTHON")
    if override:
        return override
    argv = ["uv", "python", "find", "--no-config", "--python-preference", "only-managed",
            "--no-project", bundle_lock.PYTHON]
    found = subprocess.run(argv, capture_output=True, text=True, check=False, env=_uv_env())
    if found.returncode:
        subprocess.run(["uv", "python", "install", "--no-config", "--python-preference",
                        "only-managed", bundle_lock.PYTHON], check=True, env=_uv_env())
        found = subprocess.run(argv, capture_output=True, text=True, check=True, env=_uv_env())
    return found.stdout.strip()


# -- what each bundle installs ----------------------------------------------------------------

@dataclass
class Plan:
    """One environment to build: its key, what it installs, and the bundles that run in it."""

    key: str
    inputs: dict
    requirements: str
    bundles: list


def _entries(requirements: str) -> list[list[str]]:
    entries: list[list[str]] = []
    for line in requirements.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace() and entries:
            entries[-1].append(line)
        else:
            entries.append([line])
    return entries


def unhashed(requirements: str) -> list[str]:
    """Each requirement in an exported lock that carries no hash."""
    return [re.split(r"[=<>!~ ;@]", entry[0].strip(), maxsplit=1)[0]
            for entry in _entries(requirements)
            if not any("--hash=" in line for line in entry)]


def _lock_dir(bundle_lock) -> Path:
    override = os.environ.get("BUNDLE_RUNNER_LOCK_DIR")
    if override:
        return Path(override)
    name = bundle_lock.host_platform()
    if name is None:
        raise Refusal(f"no bundle lock is generated for this machine; the lock covers "
                      f"{', '.join(bundle_lock.PLATFORMS)} (scripts/bundle_lock.py)")
    return ROOT / ".github" / "bundle-lock" / name


def _export(lock_dir: Path, extra: str | None) -> str:
    argv = ["uv", "export", "--no-config", "--frozen", "-q", "--project", str(lock_dir),
            "--default-index", _load("bundle_lock").project_index(lock_dir),
            "--no-emit-project", "--no-emit-package", "personalclaw", "--no-header",
            "--no-annotate", *(["--extra", extra] if extra else [])]
    run = subprocess.run(argv, capture_output=True, text=True, check=False, env=_uv_env())
    if run.returncode:
        raise Refusal(f"uv export of the bundle lock in {lock_dir} failed: {run.stderr.strip()}")
    return run.stdout


def plan(bundles: Sequence[Path], *, floating: bool, core: str,
         python: str) -> tuple[list[Plan], str]:
    """Group *bundles* by the environment each needs, refusing any bundle the lock does not hold
    exactly, or holds without a hash. Returns the plans and the index they install from."""
    bundle_lock = _load("bundle_lock")
    host = host_inputs(python)
    index, locked, lock_dir, fixed = bundle_lock.INDEX, {}, None, {}
    if not floating:
        lock_dir = _lock_dir(bundle_lock)
        project_text = (lock_dir / "pyproject.toml").read_text(encoding="utf-8")
        project = tomllib.loads(project_text)
        fixed = bundle_lock.lock_environment(project_text)
        lock_text = (lock_dir / "uv.lock").read_text(encoding="utf-8")
        broken = (bundle_lock.edge_problems(lock_text, project_text)
                  + bundle_lock.version_problems(lock_text, project_text))
        if broken:
            raise Refusal(f"the bundle lock in {lock_dir} no longer holds what its project asks "
                          f"for: {'; '.join(broken)}. Run `python scripts/bundle_lock.py write` "
                          "and commit the lock.")
        locked = (project.get("project") or {}).get("optional-dependencies") or {}
        indexes = ((project.get("tool") or {}).get("uv") or {}).get("index") or []
        index = next((i["url"] for i in indexes if i.get("default")), bundle_lock.INDEX)
    plans: dict[str, Plan] = {}
    exports: dict[str | None, str] = {}
    for bundle in bundles:
        name = bundle.name
        try:
            declared = declared_dependencies(bundle)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise Refusal(str(error)) from error
        if floating:
            requirements = "\n".join([*bundle_lock.BASE, *declared]) + "\n"
        else:
            if list(locked.get(name, [])) != declared:
                raise Refusal(
                    f"{name}: app.json declares {declared or 'no pythonDependencies'}, but the "
                    f"bundle lock holds {locked.get(name) or 'none'} for it. Run `python "
                    "scripts/bundle_lock.py write` and commit the lock."
                )
            extra = name if declared else None
            if extra not in exports:
                exports[extra] = _export(lock_dir, extra)
            requirements = exports[extra]
            held = {bundle_lock.canonical(re.split(r"[=<>!~ ;@\[]", entry[0].strip(),
                                                   maxsplit=1)[0])
                    for entry in _entries(requirements)}
            absent = [d for d in declared if bundle_lock.applies(d, fixed, name)
                      and bundle_lock.requirement_name(d) not in held]
            if absent:
                raise Refusal(
                    f"{name}: the bundle lock's export holds no {', '.join(absent)}, which its "
                    "app.json declares. Run `python scripts/bundle_lock.py write` and commit "
                    "the lock."
                )
            missing = unhashed(requirements)
            if missing:
                raise Refusal(
                    f"{name}: the bundle lock holds {', '.join(missing)} without a hash, and an "
                    "environment installs only hashed files. Lock it from an index that "
                    "publishes each file's hash."
                )
        inputs = {**host, "core": core, "index": index,
                  "requirements": hashlib.sha256(requirements.encode()).hexdigest(),
                  "resolution": "floating" if floating else "locked"}
        key = environment_key(inputs)
        plans.setdefault(key, Plan(key, inputs, requirements, [])).bundles.append(bundle)
    return list(plans.values()), index


# -- building an environment ------------------------------------------------------------------

_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        return _KEY_LOCKS.setdefault(key, threading.Lock())


@contextlib.contextmanager
def _held(envs: Path, key: str, mode: int):
    envs.mkdir(parents=True, exist_ok=True)
    with open(envs / f"{key}.lock", "a") as handle:
        fcntl.flock(handle, mode)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def _locked(envs: Path, key: str):
    """Exclusive, between this run's threads: an environment is built, rebuilt or deleted only
    under it."""
    with _key_lock(key), _held(envs, key, fcntl.LOCK_EX):
        yield


class Busy(Exception):
    """Another runner is using this checkout's environments."""


def hold_checkout(envs: Path, argv: Sequence[str]):
    """The checkout's one runner: an exclusive lock on ``<envs>/.runner.lock``, held until this
    process and every pytest it starts have exited. A second runner refuses, naming the first."""
    envs.mkdir(parents=True, exist_ok=True)
    handle = open(envs / ".runner.lock", "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        holder = handle.read().strip() or "a runner that left no description"
        handle.close()
        raise Busy(f"another bundle run is using {envs}: {holder}. One checkout runs one bundle "
                   "runner at a time; wait for it to finish, or run from another checkout.")
    handle.seek(0)
    handle.truncate()
    handle.write(f"process {os.getpid()}, started "
                 f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}, running "
                 f"`{' '.join(argv)}`\n")
    handle.flush()
    return handle


def _held_by_gate() -> tuple[int, ...]:
    """The gate's results-folder lock, when the gate passed it down, so each pytest holds it."""
    raw = os.environ.get("GATE_RESULTS_LOCK_FD", "")
    try:
        fd = int(raw)
        os.fstat(fd)
    except (ValueError, OSError):
        return ()
    return (fd,)


def _python(final: Path) -> Path:
    return final / ".venv" / "bin" / "python"


def _complete(final: Path, key: str, inputs: Mapping[str, str]) -> bool:
    """Whether *final* is still the environment its receipt says was built for *key*."""
    try:
        receipt = json.loads((final / "environment.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if receipt.get("complete") is not True or receipt.get("key") != key:
        return False
    # Changed since it was built: a bundle's test installed or removed something and the run that
    # saw it stopped before discarding it, or a discard was cut short.
    if receipt.get("installed") != sorted(snapshot(final)):
        return False
    try:
        probe = _probe(str(_python(final)))
    except (OSError, ValueError):
        return False
    # uv links an environment through its minor-version Python folder, which a patch upgrade
    # re-points: the interpreter must still be the Python the key names.
    if (probe["python"], probe["abi"]) != (inputs.get("python"), inputs.get("abi")):
        return False
    if probe["core_commit"] == "absent":
        return False
    core = inputs.get("core") or ""
    return not (re.fullmatch(r"[0-9a-f]{40}", core) and probe["core_commit"] != core)


def interpreter_moved(final: Path, inputs: Mapping[str, str]) -> str | None:
    """Why *final*'s interpreter is no longer the Python its key names, or None while it is.

    :func:`_complete` asks this once, when an environment is reused. A ``uv python upgrade`` that
    lands while a run goes on re-points the folder the environment links through after that, so
    each bundle asks again just before its pytest starts."""
    try:
        probe = _probe(str(_python(final)))
    except (OSError, ValueError) as error:
        return f"its interpreter cannot run ({error})"
    if (probe["python"], probe["abi"]) != (inputs.get("python"), inputs.get("abi")):
        return (f"its interpreter is now Python {probe['python']} ({probe['abi']}), not the "
                f"{inputs.get('python')} ({inputs.get('abi')}) its key and receipt name")
    return None


#: How long an environment no run plans for is kept, unused, before a run removes it.
KEEP_UNUSED_SECONDS = 7 * 24 * 3600


def sweep(envs: Path, current: set[str], *, older_than: float = KEEP_UNUSED_SECONDS) -> list[str]:
    """Remove what no run can use: a temporary folder no build holds, and an environment outside
    *current* that no run has used for *older_than* seconds. Each goes only under its key's
    exclusive lock taken without waiting, so one a run is building or using stays."""
    if not envs.is_dir():
        return []
    now, removed = time.time(), []

    def age(path: Path) -> float | None:
        stamp = path / "environment.json"
        try:
            return now - (stamp if stamp.exists() else path).stat().st_mtime
        except OSError:
            return None

    for path in sorted(envs.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        # Only the names the runner gives its folders: anything else here is not the sweep's.
        temporary = re.fullmatch(r"\.tmp-([0-9a-f]{32})-[0-9]+-[0-9]+", path.name)
        if temporary:
            key = temporary.group(1)
        elif re.fullmatch(r"[0-9a-f]{32}", path.name):
            key = path.name
            if key in current:
                continue
            old = age(path)
            if old is None or old < older_than:
                continue
        else:
            continue
        with _key_lock(key), open(envs / f"{key}.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                # Read again under the lock: a build may have used it since this run looked.
                if not temporary:
                    old = age(path)
                    if old is None or old < older_than:
                        continue
                shutil.rmtree(path, ignore_errors=True)
                if not os.path.lexists(path):
                    removed.append(path.name)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    return removed


def snapshot(final: Path) -> set[str]:
    """What an environment holds: each distribution as ``name==version``, and every other
    top-level entry of its site-packages."""
    held: set[str] = set()
    for packages in (final / ".venv").glob("lib/python*/site-packages"):
        for entry in packages.iterdir():
            if entry.name == "__pycache__":
                continue
            if entry.name.endswith(".dist-info"):
                meta: dict[str, str] = {}
                with contextlib.suppress(OSError):
                    for line in (entry / "METADATA").read_text(encoding="utf-8").splitlines():
                        if not line:
                            break
                        key, _, value = line.partition(": ")
                        if key in ("Name", "Version") and key not in meta:
                            meta[key] = value.strip()
                held.add(f"{meta.get('Name', entry.name)}=={meta.get('Version', '?')}")
            else:
                held.add(entry.name)
    return held


def _uv(*argv: str, log: list) -> None:
    log.append(["uv", *argv])
    run = subprocess.run(["uv", *argv], capture_output=True, text=True, check=False,
                         env=_uv_env())
    if run.returncode:
        raise OSError(f"uv {' '.join(argv[:2])} failed: {(run.stderr or run.stdout).strip()}")


def ensure(envs: Path, item: Plan, *, python: str, core_spec: str, index: str,
           rebuild: bool = False) -> tuple[Path, str, float]:
    """The environment for *item*, built under its lock or reused, and which it was."""
    started = time.monotonic()
    final = envs / item.key
    with _locked(envs, item.key):
        if not rebuild and _complete(final, item.key, item.inputs):
            # Its last-used time, which the sweep of environments no run plans for reads.
            with contextlib.suppress(OSError):
                os.utime(final / "environment.json")
            return final, "reused", time.monotonic() - started
        shutil.rmtree(final, ignore_errors=True)
        for stale in envs.glob(f".tmp-{item.key}-*"):
            shutil.rmtree(stale, ignore_errors=True)
        hold = os.environ.get("BUNDLE_RUNNER_REBUILD_HOLD") if rebuild else None
        while hold and os.path.exists(hold):
            time.sleep(0.05)  # a test's window onto a run while it settles
        temporary = envs / f".tmp-{item.key}-{os.getpid()}-{threading.get_ident()}"
        # Named before it builds, so a build that hangs names the bundles waiting on it.
        print(f"environment {item.key[:12]}: building for "
              f"{', '.join(b.name for b in item.bundles)}", file=sys.stderr, flush=True)
        try:
            _build(temporary, item, python=python, core_spec=core_spec, index=index)
        except BaseException:
            # A build that fails leaves nothing behind; one that is killed leaves only this
            # folder, which the next build of the key, or the sweep, removes.
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        os.rename(temporary, final)
    return final, "built", time.monotonic() - started


def _build(temporary: Path, item: Plan, *, python: str, core_spec: str, index: str) -> None:
    """Build *item*'s environment in *temporary*, its receipt written last."""
    venv = temporary / ".venv"
    installs: list = []
    _uv("venv", "--no-config", "-q", "--relocatable", "--python", python, str(venv),
        log=installs)
    pause = float(os.environ.get("BUNDLE_RUNNER_BUILD_PAUSE") or 0)
    if pause:
        time.sleep(pause)  # a test's window onto a build part way through
    target = str(venv / "bin" / "python")
    locked = item.inputs["resolution"] == "locked"
    if item.requirements.strip():
        requirements = temporary / "requirements.txt"
        requirements.write_text(item.requirements, encoding="utf-8")
        if locked:
            _uv("pip", "install", "--no-config", "-q", "--python", target, "--default-index",
                index, "--require-hashes", "-r", str(requirements), log=installs)
        else:
            _uv("pip", "install", "--no-config", "-q", "--python", target, "--default-index",
                index, "-r", str(requirements), core_spec, log=installs)
    if locked or not item.requirements.strip():
        # Core comes from its commit, which a hash cannot name; its own dependencies came
        # from the lock above, hashed.
        _uv("pip", "install", "--no-config", "-q", "--python", target, "--no-deps",
            core_spec, log=installs)
    receipt = {
        "key": item.key, "inputs": item.inputs, "core_spec": core_spec,
        "installs": installs, "installed": sorted(snapshot(temporary)),
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "complete": True,
    }
    (temporary / "environment.json").write_text(json.dumps(receipt, indent=2) + "\n",
                                                encoding="utf-8")


def discard(envs: Path, key: str) -> None:
    with _locked(envs, key):
        shutil.rmtree(envs / key, ignore_errors=True)


# -- running a bundle -------------------------------------------------------------------------

@dataclass
class Ran:
    returncode: int
    output: str


def run_pytest(bundle: Path, *, python: str, env: Mapping[str, str] | None = None,
               junit: Path | None = None, keep: tuple[int, ...] = ()) -> Ran:
    """One bundle's suite, in its own pytest process, with its output captured."""
    run_env = dict(os.environ if env is None else env)
    run_env.pop("VIRTUAL_ENV", None)
    run_env.pop("PYTHONPATH", None)
    # A test's way to reach pytest from an environment that does not hold it.
    if run_env.get("BUNDLE_RUNNER_TEST_PYTHONPATH"):
        run_env["PYTHONPATH"] = run_env["BUNDLE_RUNNER_TEST_PYTHONPATH"]
    run_env.setdefault("PERSONALCLAW_SKIP_APP_BACKENDS", "1")
    run_env.setdefault("PYTEST_XDIST_AUTO_NUM_WORKERS", "3")
    argv = [python, "-m", "pytest", str(bundle), "-q", "-p", "no:cacheprovider",
            *([f"--junitxml={junit}"] if junit is not None else [])]
    try:
        # *keep*: the checkout's runner lock and the gate's, so neither is let go while this
        # pytest, which writes into the results folder and runs in an environment, is alive.
        proc = subprocess.run(argv, cwd=ROOT, env=run_env, capture_output=True, text=True,
                              check=False, pass_fds=keep)
    except FileNotFoundError:
        return Ran(127, f"error: Python interpreter not found: {python}\n")
    return Ran(proc.returncode, proc.stdout + proc.stderr)


def _cores(args) -> tuple[str, str]:
    """The core environments hold (its identity in the key) and the spec that installs it."""
    planted = os.environ.get("BUNDLE_RUNNER_CORE_SPEC")
    if planted:
        return planted, planted
    core_lock = _load("core_lock")
    if args.floating and args.core_commit:
        return args.core_commit, core_lock.spec(args.core_commit)
    locked = core_identity(ROOT)
    if args.core_commit and not locked.startswith(args.core_commit.lower()):
        raise Refusal(f"the bundle lock resolves the core core.lock pins ({locked}), not "
                      f"{args.core_commit}; only --float tests another core")
    return locked, core_lock.spec(locked)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/test_bundles.py",
        description=("Run each app bundle's tests in a locked environment holding only what its "
                     "app.json declares. With no bundle arguments, run every tested bundle."),
    )
    parser.add_argument("bundles", nargs="*", type=Path, help="bundle paths (default: all)")
    parser.add_argument("--jobs", type=int, default=None,
                        help=f"bundles to run at once (default: {default_jobs()}, half the CPUs, "
                             "at most 8)")
    parser.add_argument("--changed", metavar="BASE", nargs="?", const="",
                        help="run only the bundles a change since BASE can affect")
    parser.add_argument("--float", action="store_true", dest="floating",
                        help="resolve each bundle's declared ranges afresh, not from the lock "
                             "(the weekly canary's mode; never merge evidence)")
    parser.add_argument("--core-commit", metavar="SHA",
                        help="the core commit environments hold (default: core.lock's); a "
                             "locked run refuses any other")
    parser.add_argument("--results", type=Path,
                        help="also write results.json (whose `apps` list core's quality verifier "
                             "reads with --results), each bundle's JUnit XML and each "
                             "environment's receipt into this directory")
    parser.add_argument("--list", action="store_true",
                        help="print the selection and its reason, and run nothing")
    args = parser.parse_args(argv)
    if args.bundles and args.changed is not None:
        parser.error("name bundles or pass --changed, not both")

    bundles, reason = select(args.bundles, args.changed, args.changed is not None)
    print(f"selection: {reason}", flush=True)
    if args.list:
        for bundle in bundles:
            print(_label(bundle))
        return 0
    for bundle in bundles:
        if not (bundle / "app.json").is_file():
            print(f"error: {bundle} is not an app bundle (missing app.json)", file=sys.stderr)
            return 2
        if not _has_tests(bundle):
            print(f"error: {bundle} has no test_*.py files", file=sys.stderr)
            return 2
    jobs = args.jobs or default_jobs()

    results_dir = args.results.resolve() if args.results is not None else None
    if results_dir is not None:
        (results_dir / "environments").mkdir(parents=True, exist_ok=True)
    summary: dict = {"selection": {"bundles": [_label(b) for b in bundles], "reason": reason},
                     "jobs": jobs, "resolution": "floating" if args.floating else "locked",
                     "bundles": [], "environments": []}
    guard = threading.Lock()

    def save() -> None:
        if results_dir is None:
            return
        summary["bundles"].sort(key=lambda row: row["bundle"])
        summary["apps"] = quality_results(summary["bundles"])
        (results_dir / "results.json").write_text(json.dumps(summary, indent=2) + "\n",
                                                  encoding="utf-8")

    if not bundles:
        if args.changed is None or not discover_bundles():
            # A full run, or --changed falling back to every bundle, that finds none has tested
            # nothing: only a diff that no bundle reads may select none and pass.
            print("error: no tested app bundles found", file=sys.stderr)
            return 2
        save()
        print("\nOK: 0 bundle(s) to run; no bundle selected")
        return 0
    envs = Path(os.environ.get("BUNDLE_RUNNER_ENVS") or ROOT / ".bundle-venvs")
    try:
        runner_lock = hold_checkout(envs, ["scripts/test_bundles.py", *(argv if argv is not None
                                                                      else sys.argv[1:])])
    except Busy as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    # Every pytest this run starts holds the checkout's lock, and the gate's when it passed one.
    keep = (runner_lock.fileno(), *_held_by_gate())
    try:
        core, core_spec = _cores(args)
        python = env_python(_load("bundle_lock"))
        plans, index = plan(bundles, floating=args.floating, core=core, python=python)
    except (Refusal, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    summary["swept"] = sweep(envs, {item.key for item in plans})
    if summary["swept"]:
        print(f"removed {len(summary['swept'])} environment folder(s) left by a stopped build or "
              f"unused for {KEEP_UNUSED_SECONDS // 86400} days", flush=True)
    builder = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    ready = {item.key: builder.submit(ensure, envs, item, python=python, core_spec=core_spec,
                                      index=index, rebuild=args.floating) for item in plans}
    changed_envs: set[str] = set()

    def environment(item: Plan) -> Path | None:
        try:
            final, action, seconds = ready[item.key].result()
        except OSError as error:
            with guard:
                if not any(e["key"] == item.key for e in summary["environments"]):
                    summary["environments"].append({"key": item.key, "action": "FAILED",
                                                    "error": str(error), "inputs": item.inputs})
                    print(f"error: environment {item.key[:12]}: {error}", file=sys.stderr)
            return None
        with guard:
            if not any(e["key"] == item.key for e in summary["environments"]):
                summary["environments"].append({
                    "key": item.key, "action": action, "seconds": round(seconds, 1),
                    "python": str(_python(final)), "inputs": item.inputs,
                })
                if results_dir is not None:
                    shutil.copy2(final / "environment.json",
                                 results_dir / "environments" / f"{item.key}.json")
                print(f"environment {item.key[:12]}: {action} in {seconds:.1f}s "
                      f"({len(item.bundles)} bundle(s))", flush=True)
        return final

    # Which bundles are in each environment now, and which shared one with each bundle while it
    # ran: a change seen by a bundle that had its environment to itself is that bundle's own;
    # one seen beside others is settled by running each of them alone.
    active: dict[str, set[str]] = {}
    beside: dict[str, set[str]] = {}
    suspects: dict[str, set[str]] = {}
    rows: dict[str, dict] = {}
    where: dict[str, tuple[Plan, Path]] = {}

    def changed_note(label: str, before: set[str], after: set[str]) -> str:
        added = ", ".join(sorted(after - before)) or "nothing"
        removed = ", ".join(sorted(before - after)) or "nothing"
        return (f"error: {label} changed its environment (added {added}; removed {removed}); a "
                "bundle's tests must not install or remove packages")

    def record(label: str, item: Plan, rc: int, started: float, junit: Path | None) -> None:
        rows[label] = {
            "bundle": label, "returncode": rc, "environment": item.key,
            "seconds": round(time.monotonic() - started, 1),
            "junit": junit.name if junit is not None and junit.is_file() else None,
        }
        summary["bundles"] = list(rows.values())

    def junit_for(label: str) -> Path | None:
        if results_dir is None:
            return None
        return results_dir / f"{label.replace('/', '__')}.junit.xml"

    first: dict[str, int] = {}

    def run_one(item: Plan, bundle: Path) -> None:
        label = _label(bundle)
        where[label] = (item, bundle)
        junit = junit_for(label)
        started = time.monotonic()
        final = environment(item)
        notes: list[str] = []
        moved = interpreter_moved(final, item.inputs) if final is not None else None
        if final is None:
            rc, output = 2, ""
            notes.append(f"error: {label}: its environment {item.key[:12]} could not be built")
        elif moved:
            # Not run: its result would be a Python's that no receipt names. The environment is
            # discarded at the end, and the next run builds it again.
            rc, output = 2, ""
            with guard:
                changed_envs.add(item.key)
            notes.append(f"error: {label}: not run, because environment {item.key[:12]} moved "
                         f"during this run: {moved}. The next run builds it again.")
        else:
            with guard:
                # Named before it runs, so a bundle that hangs is the one that started and
                # never printed its block.
                print(f"{label}: started", file=sys.stderr, flush=True)
                peers = active.setdefault(item.key, set())
                beside[label] = set(peers)
                for peer in peers:
                    beside[peer].add(label)
                peers.add(label)
            before = snapshot(final)
            ran = run_pytest(bundle, python=str(_python(final)), junit=junit, keep=keep)
            after = snapshot(final)
            with guard:
                active[item.key].discard(label)
                shared = beside.pop(label)
            rc, output = ran.returncode, ran.output
            if before != after:
                changed_envs.add(item.key)
                if shared:
                    with guard:
                        suspects.setdefault(item.key, set()).update({label, *shared})
                    notes.append(f"error: environment {item.key[:12]} changed while {label} ran "
                                 f"beside {', '.join(sorted(shared))}; each of them runs alone "
                                 "once the others are done, to find which")
                else:
                    notes.append(changed_note(label, before, after))
                    rc = rc or 3
        with guard:
            print(f"\n=== {label} ===\n{output}", end="" if output.endswith("\n") else "\n",
                  flush=True)
            for note in notes:
                print(note, file=sys.stderr, flush=True)
            first[label] = rc
            # A suspect counts as failed until settle() has run it alone, so a run stopped
            # before then never records one as passed, whichever of them finished first.
            pending = suspects.get(item.key, set())
            record(label, item, (rc or 3) if label in pending else rc, started, junit)
            for other in pending & rows.keys():
                rows[other]["returncode"] = rows[other]["returncode"] or 3
            summary["bundles"] = list(rows.values())
            # Written after every bundle, so a run stopped part way still says how far it got.
            save()

    def settle(key: str, labels: set[str]) -> None:
        """An environment changed while several bundles ran in it: run each alone, in it built
        afresh, to name the one that changes it. The runs alone only diagnose: every bundle's
        verdict is its first run's, a culprit's raised to a failure, and the first run's JUnit
        stays; each run alone writes its own beside it, under ``alone/``."""
        print(f"\nenvironment {key[:12]} changed while {', '.join(sorted(labels))} ran in it; "
              "running each alone to find which", file=sys.stderr, flush=True)
        culprits, undiagnosed = [], set()
        for label in sorted(labels):
            item, bundle = where[label]
            junit = junit_for(label)
            alone = None if junit is None else junit.parent / "alone" / junit.name
            print(f"{label} (alone): started", file=sys.stderr, flush=True)
            try:
                final, _, _ = ensure(envs, item, python=python, core_spec=core_spec, index=index,
                                     rebuild=True)
            except OSError as error:
                print(f"error: {label}: its environment {key[:12]} could not be rebuilt to run it "
                      f"alone: {error}", file=sys.stderr, flush=True)
                undiagnosed.add(label)
                continue
            before = snapshot(final)
            ran = run_pytest(bundle, python=str(_python(final)), junit=alone, keep=keep)
            after = snapshot(final)
            print(f"\n=== {label} (alone) ===\n{ran.output}",
                  end="" if ran.output.endswith("\n") else "\n", flush=True)
            if before != after:
                culprits.append(label)
                print(changed_note(label, before, after), file=sys.stderr, flush=True)
            elif ran.returncode != first[label]:
                print(f"{label}: alone it exits {ran.returncode}, beside the others it exited "
                      f"{first[label]}; its first run is its result", file=sys.stderr, flush=True)
        if not culprits:
            # No bundle changes it alone: the run cannot say which did, so none of them passes.
            print(f"error: environment {key[:12]} changed while {', '.join(sorted(labels))} ran "
                  "in it, and none of them changes it when run alone; a bundle's tests must not "
                  "install or remove packages", file=sys.stderr, flush=True)
        for label in labels:
            blamed = label in culprits or label in undiagnosed or not culprits
            rows[label]["returncode"] = (first[label] or 3) if blamed else first[label]
        summary["bundles"] = list(rows.values())
        changed_envs.add(key)
        save()

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for future in [pool.submit(run_one, item, b) for item in plans for b in item.bundles]:
            future.result()
    builder.shutdown()
    for key, labels in sorted(suspects.items()):
        settle(key, labels)
    for key in changed_envs:
        discard(envs, key)
    failed = [label for label, row in rows.items() if row["returncode"]]
    save()

    if failed:
        print(f"\nFAILED bundles: {', '.join(sorted(failed))}", file=sys.stderr)
        return 1
    print(f"\nOK: {len(bundles)} bundle(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
