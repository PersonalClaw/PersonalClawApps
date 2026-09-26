"""Every chat-model app proves, on the wire, that it sends what core asks of a call.

Core hands a model factory the sampling ``temperature`` best-of-N asked for and the ``max_tokens``
budget it derived, as build kwargs. A factory that drops one fails nothing: the call still
answers, at the provider's default, and best-of-N samples one answer N times. Five first-party
factories dropped both until #124, core's branded-app factory dropped the budget until core
#3621, and the three Anthropic-protocol apps crashed on any temperature while their manifests let
in the 1.x ``anthropic`` SDK, which takes none. Each app that declares ``chat`` therefore carries
a test that drives its real SDK client against ``apps_testkit.model_wire``'s recording endpoint,
and this rail refuses a chat-model app without one.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _chat_model_apps(apps_root: Path) -> list[Path]:
    found = []
    for manifest in sorted(apps_root.glob("*/app.json")):
        provider = json.loads(manifest.read_text(encoding="utf-8")).get("provider") or {}
        if provider.get("type") == "model" and "chat" in (provider.get("capabilities") or []):
            found.append(manifest.parent)
    return found


def _proves_the_wire(bundle: Path) -> bool:
    for test in (*bundle.glob("test_*.py"), *bundle.glob("tests/test_*.py")):
        text = test.read_text(encoding="utf-8")
        if "apps_testkit.model_wire" in text and "sampling_sent(" in text:
            return True
    return False


def test_every_chat_model_app_proves_what_its_requests_carry():
    apps = _chat_model_apps(ROOT)
    # Vacuity floor: 15 apps declare chat as this rail lands; a glob or key that matched nothing
    # would pass the assertion below.
    assert len(apps) >= 15, f"only {len(apps)} chat-model apps found — did the manifest keys move?"
    missing = [app.name for app in apps if not _proves_the_wire(app)]
    assert not missing, (
        "these chat-model apps have no test proving the request carries the per-call temperature "
        "and max_tokens (see apps_testkit/model_wire.py): " + ", ".join(missing)
    )


def test_the_rail_sees_an_app_without_one(tmp_path):
    """Positive control: a chat-model bundle whose tests never drive the recording endpoint."""
    bundle = tmp_path / "probe-models"
    bundle.mkdir()
    manifest = {"name": "probe-models", "provider": {"type": "model", "capabilities": ["chat"]}}
    (bundle / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    (bundle / "test_provider.py").write_text("def test_builds():\n    pass\n", encoding="utf-8")
    assert _chat_model_apps(tmp_path) == [bundle]
    assert not _proves_the_wire(bundle)
