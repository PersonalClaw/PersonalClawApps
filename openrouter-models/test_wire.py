"""OpenRouter (chat): what core asks of a call is in the request the model receives.

Core hands a model factory what it wants of ONE call as build kwargs: the sampling
``temperature`` best-of-N asked for, and the output ``max_tokens`` budget it derived for the
model. Both have to reach the request, and ``sampling_temperature`` (which core reads back to say
whether a candidate was really sampled at its rung) has to agree with what was sent. The instance
is built the way the product builds one saved from the Add-instance form, and called the way core
calls a bound model.

It rides core's branded-app factory (``register_branded_app``), which reads both. So this drives
the app's real ``openai`` client against a fake endpoint that records the request. The
reasoning-effort levels each listed model declares are held to the request the same way: what the
catalog lists for a model is what its request carries.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

from apps_testkit.egress import HOST, owner_egress  # noqa: E402
from apps_testkit.model_wire import (  # noqa: E402
    LISTED,
    MODEL,
    REPLY,
    RecordingModelServer,
    blank_model_expected,
    blank_model_report,
    form_options,
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
        name="wire", type="openrouter", model="", options=form_options(APP_DIR, url)
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
        entry_type="openrouter",
        factory=provider._factory,
        create_provider=provider.create_provider,
        server=listing,
    )
    assert report == blank_model_expected(APP_DIR)


# ── Reasoning efforts: the levels a model is listed with are the levels its request carries ──

#: Every level a native chat can hold, lowest first.
LADDER = ("low", "medium", "high", "xhigh", "max")

#: Models OpenRouter lists, by the ids it names them with: reasoning models, which take
#: ``reasoning_effort``, and models that refuse it.
DECLARED = {
    "openai/o3": ["low", "medium", "high"],
    "openai/gpt-5-mini": ["low", "medium", "high"],
    "openai/gpt-4o": [],
    "anthropic/claude-sonnet-4.5": [],
    "qwen/qwen3.7-flash": [],
}


@pytest.fixture
def reasoners():
    """An endpoint that lists each model in :data:`DECLARED`. It is on this computer, so its
    owner allows its host in Settings → Security → Network egress, as she would for her own."""
    with RecordingModelServer(models=tuple(DECLARED)) as recording:
        owner_egress(allow_hosts=[HOST])
        yield recording


@pytest.mark.asyncio
async def test_each_listed_model_is_sent_exactly_the_levels_it_declares(reasoners):
    """🔴 Red before: every model was listed with no levels, and a listed model declares what its
    listing says, so the composer offered no reasoning effort even on OpenRouter's reasoning
    models. Each model now declares the levels core's Chat Completions client sends it as
    themselves: every level of the ladder, asked of each model through the catalog and the wire,
    goes out verbatim when the model declares it, a level it does not declare goes as a lower one
    or not at all, and a model that declares none is sent no ``reasoning_effort`` at any level."""
    catalog = provider.create_catalog({"api_key": "k-wire", "endpoint": reasoners.url})
    rows = await catalog.list_models()
    assert {row.id: row.efforts for row in rows} == DECLARED

    for row in rows:
        sent = {}
        for level in LADDER:
            built = provider._factory(entry=_entry(reasoners.url), model=row.id)
            await built.start()
            try:
                async for _event in built.complete(
                    [{"role": "user", "content": "hi"}], reasoning_effort=level
                ):
                    pass
            finally:
                await built.shutdown()
            sent[level] = reasoners.calls()[-1]["body"].get("reasoning_effort")
        assert [level for level in LADDER if sent[level] == level] == row.efforts, row.id
        if not row.efforts:
            assert set(sent.values()) == {None}, row.id
        else:
            assert set(sent.values()) <= set(row.efforts), row.id
