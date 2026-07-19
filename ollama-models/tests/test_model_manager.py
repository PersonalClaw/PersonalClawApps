"""Ollama first-class model management, now via the generic ModelCatalog seam.

The management logic (``/show`` metadata extraction, ``_humanize_bytes``, the pull
NDJSON stream + cancellation) moved out of the HTTP handlers into a core
``OllamaCatalog(ModelManager)``; the handlers became generic dispatchers that resolve
``registry.build_catalog(entry)`` and gate management on ``isinstance(cat, ModelManager)``.

These tests drive the real handlers end-to-end: a fake registry whose
``build_catalog`` returns a real ``OllamaCatalog`` (so the real ollama HTTP logic
runs against a stubbed aiohttp), proving both the handler dispatch AND the catalog
behavior. A non-manager provider (a hosted-API ModelCatalog) is rejected with 400.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from personalclaw.llm.catalog import ModelCatalog, ModelInfo
from provider import OllamaCatalog, _humanize_bytes
from personalclaw.dashboard.handlers.providers import (
    api_provider_model_pull,
    api_provider_model_show,
)


# ── _humanize_bytes (now lives on the ollama catalog module) ──
@pytest.mark.parametrize("n,expected", [
    (0, ""),
    (-5, ""),
    (512, "512 B"),
    (1536, "1.5 KB"),
    (300_000_000, "286.1 MB"),
    (4_700_000_000, "4.4 GB"),
    (2_000_000_000_000, "1.8 TB"),
])
def test_humanize_bytes(n, expected):
    assert _humanize_bytes(n) == expected


# ── /show metadata extraction (via OllamaCatalog.show_model) ──
class _FakeResp:
    def __init__(self, status: int, payload: dict[str, Any]):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return ""


class _FakeSession:
    def __init__(self, resp: _FakeResp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, **kw):
        return self._resp


class _PlainCatalog(ModelCatalog):
    """A hosted-API catalog (discovery only, NOT a ModelManager) — used to prove the
    management endpoints reject a provider that can't manage local models."""

    async def list_models(self):
        return []


def _fake_registry(entry, *, catalog):
    """A registry shim: get_entry returns *entry*; build_catalog returns *catalog*."""
    return SimpleNamespace(get_entry=lambda n: entry, build_catalog=lambda e: catalog)


def _patch(monkeypatch, payload, status=200):
    """Patch the registry (an ollama entry + a real OllamaCatalog) + aiohttp session."""
    import personalclaw.llm.registry as reg

    entry = SimpleNamespace(type="ollama", model="", options={"endpoint": "http://x:11434"})
    catalog = OllamaCatalog(endpoint="http://x:11434")
    monkeypatch.setattr(reg, "get_default_registry", lambda: _fake_registry(entry, catalog=catalog))

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(_FakeResp(status, payload)))


def _request(model="llama3.2:3b", name="ollama-local"):
    return SimpleNamespace(
        match_info={"name": name},
        rel_url=SimpleNamespace(query={"model": model}),
    )


@pytest.mark.asyncio
async def test_show_extracts_decision_fields(monkeypatch):
    _patch(monkeypatch, {
        "details": {"family": "llama", "parameter_size": "3.2B",
                    "quantization_level": "Q4_K_M", "format": "gguf"},
        "model_info": {"llama.context_length": 131072, "llama.block_count": 28},
        "capabilities": ["completion", "tools"],
        "license": "Meta Llama 3 License\n\nlong text…",
    })
    resp = await api_provider_model_show(_request())
    import json
    body = json.loads(resp.body.decode())
    assert body["family"] == "llama"
    assert body["parameter_size"] == "3.2B"
    assert body["quantization"] == "Q4_K_M"
    assert body["context_length"] == 131072  # pulled from the family-prefixed key
    assert body["capabilities"] == ["completion", "tools"]
    assert body["license_short"] == "Meta Llama 3 License"  # first line only


@pytest.mark.asyncio
async def test_show_omits_empty_fields(monkeypatch):
    _patch(monkeypatch, {"details": {"family": "qwen2"}})  # minimal
    resp = await api_provider_model_show(_request())
    import json
    body = json.loads(resp.body.decode())
    assert body["family"] == "qwen2"
    # Empty/missing fields are dropped, not returned as "".
    assert "quantization" not in body
    assert "context_length" not in body
    assert "capabilities" not in body


@pytest.mark.asyncio
async def test_show_requires_model_param(monkeypatch):
    _patch(monkeypatch, {})
    req = SimpleNamespace(match_info={"name": "ollama-local"}, rel_url=SimpleNamespace(query={}))
    resp = await api_provider_model_show(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_show_rejects_non_manager_provider(monkeypatch):
    """A hosted-API provider (ModelCatalog but not ModelManager) can't show local
    model detail → 400 (was: entry.type != "ollama")."""
    import personalclaw.llm.registry as reg

    entry = SimpleNamespace(type="openai", model="", options={})
    monkeypatch.setattr(reg, "get_default_registry",
                        lambda: _fake_registry(entry, catalog=_PlainCatalog()))
    resp = await api_provider_model_show(_request())
    assert resp.status == 400


@pytest.mark.asyncio
async def test_show_reports_error_when_provider_returns_non_200(monkeypatch):
    """A non-200 from Ollama's /api/show surfaces as a 500 (the generic dispatcher
    treats a failed metadata fetch as a server-side error)."""
    _patch(monkeypatch, {}, status=404)
    resp = await api_provider_model_show(_request())
    assert resp.status == 500


# ── pull cancellation (user can Stop a download), now via OllamaCatalog.pull_model ──
class _FakeContent:
    """Async-iterable of NDJSON byte lines from a fake Ollama pull stream."""

    def __init__(self, lines: list[bytes]):
        self._lines = lines

    def __aiter__(self):
        async def _gen():
            for ln in self._lines:
                yield ln

        return _gen()


class _FakePullResp:
    def __init__(self, lines):
        self.status = 200
        self.content = _FakeContent(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePullSession:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, **kw):
        return _FakePullResp(self._lines)


class _FakeStreamResp:
    """Captures write()s; the test drives request.transport to simulate a client
    disconnect mid-stream."""

    def __init__(self):
        self.writes: list[bytes] = []
        self.eof = False

    async def prepare(self, request):
        return None

    async def write(self, data: bytes):
        self.writes.append(data)

    async def write_eof(self):
        self.eof = True


class _Transport:
    def __init__(self):
        self._closing = False

    def is_closing(self):
        return self._closing


def _pull_request(transport, model="llama3.2:1b"):
    return SimpleNamespace(
        match_info={"name": "ollama-local"},
        rel_url=SimpleNamespace(query={}),
        transport=transport,
        json=_async_return({"model": model}),
    )


def _async_return(value):
    async def _f():
        return value

    return _f


def _patch_pull_registry(monkeypatch):
    """Fake registry: ollama entry + a real OllamaCatalog (so pull_model runs)."""
    import personalclaw.llm.registry as reg

    entry = SimpleNamespace(type="ollama", model="", options={"endpoint": "http://x:11434"})
    catalog = OllamaCatalog(endpoint="http://x:11434")
    monkeypatch.setattr(reg, "get_default_registry", lambda: _fake_registry(entry, catalog=catalog))


@pytest.mark.asyncio
async def test_pull_stops_when_client_disconnects(monkeypatch):
    """A client Stop closes the transport mid-stream → the loop breaks, the pull
    generator is closed (cancelling the Ollama-side download), and write_eof still
    runs cleanly."""
    _patch_pull_registry(monkeypatch)

    transport = _Transport()
    lines = [b'{"status":"pulling","total":100,"completed":10}\n',
             b'{"status":"pulling","total":100,"completed":20}\n',
             b'{"status":"pulling","total":100,"completed":30}\n']

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakePullSession(lines))

    stream = _FakeStreamResp()
    monkeypatch.setattr("personalclaw.dashboard.handlers.providers.web.StreamResponse",
                        lambda *a, **k: stream)

    # Flip the transport to closing after the first successful write.
    orig_write = stream.write

    async def _write_then_close(data: bytes):
        await orig_write(data)
        transport._closing = True  # next loop iteration sees the disconnect

    monkeypatch.setattr(stream, "write", _write_then_close)

    resp = await api_provider_model_pull(_pull_request(transport))
    # Exactly one line written before the disconnect was detected; closed cleanly.
    assert len(stream.writes) == 1
    assert stream.eof is True
    assert resp is stream


@pytest.mark.asyncio
async def test_pull_completes_when_not_interrupted(monkeypatch):
    """Baseline: with the transport open throughout, every progress frame is written."""
    _patch_pull_registry(monkeypatch)

    transport = _Transport()
    lines = [b'{"status":"pulling"}\n', b'{"status":"verifying"}\n', b'{"status":"success"}\n']

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakePullSession(lines))

    stream = _FakeStreamResp()
    monkeypatch.setattr("personalclaw.dashboard.handlers.providers.web.StreamResponse",
                        lambda *a, **k: stream)

    await api_provider_model_pull(_pull_request(transport))
    assert len(stream.writes) == 3  # all frames delivered
    assert stream.eof is True
