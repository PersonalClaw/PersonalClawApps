"""The bundle lock is current: both platforms' generated project and uv.lock match the tree.

``scripts/bundle_lock.py`` generates ``.github/bundle-lock/<platform>/`` from every bundle's
``dependencies.pythonDependencies`` and ``core.lock``. A change to either without a fresh lock
fails here, so the bundle runner never meets a bundle the lock does not hold.

    python .github/scripts/check_bundle_lock.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    spec = importlib.util.spec_from_file_location("_check_bundle_lock",
                                                  ROOT / "scripts" / "bundle_lock.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.main(["check"])


if __name__ == "__main__":
    sys.exit(main())
