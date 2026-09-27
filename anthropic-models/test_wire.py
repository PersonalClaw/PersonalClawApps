"""Anthropic: what core asks of a call is in the request the model receives.

Core hands a model factory what it wants of ONE call as build kwargs: the sampling
``temperature`` best-of-N asked for, and the output ``max_tokens`` budget it derived for the
model. Both have to reach the request, and ``sampling_temperature`` (which core reads back to say
whether a candidate was really sampled at its rung) has to agree with what was sent. The instance
is built the way the product builds one saved from the Add-instance form, and called the way core
calls a bound model.

The factory is this app's own. The Messages API requires a cap, so one is always sent. So this
drives the app's real ``anthropic`` client against a fake endpoint that records the request.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

from apps_testkit.model_wire import (  # noqa: E402
    LISTED,
    MODEL,
    REPLY,
    RecordingModelServer,
    blank_model_expected,
    blank_model_report,
    form_options,
    model_sent,
    one_call,
    sampling_sent,
)
from personalclaw.sdk.model import ProviderEntry  # noqa: E402

import provider  # noqa: E402 — app-local


@pytest.fixture
def server():
    with RecordingModelServer() as recording:
        yield recording


APP_DIR = Path(__file__).resolve().parent


def _entry(url: str) -> ProviderEntry:
    """An instance saved from the Add-instance form: the form's fields, no pinned model."""
    return ProviderEntry(
        name="wire", type="anthropic", model="", options=form_options(APP_DIR, url)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asked", "sent"),
    [
        ({"temperature": 0.7, "max_tokens": 1234}, (0.7, 1234)),
        ({}, (None, 4096)),
    ],
    ids=["asked", "nothing-asked"],
)
async def test_what_core_asks_of_a_call_is_what_the_request_carries(server, asked, sent):
    # Called the way core calls a bound model: the binding's model as the ``model`` build kwarg.
    built = provider._factory(entry=_entry(server.url), model=MODEL, **asked)
    assert built.sampling_temperature == sent[0]
    assert await one_call(built) == REPLY
    assert [sampling_sent(call) for call in server.calls()] == [sent]
    assert [model_sent(call) for call in server.calls()] == [MODEL]


# ── A call no model is chosen for ─────────────────────────────────────────────────────────


@pytest.fixture
def listing():
    """An endpoint that lists only a model nobody chose (``LISTED``): a provider that picked a
    model of its own would name it on the wire."""
    with RecordingModelServer(models=(LISTED,)) as recording:
        yield recording


@pytest.mark.asyncio
async def test_no_model_chosen_is_refused_and_the_default_model_is_named(listing):
    """Saved with its Default Model empty and called with nothing bound, an instance is sent no
    call, and no model is picked in its place. With a Default Model, both calls name it."""
    report = await blank_model_report(
        app_dir=APP_DIR,
        entry_type="anthropic",
        factory=provider._factory,
        create_provider=provider.create_provider,
        server=listing,
    )
    assert report == blank_model_expected(APP_DIR)
