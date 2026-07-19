"""Put the app dir on sys.path so app tests import ``provider`` (the ollama app
module) the way the gateway's app loader does at runtime."""

import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[1]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))
