"""Put the bundle directory on ``sys.path`` so ``from connector import ...`` resolves.

Explicit rather than relying on the runner's rootdir heuristics — the same thing pytest's
default prepend-import mode does, spelled out so the import holds however the suite is invoked.
Imports nothing from ``personalclaw``, so it stays inside the apps SDK boundary.
"""

import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))
