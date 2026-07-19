"""DuckDuckGo adapter unit tests — the keyless, zero-config search floor.

Covers the HTML parser, DDG redirect-URL decoding, keyless availability, capability
disclosure, and recency mapping. Mocks the net.fetch egress chokepoint (the adapter
routes outbound HTTP through it instead of raw httpx) so no network needed. Unlike the
JSON search adapters, DDG returns raw HTML text, so the fake's ``.text`` returns the
HTML string directly (no json.dumps). The registry-level fallback/resolution behavior
this provider participates in is covered by the core suite
(test_search_registry_fallback.py), which uses a stand-in.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from provider import DuckDuckGoProvider, _DDGResultParser, _decode_ddg_url


# A representative slice of DDG's HTML result markup (two results).
_DDG_HTML = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=x">First Result</a>
  <a class="result__snippet">The first snippet text.</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Ftwo">Second Result</a>
  <a class="result__snippet">Second snippet.</a>
</div>
"""


class _FakeFetchResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.text = text


class _FakeFetch:
    """Records the last fetch() call + returns a canned FetchResponse.

    DDG returns raw HTML (not JSON), so .text carries the HTML string verbatim.
    """
    last: dict[str, Any] = {}
    html: str = ""
    status: int = 200

    async def __call__(self, url: str, *, policy=None, method: str = "GET",
                       headers: dict | None = None, data: bytes | None = None) -> _FakeFetchResponse:
        parsed = urlparse(url)
        # parse_qs → single-value dict for easy assertions (values are strings)
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        type(self).last = {"url": url, "params": qs, "headers": headers or {},
                           "method": method, "policy": policy}
        return _FakeFetchResponse(type(self).status, type(self).html)


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeFetch()
    _FakeFetch.last = {}
    _FakeFetch.html = ""
    _FakeFetch.status = 200
    # The adapter does `from personalclaw.sdk.net import CONNECTOR, EgressBlocked, fetch`
    # inside search(); patch the source module's fetch so the late import picks it up.
    monkeypatch.setattr("personalclaw.net.client.fetch", fake, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", fake, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", fake, raising=False)
    return _FakeFetch


# ── URL decode + parser ────────────────────────────────────────────────────────

def test_decode_wrapped_ddg_url():
    assert _decode_ddg_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa") == "https://example.com/a"


def test_decode_passes_direct_url():
    assert _decode_ddg_url("https://direct.example/x") == "https://direct.example/x"


def test_parser_pairs_titles_and_snippets():
    p = _DDGResultParser()
    p.feed(_DDG_HTML)
    assert [r["url"] for r in p.results] == ["https://example.com/one", "https://example.org/two"]
    assert p.results[0]["title"] == "First Result"
    assert p.results[0]["snippet"] == "The first snippet text."


# ── capabilities + availability ──────────────────────────────────────────────

def test_capabilities_keyless_links_only():
    caps = DuckDuckGoProvider().capabilities()
    assert caps.returns_answer is False
    assert caps.supports_fetch is False
    assert caps.supports_recency is True
    assert caps.depths == ("balanced",)


@pytest.mark.asyncio
async def test_always_available_no_key():
    assert await DuckDuckGoProvider().is_available() is True


# ── search ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_parses_results_and_sets_ua(fake_fetch):
    fake_fetch.html = _DDG_HTML
    r = await DuckDuckGoProvider().search("hello", recency="week", max_results=5)
    assert r.provider == "duckduckgo"
    assert [h.url for h in r.results] == ["https://example.com/one", "https://example.org/two"]
    assert r.results[0].snippet == "The first snippet text."
    assert fake_fetch.last["params"]["df"] == "w"            # recency → df
    assert "User-Agent" in fake_fetch.last["headers"]         # DDG needs a UA
    assert fake_fetch.last["policy"] is not None              # CONNECTOR policy passed


@pytest.mark.asyncio
async def test_search_respects_max_results(fake_fetch):
    fake_fetch.html = _DDG_HTML
    r = await DuckDuckGoProvider().search("q", max_results=1)
    assert len(r.results) == 1


@pytest.mark.asyncio
async def test_search_no_recency_omits_df(fake_fetch):
    fake_fetch.html = _DDG_HTML
    await DuckDuckGoProvider().search("q")
    assert "df" not in fake_fetch.last["params"]


@pytest.mark.asyncio
async def test_search_raises_on_http_error(fake_fetch):
    fake_fetch.status = 429  # rate-limited
    with pytest.raises(RuntimeError):
        await DuckDuckGoProvider().search("q")
