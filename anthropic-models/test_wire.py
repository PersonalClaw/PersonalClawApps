"""Anthropic: what core asks of a call is in the request the model receives.

Core hands a model factory what it wants of ONE call as build kwargs: the sampling
``temperature`` best-of-N asked for, and the output ``max_tokens`` budget it derived for the
model. Both have to reach the request, and ``sampling_temperature`` (which core reads back to say
whether a candidate was really sampled at its rung) has to agree with what was sent. The instance
is built the way the product builds one saved from the Add-instance form, and called the way core
calls a bound model.

The factory is this app's own. The Messages API requires a cap, so one is always sent. So this
drives the app's real ``anthropic`` client against a fake endpoint that records the request. The
reasoning-effort levels each listed model declares are held to the request the same way: what the
catalog lists for a model is what its request carries.
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


# ── Reasoning efforts: the levels a model is listed with are the levels its request carries ──

#: Every level a native chat can hold, lowest first.
LADDER = ("low", "medium", "high", "xhigh", "max")

#: A cap above every thinking budget the wire sends, so each level's budget is its own and none is
#: cut down to the same size as another's.
ROOMY = 128_000


def _reasoning_sent(call: dict) -> dict:
    """What one recorded Messages call asked of the model's reasoning: its ``thinking`` and
    ``output_config`` fields, ``{}`` when it carries neither."""
    return {key: call["body"][key] for key in ("thinking", "output_config") if key in call["body"]}


def _sent_as_themselves(sent: dict[str, dict]) -> list[str]:
    """The levels the wire sent as themselves, lowest first: each whose request asked for
    reasoning, and asked differently from every lower level's. A level sent as a lower one (the
    request a lower level gets) is not."""
    levels = []
    for i, level in enumerate(LADDER):
        if sent[level] and all(sent[level] != sent[lower] for lower in LADDER[:i]):
            levels.append(level)
    return levels


@pytest.mark.asyncio
async def test_each_listed_model_declares_exactly_the_levels_the_wire_sends_it(server):
    """🔴 Red before: every curated row was listed with no levels, and a listed model declares what
    its listing says, so the composer offered no reasoning effort on any Claude model this app
    lists. Each row now declares the levels core's Messages client sends that model as themselves:
    every level of the ladder, asked of each model through the wire, is sent as itself exactly
    when the model declares it, and an adaptive model's request names the level verbatim."""
    catalog = provider.create_catalog({"api_key": "k-wire", "endpoint": server.url})
    rows = await catalog.list_models()
    assert rows, "the catalog lists no model"

    report: dict[str, tuple[list[str], list[str]]] = {}
    for row in rows:
        sent: dict[str, dict] = {}
        for level in LADDER:
            built = provider._factory(entry=_entry(server.url), model=row.id, max_tokens=ROOMY)
            await built.start()
            try:
                async for _event in built.complete(
                    [{"role": "user", "content": "hi"}], reasoning_effort=level
                ):
                    pass
            finally:
                await built.shutdown()
            sent[level] = _reasoning_sent(server.calls()[-1])
            if level in row.efforts and sent[level].get("thinking", {}).get("type") == "adaptive":
                assert sent[level]["output_config"] == {"effort": level}, (row.id, level)
        report[row.id] = (row.efforts, _sent_as_themselves(sent))

    differ = {model: pair for model, pair in report.items() if pair[0] != pair[1]}
    assert differ == {}, "(declared, sent as themselves) for each model where they differ"
    # Core's Messages client reasons with every Claude model, adaptively or with a budget, so no
    # row this app lists declares none.
    assert [model for model, (declared, _) in report.items() if not declared] == []
