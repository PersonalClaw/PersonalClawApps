"""Claude subscription: what core asks of a call is in the request the model receives.

Core hands a model factory what it wants of ONE call as build kwargs: the sampling
``temperature`` best-of-N asked for, and the output ``max_tokens`` budget it derived for the
model. Both have to reach the request, and ``sampling_temperature`` (which core reads back to say
whether a candidate was really sampled at its rung) has to agree with what was sent. The instance
is built the way the product builds one saved from the Add-instance form, and called the way core
calls a bound model.

It rides core's branded-app factory (``register_branded_app``), which reads both. The Messages
API requires a cap, so the spec's is sent when none is asked. So this drives the app's real
``anthropic`` client against a fake endpoint that records the request.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

from apps_testkit.model_wire import (  # noqa: E402
    MODEL,
    REPLY,
    RecordingModelServer,
    form_options,
    one_call,
    sampling_sent,
)
from personalclaw.sdk.model import ProviderEntry  # noqa: E402

import provider  # noqa: E402 — app-local

#: Not shaped like the vendor's token prefix: a realistic literal trips secret scanners.
TOKEN = "TESTONLY-wire-subscription-token"


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A Claude Code sign-in under a redirected ``HOME`` — this app's only credential. ``~`` is
    tmp and ``CLAUDE_CONFIG_DIR`` is unset, so the operator's real store is out of reach."""
    store = tmp_path / "home" / ".claude" / ".credentials.json"
    store.parent.mkdir(parents=True)
    expires = int(time.time() * 1000) + 3_600_000
    store.write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": expires}}))
    store.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with RecordingModelServer() as recording:
        yield recording


APP_DIR = Path(__file__).resolve().parent


def _entry(url: str) -> ProviderEntry:
    """An instance saved from the Add-instance form: the form's fields, no pinned model."""
    return ProviderEntry(
        name="wire", type="claude_subscription", model="", options=form_options(APP_DIR, url)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asked", "sent"),
    [
        ({"temperature": 0.7, "max_tokens": 1234}, (0.7, 1234)),
        ({}, (None, provider.SPEC.max_tokens)),
    ],
    ids=["asked", "nothing-asked"],
)
async def test_what_core_asks_of_a_call_is_what_the_request_carries(server, asked, sent):
    # Called the way core calls a bound model: the binding's model as the ``model`` build kwarg.
    built = provider._factory(entry=_entry(server.url), model=MODEL, **asked)
    assert built.sampling_temperature == sent[0]
    assert await one_call(built) == REPLY
    assert [sampling_sent(call) for call in server.calls()] == [sent]


@pytest.mark.asyncio
async def test_the_sign_in_is_what_authenticates_the_call(server):
    assert await one_call(provider._factory(entry=_entry(server.url))) == REPLY
    [call] = server.calls()
    headers = call["headers"]
    assert headers.get("x-api-key") == TOKEN or headers.get("authorization") == f"Bearer {TOKEN}"
