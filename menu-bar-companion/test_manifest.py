"""The manifest, against core's OWN parser — the real contract, not a hand-rolled schema.

``AppManifest.from_dict`` is the entry point (there is no ``parse_manifest``), and the
round-trip assertion is the same one the apps-repo ``manifest-validate`` job runs.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

MANIFEST_PATH = Path(__file__).resolve().parent / "app.json"
RAW = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

manifest_mod = pytest.importorskip(
    "personalclaw.apps.manifest",
    reason="core is installed in CI's manifest-validate job; skip where it is absent",
)
AppManifest = manifest_mod.AppManifest


def _parsed():
    return AppManifest.from_dict(deepcopy(RAW))


def test_identity_fields():
    m = _parsed()
    assert m.name == "menu-bar-companion"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", m.name), "name must be kebab-case"
    assert re.fullmatch(r"\d+\.\d+\.\d+", m.version)
    assert m.displayName and m.description


def test_round_trip_is_stable():
    """The parity contract: parse → to_dict → parse must not drift."""
    m = _parsed()
    assert AppManifest.from_dict(m.to_dict()).to_dict() == m.to_dict()


def test_it_declares_itself_a_darwin_client_app():
    platform = _parsed().platform
    assert platform.installMode == "client"
    assert platform.os == ["darwin"]
    assert platform.supports_platform("darwin") is True
    assert platform.supports_platform("linux") is False
    # And the declaration survives serialisation — the Store reads it from to_dict.
    assert _parsed().to_dict()["platform"]["installMode"] == "client"
    assert _parsed().to_dict()["platform"]["os"] == ["darwin"]


def test_the_client_install_one_liner_is_present_and_inspectable():
    """``installMode: client`` makes the Store show copy-paste instead of installing.

    The shell is never auto-run by the platform, so it has to be readable by the person
    pasting it: it must name what it fetches and what it installs.
    """
    ci = _parsed().platform.clientInstall
    assert ci.shell, "a client app with no clientInstall shell tells the user nothing"
    assert "PersonalClawApps" in ci.shell, "say what is being cloned"
    assert "rumps" in ci.shell, "say which GUI dependency is being installed"
    assert "--check" in ci.postInstall, "the first run should verify the token works"


def test_permissions_are_exactly_the_minimum_this_app_uses():
    """The Store renders this block as the install-consent surface, so an unused grant
    is a real cost. Pinned exactly: adding one fails here."""
    perms = _parsed().permissions
    assert perms.api == ["/api/loops", "/api/approvals", "/api/ws"]
    assert perms.events == ["approval", "approval_resolved"]
    # Everything else: not claimed.
    assert perms.storage is False, "client app: the platform never grants it a DATA_DIR"
    assert perms.network is False, "it talks to your own gateway, not out to the internet"
    assert perms.cron is False
    assert perms.agent is False
    assert perms.memory == ""
    assert perms.mcpTools == []
    assert perms.appMessaging == []
    assert perms.storageShared is False
    assert perms.storageRead == []


def test_vacuity_floor_the_permission_rail_notices_a_widened_grant():
    """Prove the pin above discriminates: a widened manifest parses differently."""
    widened = deepcopy(RAW)
    widened["permissions"]["storage"] = True
    widened["permissions"]["api"].append("/api/memory")
    parsed = AppManifest.from_dict(widened)
    assert parsed.permissions.storage is True
    assert "/api/memory" in parsed.permissions.api
    assert parsed.permissions.to_dict() != _parsed().permissions.to_dict()


def test_the_declared_quality_bar_is_backed_by_this_bundles_own_evidence():
    """Run CI's OWN verifier against this bundle, with only the pytest spawn stubbed.

    ``verify_app``'s default runner spawns ``pytest <bundle>``; calling it from inside that
    very suite would recurse forever. So the ``tested`` axis's RUN belongs to CI (the
    ``quality-declarations`` job executes it for real, and a red bundle fails the build),
    and what this pins is the evidence SHAPE — that every axis declared here has something
    to check at all. Declaring an axis with nothing behind it is a violation, not a free
    pass, which is the whole reason a badge is worth reading.
    """
    quality = pytest.importorskip("personalclaw.apps.quality")
    violations = quality.verify_app(
        MANIFEST_PATH.parent, _parsed(), run_tests=lambda _dir: (True, "")
    )
    assert [v.render() for v in violations] == []

    declared = _parsed().quality
    # An absent block would satisfy the assertion above vacuously — it claims nothing.
    assert declared is not None, "this test would pass on a manifest that declared nothing"
    assert "tested" in declared.claims()
    assert quality.bundle_test_files(MANIFEST_PATH.parent), "tested: true with no test files"
    # a11y is deliberately NOT claimed: there is no UI here to scan and no axe report to
    # show, and an unevidenced claim is exactly what the verifier fails the build over.
    assert "a11y" not in declared.claims()


def test_vacuity_floor_the_quality_rail_rejects_a_claim_this_bundle_cannot_back():
    """Prove the rail above discriminates: claim a11y with no axe report."""
    quality = pytest.importorskip("personalclaw.apps.quality")
    lying = deepcopy(RAW)
    lying["quality"] = {"tested": True, "a11y": True}
    violations = quality.verify_app(
        MANIFEST_PATH.parent, AppManifest.from_dict(lying), run_tests=lambda _dir: (True, "")
    )
    assert "a11y" in {v.axis for v in violations}, [v.render() for v in violations]


def test_it_contributes_no_provider_backend_or_dashboard_ui():
    """A client app is not a capability provider and is not hosted by the gateway.

    Declaring any of these would be a claim the platform would try to honour on the
    server — exactly what ``installMode: client`` says will not happen.
    """
    for key in ("provider", "backend", "ui", "crons", "mcpServers", "dependencies"):
        assert key not in RAW, f"a client app must not declare {key!r}"


def test_the_manifest_never_sets_the_reserved_native_flag():
    assert "native" not in RAW
