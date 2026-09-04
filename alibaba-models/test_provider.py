"""Unit tests for the Alibaba Model Studio provider app.

The cohort contract: every model-provider bundle pins its OWN seams. For this
app that is the key/endpoint resolution helpers (config over env over regional
default), the chat factory injecting the per-instance endpoint, the app-owned
``image_gen`` media scanner (one adapter per ``alibaba`` config entry, including
the renamed-entry ``_original_type`` alias), and the image provider's request
shaping + response parsing. Vendor wire clients are stubbed into ``sys.modules``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest

import provider as prov  # app-local; registers type + catalogs on import


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIBABA_API_KEY", raising=False)


# ── Resolution helpers ───────────────────────────────────────────────────────


def test_api_key_config_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIBABA_API_KEY", "ak-env")
    assert prov._resolve_api_key({"api_key": "ak-cfg"}) == "ak-cfg"
    assert prov._resolve_api_key({}) == "ak-env"


def test_api_key_empty_without_config_or_env(no_env_key: None) -> None:
    assert prov._resolve_api_key({}) == ""


def test_endpoint_falls_back_to_regional_default() -> None:
    assert prov._resolve_endpoint({}) == prov._DEFAULT_ENDPOINT
    assert prov._resolve_endpoint({"endpoint": "https://cn.example/v1"}) == "https://cn.example/v1"


# ── Chat factory: per-instance endpoint injection ────────────────────────────


class _FakeAsyncOpenAI:
    constructed: list[dict[str, Any]] = []

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        type(self).constructed.append({"api_key": api_key, "base_url": base_url})

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = _FakeAsyncOpenAI  # type: ignore[attr-defined]
    _FakeAsyncOpenAI.constructed = []
    monkeypatch.setitem(sys.modules, "openai", fake)
    return fake


def test_create_provider_injects_regional_endpoint(
    fake_openai: types.ModuleType, no_env_key: None
) -> None:
    prov.create_provider({"api_key": "ak-x"})
    assert _FakeAsyncOpenAI.constructed[-1]["base_url"] == prov._DEFAULT_ENDPOINT
    prov.create_provider({"api_key": "ak-x", "endpoint": "https://cn.example/v1"})
    assert _FakeAsyncOpenAI.constructed[-1]["base_url"] == "https://cn.example/v1"


# ── image_gen media scanner ──────────────────────────────────────────────────


def test_scanner_builds_one_adapter_per_alibaba_entry() -> None:
    entries = [
        {"name": "ali-main", "type": "alibaba", "options": {"api_key": "k1"}},
        {"name": "other", "type": "openai", "options": {}},
        # A renamed instance keeps its lineage via _original_type.
        {
            "name": "ali-renamed",
            "type": "custom",
            "options": {"_original_type": "alibaba", "endpoint": "https://cn.example/v1"},
        },
    ]
    adapters = prov._scan_image(entries)
    assert [a.name for a in adapters] == ["ali-main", "ali-renamed"]
    assert adapters[0]._api_key == "k1"
    assert adapters[1]._endpoint == "https://cn.example/v1"


# ── Image provider ───────────────────────────────────────────────────────────


def test_image_models_are_the_static_catalog() -> None:
    p = prov.AlibabaImageProvider()
    models = _run(p.list_models())
    assert len(models) == 4
    assert {m.name for m in models} == {
        "qwen-image-2.0",
        "qwen-image-2.0-pro",
        "wan2.7-image",
        "wan2.7-image-pro",
    }


def test_image_availability_tracks_key(no_env_key: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(prov.AlibabaImageProvider().is_available()) is False
    monkeypatch.setenv("ALIBABA_API_KEY", "ak-env")
    assert _run(prov.AlibabaImageProvider().is_available()) is True


def test_generate_without_key_raises_before_network(no_env_key: None) -> None:
    from personalclaw.sdk.image import ImageGenError

    with pytest.raises(ImageGenError, match="API key"):
        _run(prov.AlibabaImageProvider().generate("a fox"))


def test_edit_is_explicitly_unsupported() -> None:
    from personalclaw.sdk.image import ImageGenError

    with pytest.raises(ImageGenError, match="not supported"):
        _run(prov.AlibabaImageProvider(api_key="ak").edit("x", source_image="s"))


# A minimal aiohttp stand-in: ClientSession().post() used as nested async
# context managers, plus ClientTimeout. Records the request for assertions.
def _fake_aiohttp(monkeypatch: pytest.MonkeyPatch, status: int, payload: Any) -> list[dict]:
    calls: list[dict] = []

    class _Resp:
        def __init__(self) -> None:
            self.status = status

        async def text(self) -> str:
            return json.dumps(payload)

        async def __aenter__(self) -> "_Resp":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Session:
        def __init__(self, *, timeout: Any = None) -> None:
            pass

        def post(self, url: str, *, headers: dict, json: dict) -> _Resp:
            calls.append({"url": url, "headers": headers, "body": json})
            return _Resp()

        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    fake = types.ModuleType("aiohttp")
    fake.ClientSession = _Session  # type: ignore[attr-defined]
    fake.ClientTimeout = lambda total=None: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    return calls


def test_generate_parses_url_and_b64_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_aiohttp(
        monkeypatch,
        200,
        {"data": [{"url": "https://img/1.png"}, {"b64_json": "aGk="}, "junk-row"]},
    )
    p = prov.AlibabaImageProvider(api_key="ak", endpoint="https://cn.example/v1")
    results = _run(p.generate("a fox", size="1024x1024", n=2))
    assert [r.url for r in results] == ["https://img/1.png", ""]
    assert results[1].b64 == "aGk="
    req = calls[0]
    assert req["url"] == "https://cn.example/v1/images/generations"
    assert req["headers"]["Authorization"] == "Bearer ak"
    assert req["body"] == {"model": "qwen-image-2.0", "prompt": "a fox", "n": 2, "size": "1024x1024"}


def test_generate_raises_on_empty_and_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from personalclaw.sdk.image import ImageGenError

    _fake_aiohttp(monkeypatch, 200, {"data": []})
    with pytest.raises(ImageGenError, match="no images"):
        _run(prov.AlibabaImageProvider(api_key="ak").generate("a fox"))

    _fake_aiohttp(monkeypatch, 429, {"error": {"message": "rate limited"}})
    with pytest.raises(ImageGenError, match="429.*rate limited"):
        _run(prov.AlibabaImageProvider(api_key="ak").generate("a fox"))


def test_error_detail_handles_json_and_garbage() -> None:
    assert prov._error_detail(json.dumps({"error": {"message": "bad key"}})) == "bad key"
    assert prov._error_detail("<html>gateway timeout</html>") == "<html>gateway timeout</html>"
    assert len(prov._error_detail("x" * 500)) == 200
