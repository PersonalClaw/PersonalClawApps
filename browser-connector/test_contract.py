"""Contract tests for the browser connector bundle.

They pin the CLOSED typed vocabulary and its addressing (every verb names the run whose own tab
it acts on), the JS↔Python parity that keeps the extension honest, the loopback rail (a public
endpoint or gateway is refused, so a cdp_url is only ever written over loopback), and the
manifest: loopback-only host permissions (the "no new listener / loopback only" surface) and only
the browser permissions the worker uses. No browser and no network — every leg is pure structure.
What the worker DOES is driven in a browser double by ``test_extension.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from connector import (
    CONTRACT_METHODS,
    RUN_TAB_REPORTS,
    ContractError,
    announce_payload,
    announce_url,
    build_request,
    is_loopback_host,
    is_run_id,
    parse_request,
    run_tab_url,
    run_tabs_url,
)

HERE = Path(__file__).resolve().parent
EXT = HERE / "extension"

LOOPBACK_CDP = "ws://127.0.0.1:9222/devtools/page/ABC123"
PUBLIC_CDP = "ws://203.0.113.7:9222/devtools/page/ABC123"
RUN = "run-a1"


# ── the closed vocabulary ────────────────────────────────────────────────────────


def test_the_vocabulary_is_exactly_the_five_typed_verbs() -> None:
    assert CONTRACT_METHODS == ("navigate", "read-outline", "click", "type", "close")


def test_each_verb_builds_and_round_trips_through_the_wire_form() -> None:
    valid = {
        "navigate": {"url": "https://example.test/x"},
        "read-outline": {},
        "click": {"ref": "e7"},
        "type": {"ref": "e7", "value": "hello"},
        "close": {},
    }
    for method in CONTRACT_METHODS:
        req = build_request(method, run=RUN, **valid[method])
        msg = req.to_message()
        assert msg["method"] == method
        assert msg["run"] == RUN
        assert parse_request(msg).method == method
        assert parse_request(msg).params == valid[method]
        assert parse_request(msg).run == RUN


def test_an_unknown_verb_is_refused() -> None:
    for bogus in ("scroll", "screenshot", "eval", "NAVIGATE", ""):
        with pytest.raises(ContractError):
            build_request(bogus, run=RUN, url="https://example.test")


def test_a_missing_required_param_is_refused() -> None:
    with pytest.raises(ContractError):
        build_request("click", run=RUN)  # no ref
    with pytest.raises(ContractError):
        build_request("type", run=RUN, ref="e1")  # no value
    with pytest.raises(ContractError):
        build_request("navigate", run=RUN, url="   ")  # blank is not a url


def test_a_verb_that_names_no_run_is_refused() -> None:
    """The extension acts only in a tab a run opened for itself, so a verb with no run, or with
    something that is not a run id, has no tab it may touch."""
    for run in ("", None, "../tabs", "run a1", "x" * 65):
        with pytest.raises(ContractError):
            build_request("read-outline", run=run)
    with pytest.raises(ContractError):
        parse_request({"method": "read-outline", "params": {}})


# ── the loopback rail ────────────────────────────────────────────────────────────


def test_announce_payload_only_carries_a_loopback_ws_endpoint() -> None:
    assert announce_payload(LOOPBACK_CDP) == {"cdp_url": LOOPBACK_CDP}
    assert announce_payload("ws://localhost:9222/devtools/page/X")["cdp_url"].endswith("/X")


def test_announce_payload_refuses_a_public_or_non_ws_endpoint() -> None:
    for bad in (PUBLIC_CDP, "http://127.0.0.1:9222/json", "ws://10.0.0.5:9222/x", ""):
        with pytest.raises(ContractError):
            announce_payload(bad)


def test_announce_url_targets_a_loopback_gateway_only() -> None:
    assert announce_url("http://127.0.0.1:10000") == "http://127.0.0.1:10000/api/browse/connector"
    assert announce_url("http://localhost:10000/").endswith("/api/browse/connector")
    for bad in ("https://example.com", "http://192.168.1.9:10000", "ftp://127.0.0.1", ""):
        with pytest.raises(ContractError):
            announce_url(bad)


def test_the_run_tab_routes_stay_on_a_loopback_gateway() -> None:
    base = "http://127.0.0.1:10000"
    assert run_tabs_url(base) == f"{base}/api/browse/connector/tabs"
    assert run_tab_url(base, RUN) == f"{base}/api/browse/connector/tabs/{RUN}"
    for bad_base in ("https://example.com", "http://192.168.1.9:10000"):
        with pytest.raises(ContractError):
            run_tabs_url(bad_base)
        with pytest.raises(ContractError):
            run_tab_url(bad_base, RUN)
    for bad_run in ("", "../connector", "a/b", "run?x=1"):
        assert is_run_id(bad_run) is False
        with pytest.raises(ContractError):
            run_tab_url(base, bad_run)


def test_loopback_host_classifier_matches_the_rail() -> None:
    for good in ("127.0.0.1", "127.5.6.7", "::1", "localhost", "app.localhost"):
        assert is_loopback_host(good) is True
    for bad in ("203.0.113.7", "10.0.0.1", "192.168.1.1", "example.com", ""):
        assert is_loopback_host(bad) is False


# ── the extension mirrors the contract, and reaches loopback only ──────────────────


def _js_array(name: str) -> list[str]:
    text = (EXT / "contract.js").read_text(encoding="utf-8")
    match = re.search(rf"{name}\s*=\s*\[(.*?)\]", text, re.S)
    assert match, f"contract.js must declare {name} as an array literal"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_js_contract_declares_the_same_vocabulary_in_the_same_order() -> None:
    assert tuple(_js_array("CONTRACT_METHODS")) == CONTRACT_METHODS, (
        "extension/contract.js drifted from connector.py — the extension would speak a "
        "different vocabulary than the tests pin"
    )


def test_the_js_contract_reports_the_same_run_tab_states() -> None:
    assert tuple(_js_array("RUN_TAB_REPORTS")) == RUN_TAB_REPORTS


def test_the_manifest_asks_for_only_the_browser_permissions_the_worker_uses() -> None:
    """``tabs`` to open the run's tab and act on it by id, ``tabGroups`` to name its group after the
    task and see the group close, ``storage`` for the settings and for which tab is which run's.
    Nothing reads the focused tab any more, so ``activeTab`` is not asked for, and nothing injects
    a script, so neither is ``scripting``."""
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(manifest["permissions"]) == ["storage", "tabGroups", "tabs"]


def test_the_manifest_is_mv3_with_loopback_only_host_permissions() -> None:
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    hosts = manifest.get("host_permissions", [])
    assert hosts, "the connector must declare its (loopback) host permissions"
    for pattern in hosts:
        # A match pattern is <scheme>://<host>/<path>; pull the host and hold it to the rail.
        host = pattern.split("://", 1)[-1].split("/", 1)[0]
        assert is_loopback_host(host), f"non-loopback host permission: {pattern!r}"
    # A broad reach would defeat "loopback only" no matter what the hosts above say.
    assert "<all_urls>" not in hosts and "*://*/*" not in hosts


def test_the_content_script_never_types_into_a_password_field() -> None:
    content = (EXT / "content.js").read_text(encoding="utf-8")
    assert "password" in content and "never types into a password" in content


# ── the manifest is a valid app (defense-in-depth with the manifest-validate CI job) ──


def test_app_json_validates_and_round_trips() -> None:
    manifest_mod = pytest.importorskip("personalclaw.apps.manifest")
    app_manifest = manifest_mod.AppManifest
    data = json.loads((HERE / "app.json").read_text(encoding="utf-8"))
    parsed = app_manifest.from_dict(data)
    assert app_manifest.from_dict(parsed.to_dict()).to_dict() == parsed.to_dict()
    assert parsed.validate() == [], "app.json must be a valid manifest"
    assert parsed.name == "browser-connector"


# ── the client install goes into the PersonalClaw home ──────────────────────────────────────


def _client_install() -> dict:
    return json.loads((HERE / "app.json").read_text(encoding="utf-8"))["platform"]["clientInstall"]


def _sh(script: str, env: dict[str, str]) -> str:
    import subprocess

    return subprocess.run(
        ["/bin/sh", "-c", script], env=env, capture_output=True, text=True, check=True
    ).stdout


@pytest.mark.parametrize(
    "pclaw_home, expected",
    [
        ("/srv/pclaw", "/srv/pclaw/apps/browser-connector"),
        (None, "/home/me/.personalclaw/apps/browser-connector"),
    ],
)
def test_the_client_install_goes_into_the_personalclaw_home(pclaw_home, expected) -> None:
    """The one-liner and the step after it name the same folder: ``$PERSONALCLAW_HOME`` when
    PersonalClaw runs on a home of its own, else ``~/.personalclaw``. It used to be
    ``$HOME/.personalclaw`` whatever the home was. Only the ``DEST`` assignment is run (the
    clone would reach the network); the postInstall step is an echo and runs whole."""
    env = {"HOME": "/home/me", "PATH": "/usr/bin:/bin"}
    if pclaw_home:
        env["PERSONALCLAW_HOME"] = pclaw_home
    ci = _client_install()
    assignment = re.search(r'DEST="[^"]*"', ci["shell"])
    assert assignment, ci["shell"]
    assert _sh(f'{assignment.group(0)}; printf %s "$DEST"', env) == expected
    assert f"{expected}/browser-connector/extension" in _sh(ci["postInstall"], env)
