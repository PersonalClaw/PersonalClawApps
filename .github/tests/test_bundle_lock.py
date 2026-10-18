"""The bundle lock holds every package its packages need, and ``bundle_lock.py check`` says when not.

``scripts/bundle_lock.py`` writes ``.github/bundle-lock/<platform>/uv.lock``. A lock in which a
package names a dependency the lock holds no entry for exports a requirements list a hashed install
refuses, so ``check`` (the static ``bundle-lock`` row) reads each lock's own records, offline, and
names each broken edge before ``uv lock --check`` is asked anything.

The planted control is the committed lock with one dependency's entry removed. A lock whose stale
metadata left the edge out altogether is self-consistent and passes that guard; the
``sherpa-onnx`` row below is what holds the one such case seen so far.

The staleness rule has its own planted control: ``core.lock`` or one bundle's ``app.json`` moved
without a fresh lock, through the row's own exit code too. So does a lock whose root entry lost one
extra's edges, or the extra one asks of a package, which ``uv lock --check`` accepts; a fresh lock
for a requirement whose marker excludes the lock's platform, and for a folder whose name uv
normalizes, passes; and a lock written with ``UV_INDEX``, ``UV_EXTRA_INDEX_URL``,
``UV_CONFIG_FILE`` or ``UV_PREVIEW`` set in the caller's environment is the one a clean check
expects.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import planted_envs as planted

ROOT = Path(__file__).resolve().parents[2]
LOCKS = ROOT / ".github" / "bundle-lock"


def _module(root: Path = ROOT):
    return planted.load(root / "scripts" / "bundle_lock.py", f"_bundle_lock_{abs(hash(root))}")


def _without_package(text: str, name: str) -> str:
    """*text* with the ``[[package]]`` entry named *name* removed, and nothing else."""
    blocks = re.split(r"(?m)^(?=\[\[package\]\]\n)", text)
    kept = [b for b in blocks if not b.startswith(f'[[package]]\nname = "{name}"\n')]
    assert len(kept) == len(blocks) - 1, f"the lock holds no single entry for {name}"
    return "".join(kept)


def _depended_on(text: str) -> str:
    """A package the committed lock holds that another package in it depends on."""
    module = _module()
    graph = module.lock_graph(text)
    needed = {dep for deps in graph["dependencies"].values() for dep in deps}
    return sorted(needed & graph["packages"])[0]


def test_both_committed_locks_hold_every_dependency_their_packages_name():
    module = _module()
    for platform in module.PLATFORMS:
        text = (LOCKS / platform / "uv.lock").read_text(encoding="utf-8")
        assert module.missing_dependencies(text) == [], platform


def test_the_guard_reads_a_graph_the_size_the_lock_holds():
    # The vacuity floor: a reader that found no packages or no edges would pass every lock.
    module = _module()
    for platform in module.PLATFORMS:
        graph = module.lock_graph((LOCKS / platform / "uv.lock").read_text(encoding="utf-8"))
        assert len(graph["packages"]) >= 150, platform
        assert sum(len(d) for d in graph["dependencies"].values()) >= 200, platform


def test_diarization_onnx_s_sherpa_onnx_brings_its_core_on_both_platforms():
    # The case that broke the first lock: sherpa-onnx requires sherpa-onnx-core, and a lock
    # resolved from stale cached metadata recorded sherpa-onnx with no dependencies at all.
    module = _module()
    for platform in module.PLATFORMS:
        graph = module.lock_graph((LOCKS / platform / "uv.lock").read_text(encoding="utf-8"))
        assert "sherpa-onnx-core" in graph["dependencies"]["sherpa-onnx"], platform


def test_a_lock_with_one_dependency_removed_is_refused_naming_both_packages():
    module = _module()
    text = (LOCKS / "linux-x86_64" / "uv.lock").read_text(encoding="utf-8")
    removed = _depended_on(text)
    problems = module.missing_dependencies(_without_package(text, removed))
    assert problems and all(removed in p for p in problems), problems
    assert any(p.split(" ", 1)[0] != removed for p in problems), problems


def test_write_resolves_from_the_index_not_from_uv_s_cache(tmp_path, monkeypatch):
    # write() relocks every platform with --refresh; check() never refreshes (it reads the lock).
    module = _module()
    calls = []
    monkeypatch.setattr(module, "uv_lock", lambda directory, *extra: calls.append(extra) or 0)
    monkeypatch.setattr(module, "pyproject", lambda root, **kw: "[project]\n")
    assert module.write(tmp_path) == 0
    assert calls == [("--refresh",)] * len(module.PLATFORMS)


def _inputs(tmp_path: Path) -> Path:
    """A copy of the tree's lock inputs: the two scripts, core.lock, every app.json, the lock."""
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    for script in ("bundle_lock.py", "core_lock.py"):
        shutil.copy2(ROOT / "scripts" / script, root / "scripts" / script)
    shutil.copy2(ROOT / "core.lock", root / "core.lock")
    for manifest in ROOT.glob("*/app.json"):
        (root / manifest.parent.name).mkdir()
        shutil.copy2(manifest, root / manifest.parent.name / "app.json")
    shutil.copytree(LOCKS, root / ".github" / "bundle-lock")
    return root


def test_check_fails_on_a_committed_lock_with_one_dependency_removed(tmp_path):
    # check() on a copy of the tree's inputs, one dependency's entry gone. The generated
    # pyproject.toml matches, so only the guard can say no.
    root = _inputs(tmp_path)
    lock = root / ".github" / "bundle-lock" / "linux-x86_64" / "uv.lock"
    removed = _depended_on(lock.read_text(encoding="utf-8"))
    lock.write_text(_without_package(lock.read_text(encoding="utf-8"), removed), encoding="utf-8")

    problems = _module(root).check(root)
    assert any(p.startswith("linux-x86_64:") and removed in p for p in problems), problems
    assert not any(p.startswith("darwin-arm64:") for p in problems), problems


@pytest.mark.parametrize("moved", ["core.lock", "app.json"])
def test_check_fails_when_core_lock_or_a_bundle_moves_without_a_fresh_lock(tmp_path, moved):
    # The staleness rule: it stops before uv is asked anything, so this needs no network.
    root = _inputs(tmp_path)
    module = _module(root)
    if moved == "core.lock":
        (root / "core.lock").write_text("1" * 40 + "\n", encoding="utf-8")
    else:
        name = next(iter(module.declared(root)))
        manifest = root / name / "app.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["dependencies"]["pythonDependencies"].append("planted-extra-dependency")
        manifest.write_text(json.dumps(data), encoding="utf-8")
    assert module.check(root) == [
        f"{p}: pyproject.toml differs from what the bundles' app.json files and core.lock generate"
        for p in module.PLATFORMS
    ]


def test_check_fails_on_a_lock_whose_root_lost_one_extra_s_edges(tmp_path):
    # What a hand-resolved merge conflict leaves: requires-dist still lists the extra, so uv lock
    # --check and the export accept it, and the bundle would run in the base environment.
    root = _inputs(tmp_path)
    lock = root / ".github" / "bundle-lock" / "linux-x86_64" / "uv.lock"
    text = lock.read_text(encoding="utf-8")
    edges = re.search(r'(?m)^sentence-transformers = \[\n(?:    \{[^\n]*\},\n)+\]\n', text)
    assert edges, "the committed lock holds no sentence-transformers extra"
    lock.write_text(text.replace(edges.group(0), "", 1), encoding="utf-8")
    problems = _module(root).check(root)
    assert any(p.startswith("linux-x86_64:") and "sentence-transformers extra" in p
               for p in problems), problems
    assert not any(p.startswith("darwin-arm64:") for p in problems), problems


def test_both_committed_locks_depend_on_every_extra_their_project_asks_for():
    module = _module()
    for platform in module.PLATFORMS:
        directory = LOCKS / platform
        assert module.edge_problems((directory / "uv.lock").read_text(encoding="utf-8"),
                                    (directory / "pyproject.toml").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("variable", ["UV_INDEX", "UV_EXTRA_INDEX_URL", "UV_CONFIG_FILE",
                                      "UV_PREVIEW", "UV_PREVIEW_FEATURES"])
def test_a_lock_written_with_uv_settings_in_the_environment_is_the_one_a_clean_check_expects(
        tmp_path, monkeypatch, variable):
    # uv reads these even under --no-config: an index ahead of --default-index, a uv.toml that
    # can name one, or preview, which leaves the root's metadata out. A lock written with one set
    # would pass a check with it set and fail CI's.
    project_index = planted.Index(tmp_path / "a")
    project_index.add("dep-alpha")
    other = planted.Index(tmp_path / "b")
    other.add("dep-alpha")
    root = planted.tree(tmp_path)
    planted.bundle(root, "alpha-app", deps=["dep-alpha>=1"])
    config = tmp_path / "uv.toml"
    config.write_text(f'[[index]]\nurl = "{other.url}"\n', encoding="utf-8")
    monkeypatch.setenv(variable, {"UV_CONFIG_FILE": str(config), "UV_PREVIEW": "1",
                                  "UV_PREVIEW_FEATURES": "lock-without-metadata"}
                       .get(variable, other.url))
    lock_dir = planted.lock(root, project_index)
    text = (lock_dir / "uv.lock").read_text(encoding="utf-8")
    assert str(project_index.root) in text and str(other.root) not in text
    assert "[package.metadata]" in text
    monkeypatch.delenv(variable)
    module = planted.load(root / "scripts" / "bundle_lock.py", "_planted_bundle_lock")
    assert module.uv_lock(lock_dir, "--check") == 0


def test_the_bundle_lock_row_exits_one_on_a_stale_lock(tmp_path):
    # The row's verdict, not only check()'s list: a core.lock moved without a fresh lock.
    root = _inputs(tmp_path)
    (root / "core.lock").write_text("1" * 40 + "\n", encoding="utf-8")
    run = subprocess.run([sys.executable, str(root / "scripts" / "bundle_lock.py"), "check"],
                         cwd=root, capture_output=True, text=True)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "pyproject.toml differs" in run.stdout


def _host_lock(tmp_path, deps, **extras):
    index = planted.Index(tmp_path / "index")
    for name in ("dep-alpha", "dep-beta", "dep-delta"):
        index.add(name)
    index.add("dep-extra", extras=extras or None)
    root = planted.tree(tmp_path)
    planted.bundle(root, "alpha-app", deps=deps)
    lock_dir = planted.lock(root, index)
    module = _module(root)
    return (module, (lock_dir / "uv.lock").read_text(encoding="utf-8"),
            (lock_dir / "pyproject.toml").read_text(encoding="utf-8"), lock_dir)


def test_a_requirement_whose_marker_excludes_the_lock_s_platform_needs_no_edge(tmp_path):
    module, lock, project, lock_dir = _host_lock(
        tmp_path, ["dep-alpha>=1; sys_platform == 'win32'",
                   f"dep-beta>=1; sys_platform == '{sys.platform}'"])
    assert module.uv_lock(lock_dir, "--check") == 0
    assert module.edge_problems(lock, project) == []
    # The one whose marker holds here still needs its edge.
    damaged = re.sub(r'(?m)^    \{ name = "dep-beta"[^\n]*\},\n', "", lock, count=1)
    assert damaged != lock
    assert any("alpha-app extra" in p and "dep-beta" in p
               for p in module.edge_problems(damaged, project))


def test_an_extra_asked_of_a_package_must_stay_on_its_edge(tmp_path):
    module, lock, project, _ = _host_lock(tmp_path, ["dep-extra[fast]>=1"],
                                          fast=["dep-delta"])
    assert module.edge_problems(lock, project) == []
    edge = re.search(r'\{ name = "dep-extra", extra = \["fast"\] \}', lock)
    assert edge, lock
    damaged = lock.replace(edge.group(0), '{ name = "dep-extra" }', 1)
    assert any("dep-extra[fast]" in p for p in module.edge_problems(damaged, project))


def test_a_bundle_folder_uv_normalizes_is_checked_under_its_normalized_extra(tmp_path):
    index = planted.Index(tmp_path / "index")
    index.add("dep-alpha")
    root = planted.tree(tmp_path)
    planted.bundle(root, "Alpha_App", deps=["dep-alpha>=1"])
    lock_dir = planted.lock(root, index)
    module = _module(root)
    lock = (lock_dir / "uv.lock").read_text(encoding="utf-8")
    project = (lock_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "\nalpha-app = [\n" in lock
    assert module.edge_problems(lock, project) == []
    damaged = lock.replace('alpha-app = [\n    { name = "dep-alpha" },\n]\n', "", 1)
    assert damaged != lock
    assert any("Alpha_App extra" in p for p in module.edge_problems(damaged, project))


# -- versions: the lock holds a version each requirement allows ---------------------------------

def test_both_committed_locks_hold_a_version_every_requirement_allows():
    module = _module()
    for name in module.PLATFORMS:
        lock = (LOCKS / name / "uv.lock").read_text(encoding="utf-8")
        project = (LOCKS / name / "pyproject.toml").read_text(encoding="utf-8")
        assert module.version_problems(lock, project) == [], name


def test_check_fails_on_a_lock_that_holds_a_package_below_a_bundle_s_floor(tmp_path):
    # A hand merge that kept one side's package entry: openai at a version alibaba-models'
    # "openai>=1.0" (and every other bundle that declares it) excludes. Offline: it stops before uv.
    root = _inputs(tmp_path)
    lock = root / ".github" / "bundle-lock" / "linux-x86_64" / "uv.lock"
    text = lock.read_text(encoding="utf-8")
    entry = re.search(r'(?m)^name = "openai"\nversion = "[^"]+"\n', text)
    assert entry, "the committed lock holds no openai entry"
    lock.write_text(text.replace(entry.group(0), 'name = "openai"\nversion = "0.9.0"\n', 1),
                    encoding="utf-8")
    problems = _module(root).check(root)
    assert any(p.startswith("linux-x86_64:") and "openai at 0.9.0" in p and "openai>=1.0" in p
               and "alibaba-models" in p for p in problems), problems
    assert not any(p.startswith("darwin-arm64:") for p in problems), problems


def test_a_requirement_whose_marker_excludes_the_lock_s_platform_asks_no_version(tmp_path):
    module = _module()
    project = module.pyproject(ROOT, marker=module.PLATFORMS["linux-x86_64"], base=[],
                               core=None)
    project = project.replace("[project.optional-dependencies]\n",
                              "[project.optional-dependencies]\nplanted-app = "
                              "[\"dep-mac>=9; sys_platform == 'darwin'\"]\n", 1)
    lock = '[[package]]\nname = "dep-mac"\nversion = "1.0.0"\n'
    assert module.version_problems(lock, project) == []


# -- staleness: every other input the generator reads ------------------------------------------

ALL_PLATFORMS = ["linux-x86_64", "darwin-arm64"]


@pytest.mark.parametrize(("old", "new", "stale"), [
    ('PYTHON = "3.12.15"', 'PYTHON = "3.12.16"', ALL_PLATFORMS),
    ('INDEX = "https://pypi.org/simple"', 'INDEX = "https://index.example.com/simple"',
     ALL_PLATFORMS),
    ("\"linux-x86_64\": \"sys_platform == 'linux' and platform_machine == 'x86_64'\"",
     "\"linux-x86_64\": \"sys_platform == 'linux' and platform_machine == 'aarch64'\"",
     ["linux-x86_64"]),
    ("CONFLICTS: list[tuple[str, str]] = []",
     'CONFLICTS: list[tuple[str, str]] = [("alibaba-models", "deepseek-models")]',
     ALL_PLATFORMS),
], ids=["the pinned Python", "the index", "a platform marker", "CONFLICTS"])
def test_check_fails_when_a_generator_input_moves_without_a_fresh_lock(
        tmp_path, monkeypatch, old, new, stale):
    root = _inputs(tmp_path)
    script = root / "scripts" / "bundle_lock.py"
    text = script.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    script.write_text(text.replace(old, new), encoding="utf-8")
    module = _module(root)
    # A platform the change leaves current goes on to uv's own check; that needs the network,
    # and is not this rule.
    monkeypatch.setattr(module, "uv_lock", lambda *args: 0)
    assert module.check(root) == [
        f"{p}: pyproject.toml differs from what the bundles' app.json files and core.lock generate"
        for p in stale
    ]
