"""Every app declares the core features it relies on, by names PersonalClaw offers.

An approval prompt's answers come from PersonalClaw (``approval_brief_for(event)["answers"]``), so
an app whose prompt offers them works only on a PersonalClaw that hands them over. Telegram
Channel's update that offered them installed on a PersonalClaw built before them, every approval on
the channel then arrived as a notice with nothing to press, and nothing had checked the update
fitted that PersonalClaw. An app names the core features it relies on in ``requiresCoreFeatures``,
and PersonalClaw refuses to review, install, update or switch on one it cannot host.

Three rails over every bundle, against the installed core:

1. every name an app declares is a core feature the installed PersonalClaw offers: a misspelt name,
   or one from a PersonalClaw that does not exist yet, would make the app installable nowhere;
2. every app whose shipped code reads the approval brief's answers declares ``approval-answers``;
3. every app whose shipped code downloads with ``personalclaw.sdk.net.open_url`` declares
   ``guarded-download``: on a PersonalClaw without it the app would not load at all.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # for apps_testkit

from apps_testkit import sdk_contract  # noqa: E402

BUNDLES = sorted(manifest.parent for manifest in ROOT.glob("*/app.json"))

#: The apps whose prompts offer the brief's answers as this rail lands: a reader the scan stopped
#: recognising would leave rail 2 checking nothing.
KNOWN_READERS = {"telegram-channel", "slack-channel", "discord-channel", "email-channel"}

#: The apps that download with ``open_url``, for rail 3 the same way.
KNOWN_DOWNLOADERS = {"diarization-onnx"}


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    """The SDK resolves core's home when it loads. Not the real one."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "home"))


def _declared(bundle: Path) -> list:
    manifest = json.loads((bundle / "app.json").read_text(encoding="utf-8"))
    declared = manifest.get("requiresCoreFeatures", [])
    return declared if isinstance(declared, list) else [declared]


def _reads_the_answers(bundle: Path) -> bool:
    """Whether the bundle's shipped code takes the approval brief from the SDK and reads its
    ``answers``: ``brief.get("answers")`` or ``brief["answers"]``."""
    imports = reads = False
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "personalclaw.sdk.channel":
                imports = imports or any(a.name == "approval_brief_for" for a in node.names)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                reads = reads or (
                    node.func.attr == "get"
                    and bool(node.args)
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "answers"
                )
            elif isinstance(node, ast.Subscript):
                key = node.slice
                reads = reads or (isinstance(key, ast.Constant) and key.value == "answers")
    return imports and reads


def test_every_declared_feature_is_one_the_installed_core_offers():
    from personalclaw.sdk.features import CORE_FEATURES

    unknown = {
        bundle.name: [f for f in _declared(bundle) if f not in CORE_FEATURES]
        for bundle in BUNDLES
    }
    unknown = {name: names for name, names in unknown.items() if names}
    assert unknown == {}, (
        f"these apps declare core features the installed PersonalClaw does not offer: {unknown} "
        f"(it offers {sorted(CORE_FEATURES)})"
    )


def test_every_app_that_offers_the_briefs_answers_declares_them():
    from personalclaw.sdk.features import APPROVAL_ANSWERS

    readers = {bundle.name for bundle in BUNDLES if _reads_the_answers(bundle)}
    assert KNOWN_READERS <= readers, (
        f"the scan no longer sees these apps read the brief's answers: "
        f"{sorted(KNOWN_READERS - readers)}"
    )
    undeclared = sorted(
        name for name in readers
        if APPROVAL_ANSWERS not in _declared(ROOT / name)
    )
    assert undeclared == [], (
        f"these apps offer the approval brief's answers without declaring "
        f"'requiresCoreFeatures': ['{APPROVAL_ANSWERS}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_a_reader_from_an_app_that_reads_nothing(tmp_path):
    """Positive and negative control, in the shape the rail reads."""
    reader = tmp_path / "reader-app"
    reader.mkdir()
    (reader / "app.json").write_text('{"name": "reader-app", "version": "0.1.0"}')
    (reader / "delivery.py").write_text(
        "from personalclaw.sdk.channel import approval_brief_for\n\n\n"
        "def answers(event):\n"
        "    return (approval_brief_for(event) or {}).get('answers') or []\n",
        encoding="utf-8",
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "provider.py").write_text("def answers(d):\n    return d.get('answers')\n")
    assert _reads_the_answers(reader) is True
    assert _reads_the_answers(other) is False


def _downloads_through_the_guard(bundle: Path) -> bool:
    """Whether the bundle's shipped code takes ``open_url`` from the SDK."""
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "personalclaw.sdk.net":
                if any(alias.name == "open_url" for alias in node.names):
                    return True
    return False


def test_every_app_that_downloads_through_the_guard_declares_it():
    from personalclaw.sdk.features import GUARDED_DOWNLOAD

    downloaders = {bundle.name for bundle in BUNDLES if _downloads_through_the_guard(bundle)}
    assert KNOWN_DOWNLOADERS <= downloaders, (
        f"the scan no longer sees these apps download with open_url: "
        f"{sorted(KNOWN_DOWNLOADERS - downloaders)}"
    )
    undeclared = sorted(
        name for name in downloaders if GUARDED_DOWNLOAD not in _declared(ROOT / name)
    )
    assert undeclared == [], (
        f"these apps download with personalclaw.sdk.net.open_url without declaring "
        f"'requiresCoreFeatures': ['{GUARDED_DOWNLOAD}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_a_downloader_from_an_app_that_only_fetches(tmp_path):
    """Positive and negative control for rail 3."""
    downloader = tmp_path / "downloader-app"
    downloader.mkdir()
    (downloader / "app.json").write_text('{"name": "downloader-app", "version": "0.1.0"}')
    (downloader / "provider.py").write_text(
        "from personalclaw.sdk.net import EgressBlocked, open_url\n", encoding="utf-8"
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "provider.py").write_text("from personalclaw.sdk.net import fetch\n")
    assert _downloads_through_the_guard(downloader) is True
    assert _downloads_through_the_guard(other) is False
