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

A media call names its model too (#148): an image, video, speech, embedding or diarization call is
refused when its binding names none, with the SDK's sentence and before anything is sent. #148 made
eight apps refuse, and nothing held the rest to it, so four still did not: both diarization apps ran
their one model, piper-tts returned nothing and said nothing, and local-image-gen answered in a
sentence of its own. So every app that serves a media call runs ``media_refusal_report`` over every
media adapter an instance of it registers, the ones its ``create_provider`` returns and the ones
each media scanner it registers builds, and this rail refuses an app without it.
"""

from __future__ import annotations

import ast
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


#: The calls a media contract has core make of an adapter, each naming a model. An app whose own
#: code defines one serves a media call itself (an app whose media is served by core's built-in
#: adapters, like openai-models' embedding, defines none).
_MEDIA_CALL = re.compile(
    r"^\s*async def (generate|edit|transcribe|transcribe_detailed|synthesize|embed|embed_batch|"
    r"diarize)\(",
    re.M,
)


def _app_code(bundle: Path) -> list[Path]:
    """The bundle's own Python, its tests left out."""
    return [
        path
        for path in sorted(bundle.rglob("*.py"))
        if not path.name.startswith("test_") and "tests" not in path.relative_to(bundle).parts
    ]


def _media_model_apps(apps_root: Path) -> list[Path]:
    found = []
    for manifest in sorted(apps_root.glob("*/app.json")):
        bundle = manifest.parent
        if any(_MEDIA_CALL.search(path.read_text(encoding="utf-8")) for path in _app_code(bundle)):
            found.append(bundle)
    return found


def _registered_scanners(bundle: Path) -> list[str]:
    """The functions the bundle hands the SDK's ``register_scanner`` (however it is aliased): each
    builds a media adapter per instance, so each is one the refusal report must cover."""
    names = []
    for path in _app_code(bundle):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and len(node.args) == 2):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if called.endswith("register_scanner") or called == "_reg_scanner":
                if isinstance(node.args[1], ast.Name):
                    names.append(node.args[1].id)
    return names


def _media_refusal_faults(bundle: Path) -> list[str]:
    """Why this media app does not prove that a call naming no model is refused. ``[]`` when a test
    runs the report over every adapter an instance of it registers."""
    proving = [
        text
        for text in _test_texts(bundle)
        if "apps_testkit.model_wire" in text
        and "media_refusal_report(" in text
        and "media_refusal_expected(" in text
        and "media_adapters(" in text
    ]
    if not proving:
        return ["no test runs apps_testkit.model_wire.media_refusal_report over its media adapters"]
    missed = [name for name in _registered_scanners(bundle) if not any(name in t for t in proving)]
    return [f"the refusal test leaves out the adapters {name} builds" for name in missed]


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


def test_every_media_model_app_proves_a_call_naming_no_model_is_refused():
    apps = _media_model_apps(ROOT)
    # Vacuity floor: 12 apps serve a media call as this rail lands; a pattern that matched nothing
    # would pass the assertion below.
    assert len(apps) >= 12, f"only {len(apps)} media-model apps found — did the calls move?"
    faults = {app.name: _media_refusal_faults(app) for app in apps}
    assert not {name: f for name, f in faults.items() if f}, (
        "these media-model apps do not prove that a call naming no model is refused before "
        "anything is sent (see apps_testkit.model_wire.media_refusal_report)"
    )


def test_the_media_rail_sees_an_app_without_one(tmp_path):
    """Positive control: a media bundle with no refusal test, then one whose test leaves out an
    adapter a scanner it registers builds, then one that covers it."""
    bundle = tmp_path / "probe-media"
    bundle.mkdir()
    (bundle / "app.json").write_text(json.dumps({"name": "probe-media"}), encoding="utf-8")
    (bundle / "provider.py").write_text(
        "from personalclaw.sdk.model import register_scanner as _reg_scanner\n"
        "class Speaker:\n"
        "    async def synthesize(self, text, voice=''):\n"
        "        return None\n"
        "def _scan_tts(entries):\n"
        "    return [Speaker() for _ in entries]\n"
        "_reg_scanner('tts', _scan_tts)\n",
        encoding="utf-8",
    )
    (bundle / "test_provider.py").write_text("def test_builds():\n    pass\n", encoding="utf-8")
    assert _media_model_apps(tmp_path) == [bundle]
    assert _registered_scanners(bundle) == ["_scan_tts"]
    assert _media_refusal_faults(bundle) == [
        "no test runs apps_testkit.model_wire.media_refusal_report over its media adapters"
    ]

    report = (
        "from apps_testkit.model_wire import media_adapters, media_refusal_expected, "
        "media_refusal_report\n"
        "def test_refused():\n"
        "    adapters = media_adapters(DIR, create_provider{scanners})\n"
        "    assert media_refusal_report(adapters) == media_refusal_expected(adapters)\n"
    )
    (bundle / "test_media.py").write_text(report.format(scanners=""), encoding="utf-8")
    assert _media_refusal_faults(bundle) == ["the refusal test leaves out the adapters _scan_tts builds"]

    (bundle / "test_media.py").write_text(
        report.format(scanners=", scanners=(_scan_tts,)"), encoding="utf-8"
    )
    assert _media_refusal_faults(bundle) == []


def test_the_media_report_tells_a_refusal_from_a_silence():
    """Control on the report itself: a call that returns nothing without saying why, one that
    reaches for the network before refusing, and a text-to-speech that claims a call it refuses
    each read differently from a refusal."""
    import asyncio
    import logging
    import socket

    from apps_testkit.model_wire import (
        media_refusal_expected,
        media_refusal_report,
        no_model_refusal,
    )
    from personalclaw.sdk.image import ImageGenError, ImageGenProvider

    sentence = no_model_refusal()

    class _Image(ImageGenProvider):
        name = "probe-image"
        display_name = "Probe"

        def __init__(self, reach_first: bool) -> None:
            self.reach_first = reach_first

        async def is_available(self) -> bool:
            return True

        async def list_models(self) -> list:
            return []

        async def generate(self, prompt, *, model="", size="", n=1, **opts):
            if self.reach_first:
                try:
                    socket.create_connection(("127.0.0.1", 9), timeout=0.1)
                except OSError:
                    pass
            raise ImageGenError(sentence)

        async def edit(self, prompt, *, source_image, mask="", model="", size="", n=1, **opts):
            raise ImageGenError(sentence)

    class _Speaker:
        def __init__(self, says: bool, claims: bool) -> None:
            self.says, self.claims = says, claims

        async def synthesize(self, text, voice="", output_path="", *, speed=1.0, **opts):
            if self.says:
                logging.getLogger("probe").warning("probe refused: %s", sentence)
            return None

        async def can_synthesize(self, voice=""):
            return self.claims

    good = [_Image(reach_first=False), _Speaker(says=True, claims=False)]
    assert asyncio.run(media_refusal_report(good)) == media_refusal_expected(good)

    bad = [_Image(reach_first=True), _Speaker(says=False, claims=True)]
    assert asyncio.run(media_refusal_report(bad)) == {
        "image generate": f"refused: {sentence} (after reaching the network)",
        "image edit": f"refused: {sentence}",
        "tts synthesize": "returned nothing, said nothing",
        "tts can_synthesize": "True",
    }


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
