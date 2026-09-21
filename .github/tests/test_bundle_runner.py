from __future__ import annotations

import os
import site
import subprocess
import sys
from pathlib import Path

from scripts import test_bundles

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_BUNDLE = ROOT / ".github/tests/fixtures/bundle-with-dependency"


def test_declared_dependency_is_required_and_installed(tmp_path, capfd):
    test_env = tmp_path / "test-env"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(test_env)],
        check=True,
        cwd=ROOT,
    )
    python = str(test_env / "bin/python")

    # Reuse pytest from the parent test environment without making the fixture's
    # declared package visible in the fresh environment.
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [*site.getsitepackages(), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    corrupted_rc = test_bundles.run_bundle(
        FIXTURE_BUNDLE,
        python=python,
        install_dependencies=False,
        env=env,
    )
    corrupted_output = "\n".join(capfd.readouterr())
    assert corrupted_rc != 0
    assert "No module named 'apps_test_dependency'" in corrupted_output

    assert test_bundles.run_bundle(FIXTURE_BUNDLE, python=python, env=env) == 0
