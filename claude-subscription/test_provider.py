"""Unit tests for the claude-subscription model provider app.

What these prove, in the order that matters for a credential-handling app:

1. the declared :class:`SubscriptionSource` is valid and the spec names it;
2. **no separate API key exists at any layer** — no ``api_key`` setting, empty
   ``api_key_env``, and a stray ``ANTHROPIC_API_KEY`` in the environment is never read;
3. a signed-in CLI store is what authenticates the wire client, on BOTH build paths
   (the manifest's ``create_provider`` and the registry's type factory);
4. not-signed-in / expired / malformed all fail **soft** with the app's own typed reason,
   through the real ``providers/loader.py`` availability entry point — no exception;
5. the resolve is **read-only** (bytes, mode and mtime unchanged) and the token is copied
   nowhere on disk;
6. the manifest itself validates.

Isolation: ``HOME`` is redirected to ``tmp_path`` for every test, so ``~``-expansion in the
declared paths can only ever reach the fixture store — the operator's real
``~/.claude/.credentials.json`` is never opened, and an autouse fixture asserts its stat is
byte-for-byte unchanged around every test. The ``anthropic`` SDK is stubbed (constructing a
provider triggers its lazy import) so the recorded client kwargs can be inspected without a
network call.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import types
from pathlib import Path

import pytest

# ── Real-home tripwire ────────────────────────────────────────────────────
# Captured BEFORE any monkeypatching, from the environment's real HOME. Only stat() is
# called — the operator's token is never read by this suite.
_REAL_HOME = Path(os.environ.get("HOME", "/nonexistent"))
_REAL_STORE = _REAL_HOME / ".claude" / ".credentials.json"


def _fingerprint(path: Path) -> tuple[int, int, int] | None:
    """(mtime_ns, size, mode) for *path*, or None when it does not exist."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, stat.S_IMODE(st.st_mode))


_REAL_STORE_BEFORE = _fingerprint(_REAL_STORE)

# Deliberately does NOT imitate the vendor's key prefix: a realistic-looking literal trips
# secret scanners (and this repo's own pre-commit hook) for no test value — nothing here
# depends on the token's shape, only on it being carried through untouched.
TOKEN = "TESTONLY-fake-subscription-token"

import provider as prov  # noqa: E402  — app-local; registers source + type + catalog


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Redirect HOME + the PersonalClaw home into tmp, and stub the anthropic SDK."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "pc-home"))
    monkeypatch.setenv("PERSONALCLAW_WORKSPACE", str(tmp_path / "pc-home" / "ws"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    calls: list[dict] = []
    fake = types.ModuleType("anthropic")

    class _AsyncAnthropic:
        def __init__(self, **kw):
            calls.append(kw)
            self.kw = kw

    fake.AsyncAnthropic = _AsyncAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    fake.calls = calls  # type: ignore[attr-defined]
    yield fake
    # The operator's real store must be untouched by anything this test did.
    assert _fingerprint(_REAL_STORE) == _REAL_STORE_BEFORE


def _store(
    tmp_path: Path,
    *,
    token: str = TOKEN,
    expires_in_ms: int = 3_600_000,
    raw: str | None = None,
    dirname: str = ".claude",
) -> Path:
    """Write a Claude-Code-shaped credential store under the redirected HOME."""
    directory = tmp_path / "home" / dirname
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".credentials.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": token,
                        "expiresAt": int(time.time() * 1000) + expires_in_ms,
                        "subscriptionType": "max",
                    }
                }
            ),
            encoding="utf-8",
        )
    path.chmod(0o600)
    return path


def _client_key(fake) -> str:
    """The api_key the (stubbed) anthropic client was constructed with."""
    assert fake.calls, "no anthropic client was constructed"
    return str(fake.calls[-1].get("api_key", ""))


def _availability() -> tuple[bool, str]:
    """The availability tuple the extensions list would show, via the REAL entry point.

    ``load_availability`` derives the probe from the spec's declared ``credential_source``
    when the app module exports no ``availability()`` hook — which this app deliberately
    does not.
    """
    from personalclaw.providers.loader import load_availability

    ext = types.SimpleNamespace(
        name="claude-subscription",
        provider_config=types.SimpleNamespace(
            implementation="provider:create_provider",
            providerType=prov.SPEC.type,
        ),
    )
    probe = load_availability(ext)
    assert probe is not None, "no availability probe derived from the declared source"
    return probe()


# ── 1. Declarations ───────────────────────────────────────────────────────


def test_source_declaration_is_valid_and_registered():
    from personalclaw.llm.subscription_credentials import _SOURCES

    assert prov.SOURCE.validate() == []
    assert prov.SPEC.credential_source == prov.SOURCE.id == "claude-code"
    assert _SOURCES.get("claude-code") is prov.SOURCE
    # The app owns its login sentence; core never names a vendor's login verb.
    assert "claude login" in prov.SOURCE.login_hint


def test_spec_registered_for_core_to_read():
    from personalclaw.llm.registry import get_default_registry
    from personalclaw.sdk.provider_helpers import spec_credential_source

    reg = get_default_registry()
    assert reg.capability_of("claude_subscription").type == "claude_subscription"
    assert reg.catalog_of("claude_subscription") is not None
    # This is what providers/loader.py reads to derive the availability probe.
    assert spec_credential_source("claude_subscription") == "claude-code"


# ── 2. No separate API key, anywhere ─────────────────────────────────────


def test_no_api_key_surface_exists():
    manifest = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))
    schema = manifest["provider"]["settingsSchema"]["properties"]
    assert "api_key" not in schema and "apiKey" not in schema
    assert prov.SPEC.api_key_env == ""  # nothing to fall back to


def test_env_api_key_is_never_read(monkeypatch, tmp_path, _isolated):
    """A stray ANTHROPIC_API_KEY must not authenticate this provider — signed in or not."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-must-not-be-used")

    # Not signed in: the placeholder, NOT the env key.
    prov.create_provider({})
    assert _client_key(_isolated) == "unused"

    # Signed in: the CLI's token, still not the env key.
    _store(tmp_path)
    prov.create_provider({})
    assert _client_key(_isolated) == TOKEN


# ── 3. The signed-in CLI store is what authenticates ─────────────────────


def test_config_path_uses_the_cli_token(tmp_path, _isolated):
    _store(tmp_path)
    built = prov.create_provider({})
    assert _client_key(_isolated) == TOKEN
    assert built._model == prov.SPEC.default_model  # derived default, not a baked id
    assert built._max_tokens == 4096


def test_relocated_store_via_claude_config_dir(monkeypatch, tmp_path, _isolated):
    """The first declared candidate is ``$CLAUDE_CONFIG_DIR/.credentials.json``."""
    relocated = _store(tmp_path, dirname="elsewhere")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated.parent))
    prov.create_provider({})
    assert _client_key(_isolated) == TOKEN


def test_registry_path_uses_the_cli_token(tmp_path, _isolated):
    """The runtime path (provider_bridge → registry.build → the type factory)."""
    from personalclaw.llm.capabilities import Capability
    from personalclaw.llm.registry import ProviderEntry, get_default_registry

    _store(tmp_path)
    reg = get_default_registry()
    if not any(e.name == "claude-sub-inst" for e in reg.list_entries()):
        reg.register_entry(
            ProviderEntry(
                name="claude-sub-inst",
                type="claude_subscription",
                model="claude-sonnet-5",
                declared_capabilities=frozenset({Capability.CHAT}),
            )
        )
    built = reg.build("claude-sub-inst")
    assert _client_key(_isolated) == TOKEN
    assert built._model == "claude-sonnet-5"


def test_instance_api_key_option_still_outranks_the_subscription(tmp_path, _isolated):
    """The pinned five-hop order: an explicit key the user set wins over the source.

    The app ships no ``api_key`` setting, so this can only come from a hand-edited
    config — but the ORDER is the contract, and a subscription must never silently
    outrank a credential the user chose.
    """
    from personalclaw.llm.capabilities import Capability
    from personalclaw.llm.registry import ProviderEntry

    _store(tmp_path)
    prov._factory(
        entry=ProviderEntry(
            name="explicit",
            type="claude_subscription",
            model="claude-sonnet-5",
            options={"api_key": "explicit-key"},
            declared_capabilities=frozenset({Capability.CHAT}),
        )
    )
    assert _client_key(_isolated) == "explicit-key"


# ── 4. Fail soft + typed, never a crash ──────────────────────────────────


def test_not_signed_in_greys_out_with_the_apps_reason():
    available, reason = _availability()
    assert available is False
    assert "claude-code" in reason and "claude login" in reason
    assert TOKEN not in reason


def test_signed_in_is_available_with_no_reason(tmp_path):
    _store(tmp_path)
    assert _availability() == (True, "")


def test_expired_sign_in_is_not_signed_in(monkeypatch, tmp_path, _isolated):
    expired = _store(tmp_path, expires_in_ms=-1000)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(expired.parent))
    available, reason = _availability()
    assert available is False
    assert "expired" in reason and "claude login" in reason
    assert TOKEN not in reason
    # And the build falls THROUGH to the placeholder rather than using a dead token.
    prov.create_provider({})
    assert _client_key(_isolated) == "unused"


def test_malformed_store_is_not_signed_in_and_leaks_nothing(monkeypatch, tmp_path):
    half_written = _store(tmp_path, raw='{"claudeAiOauth": {"accessToken": "' + TOKEN)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(half_written.parent))
    available, reason = _availability()
    assert available is False
    assert "malformed or half-written" in reason
    assert TOKEN not in reason  # no fragment of the file rides out in the reason
    assert "claude login" in reason


def test_blank_token_is_not_signed_in(monkeypatch, tmp_path):
    blank = _store(tmp_path, token="   ")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(blank.parent))
    available, reason = _availability()
    assert available is False
    assert "holds no sign-in token" in reason
    assert "claude login" in reason


def test_a_leading_unset_candidate_masks_the_specific_reason(tmp_path, _isolated):
    """Pinned CORE behaviour, not this app's preference — and the reason it is accepted.

    ``resolve_subscription_credential`` keeps the FIRST informative failure across the
    declared candidate paths (llm/subscription_credentials.py:225 ``first_failure``). This
    app declares ``$CLAUDE_CONFIG_DIR/.credentials.json`` FIRST so an explicit operator
    override outranks the default location — the same precedence discipline the credential
    order itself follows. The cost is that on the common install (variable unset, so that
    candidate cannot open) the specific sentence from a later candidate — "expired",
    "malformed", "holds no sign-in token" — is replaced by the generic one below.

    The remedy is still identical (``claude login``), so the user is not misled, and
    precedence correctness is worth more than message specificity. Pinned here so a future
    change to either side is a visible diff rather than a silent regression.
    """
    _store(tmp_path, expires_in_ms=-1000)  # CLAUDE_CONFIG_DIR deliberately unset
    available, reason = _availability()
    assert available is False
    assert reason == (
        "claude-code is not signed in on this machine — sign in with `claude login` first"
    )


def test_missing_store_never_raises(tmp_path, _isolated):
    """The whole not-signed-in path is soft: probe and build both return normally."""
    assert _availability()[0] is False
    assert prov.create_provider({}) is not None


# ── 5. Read-only, and the token is copied nowhere ────────────────────────


def test_resolving_never_writes_the_cli_store(tmp_path, _isolated):
    path = _store(tmp_path)
    before = (path.read_bytes(), _fingerprint(path))
    time.sleep(0.01)  # any write would move mtime_ns
    prov.create_provider({})
    _availability()
    prov.create_provider({"endpoint": "https://gateway.invalid"})
    assert (path.read_bytes(), _fingerprint(path)) == before


def test_token_is_not_copied_anywhere_on_disk(tmp_path, _isolated):
    path = _store(tmp_path)
    prov.create_provider({})
    assert _client_key(_isolated) == TOKEN  # it DID resolve — the sweep is not vacuous
    holders = []
    for candidate in tmp_path.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            body = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if TOKEN in body:
            holders.append(candidate)
    assert holders == [path]  # only the CLI's own store holds it


# ── 6. Catalog + manifest ────────────────────────────────────────────────


def test_catalog_lists_the_curated_models():
    import asyncio

    from personalclaw.llm.catalog import ModelCatalog

    catalog = prov.create_catalog({})
    assert isinstance(catalog, ModelCatalog)
    ids = [m.id for m in asyncio.run(catalog.list_models())]
    assert ids == [str(m["id"]) for m in prov._CURATED_MODELS]
    assert prov.SPEC.default_model in ids


def test_manifest_and_provider_config_validate():
    from personalclaw.apps.manifest import AppManifest

    data = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))
    manifest = AppManifest.from_dict(data)
    assert manifest.validate() == []
    assert manifest.provider is not None
    assert manifest.provider.validate() == []
    assert manifest.provider.providerType == prov.SPEC.type
    # The manifest's implementation entry point must actually resolve on this module.
    module_path, _, func = manifest.provider.implementation.partition(":")
    assert module_path == "provider"
    assert callable(getattr(prov, func))
    # Minimum permissions: the vendor call needs network, and nothing else is claimed.
    assert manifest.permissions.to_dict() == {"network": True}


def test_manifest_round_trips():
    from personalclaw.apps.manifest import AppManifest

    data = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))
    manifest = AppManifest.from_dict(data)
    assert AppManifest.from_dict(manifest.to_dict()) == manifest
