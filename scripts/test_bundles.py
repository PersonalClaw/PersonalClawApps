"""Run app-bundle tests with the same dependency posture locally and in CI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]


def _has_tests(bundle: Path) -> bool:
    return any(bundle.glob("test_*.py")) or any(bundle.glob("tests/test_*.py"))


def discover_bundles() -> list[Path]:
    """Return every top-level app bundle that ships tests."""
    return [
        bundle
        for bundle in sorted(ROOT.iterdir())
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


def _label(bundle: Path) -> str:
    try:
        return str(bundle.relative_to(ROOT))
    except ValueError:
        return str(bundle)


def _install_dependencies(
    dependencies: Sequence[str], *, python: str, env: Mapping[str, str]
) -> int:
    if not dependencies:
        return 0
    print(f"installing declared dependencies: {', '.join(dependencies)}", flush=True)
    try:
        return subprocess.run(
            ["uv", "pip", "install", "--python", python, *dependencies],
            cwd=ROOT,
            env=env,
            check=False,
        ).returncode
    except FileNotFoundError:
        print("error: uv is required to install bundle dependencies", file=sys.stderr)
        return 127


def run_bundle(
    bundle: Path,
    *,
    python: str = sys.executable,
    install_dependencies: bool = True,
    env: Mapping[str, str] | None = None,
) -> int:
    """Install one bundle's declared dependencies, then run its tests."""
    bundle = bundle.resolve()
    print(f"\n=== {_label(bundle)} ===", flush=True)
    if not (bundle / "app.json").is_file():
        print(f"error: {bundle} is not an app bundle (missing app.json)", file=sys.stderr)
        return 2
    if not _has_tests(bundle):
        print(f"error: {bundle} has no test_*.py files", file=sys.stderr)
        return 2

    try:
        dependencies = declared_dependencies(bundle)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    run_env = os.environ.copy()
    if env is not None:
        run_env.update(env)
    run_env.setdefault("PERSONALCLAW_SKIP_APP_BACKENDS", "1")
    run_env.setdefault("PYTEST_XDIST_AUTO_NUM_WORKERS", "3")

    if install_dependencies:
        install_rc = _install_dependencies(dependencies, python=python, env=run_env)
        if install_rc:
            return install_rc

    try:
        return subprocess.run(
            [python, "-m", "pytest", str(bundle), "-q"],
            cwd=ROOT,
            env=run_env,
            check=False,
        ).returncode
    except FileNotFoundError:
        print(f"error: Python interpreter not found: {python}", file=sys.stderr)
        return 127


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install each app manifest's declared Python dependencies and run its tests. "
            "With no bundle arguments, run every tested bundle."
        )
    )
    parser.add_argument("bundles", nargs="*", type=Path, help="bundle paths (default: all)")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter whose environment receives dependencies and runs pytest",
    )
    args = parser.parse_args(argv)

    bundles = [path if path.is_absolute() else ROOT / path for path in args.bundles]
    if not bundles:
        bundles = discover_bundles()
    if not bundles:
        print("error: no tested app bundles found", file=sys.stderr)
        return 2

    failed: list[str] = []
    for bundle in bundles:
        if run_bundle(bundle, python=args.python):
            failed.append(_label(bundle.resolve()))

    if failed:
        print(f"\nFAILED bundles: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\nOK: {len(bundles)} bundle(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
