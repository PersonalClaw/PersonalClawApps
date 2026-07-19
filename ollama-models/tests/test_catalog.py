"""OllamaCatalog — the reference ModelManager: default-registry catalog
registration + list/test/delete behaviors over stubbed aiohttp (moved from core
tests/test_model_catalog_seam.py; the catalog SEAM itself stays core-tested)."""

from __future__ import annotations

import asyncio

import pytest

from personalclaw.llm.catalog import ConnectionResult, ModelManager
from personalclaw.llm.registry import ProviderEntry, get_default_registry
from provider import OllamaCatalog


def _run(coro):
    return asyncio.run(coro)


def test_ollama_catalog_registered_on_default_registry():
    # ollama.py is eager-imported by core; its catalog registers via the same seam.
    import provider  # noqa: F401 (ollama app module — a concrete model provider for core-seam tests)
    reg = get_default_registry()
    entry = ProviderEntry(name="Local", type="ollama", model="llama3",
                          options={"endpoint": "http://localhost:11434"})
    cat = reg.build_catalog(entry)
    assert isinstance(cat, OllamaCatalog)
    assert isinstance(cat, ModelManager)  # ollama is the reference manager


# ── OllamaCatalog behaviors (stubbed aiohttp) ────────────────────────────


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class _Session:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self._resp

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        return self._resp


def _stub_aiohttp(monkeypatch, resp):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _Session(resp))


def test_ollama_list_models_maps_tags(monkeypatch):
    _stub_aiohttp(monkeypatch, _Resp(200, {"models": [
        {"name": "llama3:8b", "size": 4_700_000_000,
         "details": {"family": "llama", "parameter_size": "8B", "quantization_level": "Q4"}},
        {"name": "nomic-embed-text", "size": 200_000_000, "details": {}},
    ]}))
    cat = OllamaCatalog("http://x:11434")
    models = _run(cat.list_models())
    by_id = {m.id: m for m in models}
    assert "chat" in by_id["llama3:8b"].capabilities
    assert by_id["nomic-embed-text"].capabilities == ["embedding"]
    # size + humanized + detail fields surface via extra
    assert by_id["llama3:8b"].extra.get("parameter_size") == "8B"
    assert by_id["llama3:8b"].extra.get("size_human") == "4.4 GB"


def test_ollama_list_models_failsoft_on_error(monkeypatch):
    _stub_aiohttp(monkeypatch, _Resp(500))
    assert _run(OllamaCatalog("http://x:11434").list_models()) == []


def test_ollama_test_connection(monkeypatch):
    _stub_aiohttp(monkeypatch, _Resp(200, {"models": [{"name": "llama3"}]}))
    res = _run(OllamaCatalog("http://x:11434").test_connection())
    assert isinstance(res, ConnectionResult)
    assert res.ok is True and res.model_count == 1


def test_ollama_delete_raises_on_non_200(monkeypatch):
    _stub_aiohttp(monkeypatch, _Resp(404, text="not found"))
    with pytest.raises(RuntimeError):
        _run(OllamaCatalog("http://x:11434").delete_model("gone:latest"))


def test_ollama_delete_ok(monkeypatch):
    _stub_aiohttp(monkeypatch, _Resp(200))
    # No raise == success.
    _run(OllamaCatalog("http://x:11434").delete_model("llama3:8b"))
