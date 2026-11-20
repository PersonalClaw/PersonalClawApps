"""Catalog tests for the meta-muse-spark app — core's branded catalog over the Meta AI API.

``list_models`` reads the endpoint's own list and falls back to the one model this app knows
of; ``test_connection`` probes the instance's Base URL with the strict listing. Every probe
here reaches only a loopback server, never Meta's address. The
catalog tests double as the regression rail for the registry calling convention:
``ProviderRegistry.build_catalog`` invokes the factory as ``factory(options,
model=...)`` and swallows a mismatch fail-soft, so a factory with the wrong
signature ships as a provider that silently has no discovery and no working
"Test connection" — exactly the defect this app shipped with.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

import provider as prov  # app-local; registers type + catalog on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager
from personalclaw.llm.registry import get_default_registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit
from apps_testkit.model_wire import LISTED, RecordingModelServer  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)


@pytest.fixture
def only_loopback(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every name lookup but loopback's is refused and noted: a probe of the app's default
    address (``api.meta.ai``) is a lookup this test can see, and nothing leaves the machine."""
    asked: list[str] = []
    real = socket.getaddrinfo

    def _lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host) not in ("127.0.0.1", "localhost", "::1"):
            asked.append(str(host))
            raise OSError(f"this test reaches only loopback, not {host}")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _lookup)
    return asked


def test_catalog_is_plain_catalog() -> None:
    cat = prov.create_catalog({})
    assert isinstance(cat, ModelCatalog)
    # A hosted API is NOT a manager (no local pull/delete).
    assert not isinstance(cat, ModelManager)


def test_catalog_factory_honors_the_registry_calling_convention() -> None:
    """``build_catalog`` calls ``factory(options_dict, model=...)``; a factory that
    cannot accept that shape is swallowed fail-soft and the provider loses
    discovery + Test connection silently."""
    factory = get_default_registry().catalog_of("meta_muse_spark")
    assert factory is not None
    cat = factory({"api_key": "mk-x"}, model="muse-spark-1.1")
    assert isinstance(cat, ModelCatalog)


# ── Connectivity probe ───────────────────────────────────────────────────────


def test_connection_without_key_fails_before_network(no_env_key: None, only_loopback) -> None:
    res = _run(prov.create_catalog({}).test_connection())
    assert res.ok is False
    assert "key" in res.detail.lower()


def test_an_endpoint_that_lists_nothing_still_offers_the_known_model(
    no_env_key: None, only_loopback
) -> None:
    """The key worked and the list was empty: Connected, and the one known model is offered."""
    with RecordingModelServer(models=()) as server:
        cat = prov.create_catalog({"api_key": "mk-x", "endpoint": f"{server.url}/v1"})
        res = _run(cat.test_connection())
        models = _run(cat.list_models())

    assert res.ok is True and res.model_count == 1
    assert [m.id for m in models] == ["muse-spark-1.1"]


# ── Test connection on the instance's own Base URL ───────────────────────────
#
# An instance's calls go to its Base URL (``endpoint``). Test connection probed the app's default
# address with the instance's key, so it could pass while the instance's own address was wrong,
# and it said "No models returned (check key)" for every failure: an unreachable address, a host
# on Denied hosts and a 5xx all read as a key problem.


def test_connection_probes_the_instances_base_url(no_env_key: None, only_loopback) -> None:
    """🔴 Red before: the probe went to https://api.meta.ai/v1, never to the instance's address."""
    with RecordingModelServer(models=(LISTED,)) as server:
        cat = prov.create_catalog({"api_key": "mk-x", "endpoint": f"{server.url}/v1"})
        res = _run(cat.test_connection())
        asked = [(r["method"], r["path"]) for r in server.requests]

    assert res.ok is True, res.detail
    assert res.model_count == 1
    assert asked == [("GET", "/v1/models")]
    assert only_loopback == []


def test_a_failed_probe_says_its_cause(no_env_key: None, only_loopback) -> None:
    """🔴 Red before: "No models returned (check key)" for an address nothing listens on."""
    cat = prov.create_catalog({"api_key": "mk-x", "endpoint": "http://127.0.0.1:9/v1"})
    res = _run(cat.test_connection())

    assert res.ok is False
    assert "check key" not in res.detail.lower()
    assert "127.0.0.1:9" in res.detail
    assert only_loopback == []


def test_with_a_key_the_picker_offers_what_the_endpoint_lists(
    no_env_key: None, only_loopback
) -> None:
    """🔴 Red before: one fixed model, whatever the endpoint served."""
    with RecordingModelServer(models=(LISTED,)) as server:
        cat = prov.create_catalog({"api_key": "mk-x", "endpoint": f"{server.url}/v1"})
        models = _run(cat.list_models())

    assert [m.id for m in models] == [LISTED]


def test_when_the_listing_fails_the_one_known_model_is_offered(
    no_env_key: None, only_loopback
) -> None:
    cat = prov.create_catalog({"api_key": "mk-x", "endpoint": "http://127.0.0.1:9/v1"})
    models = _run(cat.list_models())

    assert [m.id for m in models] == ["muse-spark-1.1"]
    assert set(models[0].capabilities) == {"chat", "image_modality", "streaming"}
