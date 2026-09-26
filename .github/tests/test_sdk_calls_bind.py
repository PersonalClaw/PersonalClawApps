"""Every app's imports from ``personalclaw.sdk.*`` are published, and every call it makes binds.

``apps_testkit.sdk_contract`` carries two static rails, and only slack-channel's suite ran them.
So ``vector-store-qdrant/provider.py`` called ``CredentialStore()`` without the ``home`` it
requires, and nothing noticed: the ``TypeError`` was swallowed on every connect, and the app never
read the key the credential store held. The rail names that call exactly, but no test pointed it
at that bundle. This one points both rails at every bundle in the repository.

The third rail, conformance, runs at test time inside a bundle's own suite and stays there: it
needs the bundle's tests to drive the calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # for apps_testkit

from apps_testkit import sdk_contract  # noqa: E402

BUNDLES = sorted(manifest.parent for manifest in ROOT.glob("*/app.json"))


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    """The rails import SDK modules, and core resolves its home when they load. Not the real one."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "home"))


def test_the_census_reaches_every_bundle():
    """Vacuity floor: 68 bundles make 1617 SDK calls as this rail lands. A glob or a census that
    stopped matching would pass both rails below with nothing checked."""
    assert len(BUNDLES) >= 40, f"only {len(BUNDLES)} bundles found — did app.json move?"
    calls = sum(len(sdk_contract.census(bundle).calls) for bundle in BUNDLES)
    assert calls >= 1000, f"the census resolved only {calls} SDK calls"


def test_every_sdk_name_an_app_imports_is_published():
    problems = [p for bundle in BUNDLES for p in sdk_contract.unpublished_imports(bundle)]
    assert problems == [], "\n".join(problems)


def test_every_sdk_call_an_app_makes_binds_to_the_installed_signature():
    problems = [p for bundle in BUNDLES for p in sdk_contract.unbindable_calls(bundle)]
    assert problems == [], "\n".join(problems)


def test_the_bind_rail_names_a_constructor_called_without_its_required_argument(tmp_path):
    """Positive control, in the shape of the defect this rail was pointed at the repo for."""
    bundle = tmp_path / "probe-app"
    bundle.mkdir()
    (bundle / "provider.py").write_text(
        "from personalclaw.sdk.credentials import CredentialStore\n\n\n"
        "def key():\n"
        "    return CredentialStore().resolve('PROBE_KEY')\n",
        encoding="utf-8",
    )

    problems = sdk_contract.unbindable_calls(bundle)

    assert len(problems) == 1, problems
    assert "probe-app/provider.py:5" in problems[0]
    assert "missing a required argument: 'home'" in problems[0]
