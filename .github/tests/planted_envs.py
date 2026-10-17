"""Planted pieces for the bundle runner's and the gate's tests: a local index, a stub core, a lock.

Nothing here reaches the network. Every package a planted bundle declares is a wheel built here
and served from a local index whose pages carry each file's sha256, so a lock records hashes the
way PyPI's pages make it record them. The stub core is a wheel too. pytest reaches a planted
environment through ``PYTHONPATH`` (this interpreter's own site-packages), never through an
install, so a planted environment holds only what its lock and the stub core put there.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import site
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def wheel(directory: Path, name: str, version: str, files: dict[str, str],
          extras: dict[str, list[str]] | None = None) -> Path:
    """A pure-Python wheel holding *files*, built without a build backend. *extras* maps each
    extra it provides to the requirements that extra adds."""
    dist = name.replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    content = {path: text.encode() for path, text in files.items()}
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    for extra, requirements in (extras or {}).items():
        meta += f"Provides-Extra: {extra}\n"
        meta += "".join(f'Requires-Dist: {r}; extra == "{extra}"\n' for r in requirements)
    content[f"{info}/METADATA"] = meta.encode()
    content[f"{info}/WHEEL"] = (b"Wheel-Version: 1.0\nGenerator: planted\nRoot-Is-Purelib: true\n"
                                b"Tag: py3-none-any\n")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{dist}-{version}-py3-none-any.whl"
    record = []
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in content.items():
            archive.writestr(member, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{member},sha256={digest},{len(data)}")
        archive.writestr(f"{info}/RECORD", "\n".join([*record, f"{info}/RECORD,,"]) + "\n")
    return path


class Index:
    """A local simple index: one page per project, each link carrying its file's sha256."""

    def __init__(self, directory: Path):
        self.root = directory / "simple"
        self.root.mkdir(parents=True)

    @property
    def url(self) -> str:
        return self.root.as_uri() + "/"

    def add(self, name: str, version: str = "1.0.0", code: str = "VALUE = 1\n", *,
            hashed: bool = True, extras: dict[str, list[str]] | None = None) -> Path:
        """Serve *name*. With ``hashed=False`` its page carries no sha256, as a plain file server
        does, so a lock records no hash for it."""
        page = self.root / name
        path = wheel(page, name, version, {f"{name.replace('-', '_')}/__init__.py": code},
                     extras=extras)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        href = f"{path.name}#sha256={digest}" if hashed else path.name
        (page / "index.html").write_text(f'<a href="{href}">{path.name}</a>\n', encoding="utf-8")
        projects = sorted(p.name for p in self.root.iterdir() if p.is_dir())
        (self.root / "index.html").write_text(
            "".join(f'<a href="{name}/">{name}</a>\n' for name in projects), encoding="utf-8")
        return path


def stub_core(directory: Path) -> str:
    """The install spec of a stub core: a wheel named personalclaw that holds nothing."""
    return str(wheel(directory, "personalclaw", "0.0.0", {"personalclaw/__init__.py": ""}))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # A dataclass looks its module up in sys.modules while the module is still loading.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bundle(root: Path, name: str, *, deps: list[str] | None = None,
           tests: dict[str, str] | None = None) -> Path:
    """A planted app bundle: an app.json declaring *deps*, and its test files."""
    app = root / name
    app.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": "0.0.0"}
    if deps:
        manifest["dependencies"] = {"pythonDependencies": deps}
    (app / "app.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for filename, code in (tests or {"test_ok.py": "def test_ok():\n    assert True\n"}).items():
        (app / filename).write_text(code, encoding="utf-8")
    return app


def tree(tmp_path: Path) -> Path:
    """A planted repository root holding the runner, its two readers and an empty pytest.ini."""
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    for script in ("test_bundles.py", "core_lock.py", "bundle_lock.py"):
        shutil.copy2(ROOT / "scripts" / script, root / "scripts" / script)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return root


def lock(root: Path, index: Index, directory: Path | None = None, *,
         python: str | None = None) -> Path:
    """Generate and lock *root*'s bundle lock for this machine, against *index*: no core and no
    pytest in its base, this platform only, for *python* (default: this interpreter's)."""
    directory = directory or root / ".planted-lock"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pyproject.toml").write_text(project(root, index, python=python),
                                              encoding="utf-8")
    module = load(root / "scripts" / "bundle_lock.py", "_planted_bundle_lock")
    assert module.uv_lock(directory) == 0, f"uv lock failed in {directory}"
    return directory


def project(root: Path, index: Index, *, python: str | None = None) -> str:
    module = load(root / "scripts" / "bundle_lock.py", "_planted_bundle_lock")
    return module.pyproject(root, marker=module.host_marker(),
                            python=python or platform.python_version(),
                            base=[], index=index.url, core=None)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(root: Path, message: str = "planted") -> str:
    git(root, "add", "-A")
    git(root, "-c", "user.name=Runner Test", "-c", "user.email=runner@example.com",
        "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


def environ(lock_dir: Path | None = None, core: str | None = None, envs: Path | None = None,
            **extra: str) -> dict[str, str]:
    """The environment a planted run gets. The runner reads its planted-run options from it: the
    planted lock and core, where environments go, this interpreter to build them from, and this
    interpreter's site-packages on each bundle's path, so pytest is importable in an
    environment that does not hold it."""
    env = os.environ.copy()
    for name in ("VIRTUAL_ENV", "PYTHONPATH"):
        env.pop(name, None)
    env["BUNDLE_RUNNER_ENV_PYTHON"] = sys.executable
    env["BUNDLE_RUNNER_TEST_PYTHONPATH"] = os.pathsep.join(site.getsitepackages())
    if lock_dir is not None:
        env["BUNDLE_RUNNER_LOCK_DIR"] = str(lock_dir)
    if core is not None:
        env["BUNDLE_RUNNER_CORE_SPEC"] = core
    if envs is not None:
        env["BUNDLE_RUNNER_ENVS"] = str(envs)
    env.update(extra)
    return env


def runner(root: Path, lock_dir: Path, core: str, *args: str, envs: Path | None = None,
           env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """The planted tree's runner, as the gate starts it, against the planted lock and core."""
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "test_bundles.py"), *args], cwd=root,
        env={**environ(lock_dir, core, envs or root / ".bundle-venvs"), **(env or {})},
        capture_output=True, text=True,
    )
