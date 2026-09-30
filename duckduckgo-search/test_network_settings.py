"""DuckDuckGo: a search honours the owner's Settings → Security → Network egress.

The app's real request goes through the SDK's real guard to a server on this machine standing in
for DuckDuckGo's HTML endpoint (``apps_testkit.egress``): reached when the owner allowed its host,
and refused before anything is sent when they denied it or did not allow it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

import provider  # noqa: E402
from apps_testkit.egress import (  # noqa: E402
    HOST,
    REFUSAL_IDS,
    REFUSALS,
    ProviderHost,
    owner_egress,
)

_ANSWER = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fa">A</a>
  <a class="result__snippet">The answer.</a>
</div>
"""


@pytest.fixture
def ddg(monkeypatch):
    with ProviderHost(_ANSWER, content_type="text/html; charset=utf-8") as host:
        monkeypatch.setattr(provider, "_API", f"{host.url}/html/")
        yield host


@pytest.mark.asyncio
async def test_a_host_the_owner_allowed_is_searched(ddg):
    owner_egress(allow_hosts=[HOST])
    result = await provider.DuckDuckGoProvider().search("rust async")
    assert [(h.url, h.title) for h in result.results] == [("https://docs.example.com/a", "A")]
    assert [r["path"].split("?")[0] for r in ddg.requests] == ["/html/"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow", "deny", "refusal"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_host_the_owners_settings_refuse_is_never_asked(ddg, allow, deny, refusal):
    owner_egress(allow_hosts=allow, deny_hosts=deny)
    with pytest.raises(RuntimeError) as refused:
        await provider.DuckDuckGoProvider().search("rust async")
    assert str(refused.value) == refusal.said(provider._API)
    assert ddg.requests == []
