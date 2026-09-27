"""Every chat-model app proves, on the wire, that it sends what core asks of a call.

Core hands a model factory the sampling ``temperature`` best-of-N asked for and the ``max_tokens``
budget it derived, as build kwargs. A factory that drops one fails nothing: the call still
answers, at the provider's default, and best-of-N samples one answer N times. Five first-party
factories dropped both until #124, core's branded-app factory dropped the budget until core
#3621, and the three Anthropic-protocol apps crashed on any temperature while their manifests let
in the 1.x ``anthropic`` SDK, which takes none. Each app that declares ``chat`` therefore carries
a test that drives its real SDK client against ``apps_testkit.model_wire``'s recording endpoint,
and this rail refuses a chat-model app without one.

The model a call names is the other half. An instance saved with its Default Model left empty
names no model, and core refuses an unbound call on it (core #3718). Four apps still chose a model
for it when their own factories were handed none: bedrock-models took one from live discovery at
``start()``, anthropic-models and claude-subscription their curated default, meta-muse-spark
``muse-spark-1.1``. And bedrock-models' registry factory dropped the Default Model altogether.
So each chat-model app also runs ``blank_model_report`` (refused with nothing sent when no model is
chosen, the Default Model named when one is set, through both of its build paths), and its Default
Model help says that in one sentence shared by every app, the one core's bundled Ollama app uses.

Every chat-model app has a Default Model field. alibaba-models had none, so an instance of it could
serve chat only through a binding in Settings → Models, and the rail read an app without the field
as one with nothing to check.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: What an empty Default Model does, in every chat-model app's settings form and README. The same
#: words as core's bundled Ollama app (src/personalclaw/apps/native/ollama-models/app.json).
DEFAULT_MODEL_HELP = (
    "The model this instance answers with when nothing in Settings → Models names one. "
    "Leave it empty to choose its models in Settings → Models."
)

#: What the help used to promise an empty Default Model would do. None of it happens: nothing
#: picks a model a user did not choose.
RETIRED_PROMISES = (
    "discover",
    "newest",
    "first available",
    "empty =",
    "empty uses",
    "start()",
    "built-in",
)

_README_ROW = re.compile(r"^\| `default_model` \| (?P<label>[^|]+?) \| (?P<help>.+?) \|$", re.M)


def _chat_model_apps(apps_root: Path) -> list[Path]:
    found = []
    for manifest in sorted(apps_root.glob("*/app.json")):
        provider = json.loads(manifest.read_text(encoding="utf-8")).get("provider") or {}
        if provider.get("type") == "model" and "chat" in (provider.get("capabilities") or []):
            found.append(manifest.parent)
    return found


def _test_texts(bundle: Path) -> list[str]:
    return [
        test.read_text(encoding="utf-8")
        for test in (*bundle.glob("test_*.py"), *bundle.glob("tests/test_*.py"))
    ]


def _proves_the_wire(bundle: Path) -> bool:
    return any(
        "apps_testkit.model_wire" in text and "sampling_sent(" in text
        for text in _test_texts(bundle)
    )


def _proves_no_model_is_picked(bundle: Path) -> bool:
    return any(
        "apps_testkit.model_wire" in text
        and "blank_model_report(" in text
        and "blank_model_expected(" in text
        for text in _test_texts(bundle)
    )


def _settings_fields(bundle: Path) -> dict:
    manifest = json.loads((bundle / "app.json").read_text(encoding="utf-8"))
    return ((manifest.get("provider") or {}).get("settingsSchema") or {}).get("properties") or {}


def _default_model_help_faults(bundle: Path) -> list[str]:
    """Why this app's Default Model is missing or its help does not say what an empty one does.
    ``[]`` when the field is there and says it."""
    fields = _settings_fields(bundle)
    if "default_model" not in fields:
        return ["app.json has no Default Model field (`default_model`)"]
    help_text = str(((fields["default_model"].get("x-meta") or {}).get("help")) or "")
    faults = []
    lead = help_text[: -len(DEFAULT_MODEL_HELP)].rstrip()
    if not help_text.endswith(DEFAULT_MODEL_HELP) or (lead and not lead.endswith((".", ")"))):
        faults.append(f"app.json help does not end with the one sentence: {help_text!r}")
    promised = [p for p in RETIRED_PROMISES if p in help_text.lower()]
    if promised:
        faults.append(f"app.json help still promises {promised}: {help_text!r}")
    readme = bundle / "README.md"
    rows = _README_ROW.findall(readme.read_text(encoding="utf-8")) if readme.is_file() else []
    if len(rows) != 1:
        faults.append(f"README.md has {len(rows)} `default_model` settings rows, not 1")
    elif rows[0][1].replace("`", "") != help_text:
        faults.append(f"README.md says {rows[0][1]!r}, the form says {help_text!r}")
    return faults


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


def test_every_chat_model_app_proves_it_picks_no_model():
    apps = _chat_model_apps(ROOT)
    assert len(apps) >= 15, f"only {len(apps)} chat-model apps found — did the manifest keys move?"
    missing = [app.name for app in apps if not _proves_no_model_is_picked(app)]
    assert not missing, (
        "these chat-model apps have no test proving a call no model is chosen for is refused and "
        "the Default Model is the one named (apps_testkit.model_wire.blank_model_report): "
        + ", ".join(missing)
    )


def test_every_chat_model_app_has_a_default_model_that_says_what_an_empty_one_does():
    apps = _chat_model_apps(ROOT)
    assert len(apps) >= 15, f"only {len(apps)} chat-model apps found — did the manifest keys move?"
    faults = {app.name: _default_model_help_faults(app) for app in apps}
    assert not {name: f for name, f in faults.items() if f}


def test_the_rail_sees_an_app_without_one(tmp_path):
    """Positive control: a chat-model bundle whose tests never drive the recording endpoint."""
    bundle = tmp_path / "probe-models"
    bundle.mkdir()
    manifest = {"name": "probe-models", "provider": {"type": "model", "capabilities": ["chat"]}}
    (bundle / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    (bundle / "test_provider.py").write_text("def test_builds():\n    pass\n", encoding="utf-8")
    assert _chat_model_apps(tmp_path) == [bundle]
    assert not _proves_the_wire(bundle)
    assert not _proves_no_model_is_picked(bundle)


def test_the_rail_sees_a_help_that_promises_a_pick(tmp_path):
    """Positive control: a form with no Default Model, the Default Model help every app carried
    before this rail, and a README that disagrees with its form."""
    bundle = tmp_path / "probe-models"
    bundle.mkdir()
    old = "A Groq model id. Empty = resolved from live /v1/models discovery."
    field = {"type": "string", "x-meta": {"label": "Default Model", "help": old}}
    manifest: dict = {
        "name": "probe-models",
        "provider": {
            "type": "model",
            "capabilities": ["chat"],
            "settingsSchema": {"properties": {"api_key": {"type": "string"}}},
        },
    }
    (bundle / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert _default_model_help_faults(bundle) == [
        "app.json has no Default Model field (`default_model`)"
    ]

    manifest["provider"]["settingsSchema"]["properties"]["default_model"] = field
    (bundle / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    (bundle / "README.md").write_text(f"| `default_model` | Default Model | {old} |\n")
    faults = _default_model_help_faults(bundle)
    assert len(faults) == 2 and "does not end with" in faults[0] and "discover" in faults[1]

    field["x-meta"]["help"] = f"A Groq model id. {DEFAULT_MODEL_HELP}"
    (bundle / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    faults = _default_model_help_faults(bundle)
    assert len(faults) == 1 and faults[0].startswith("README.md says"), faults

    row = f"| `default_model` | Default Model | {field['x-meta']['help']} |\n"
    (bundle / "README.md").write_text(row)
    assert _default_model_help_faults(bundle) == []
