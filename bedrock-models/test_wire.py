"""Bedrock: what core asks of a call is in the request the model receives.

Core hands a model factory what it wants of ONE call as build kwargs: the sampling
``temperature`` best-of-N asked for, and the output ``max_tokens`` budget it derived for the
model. Both have to reach the request, and ``sampling_temperature`` (which core reads back to say
whether a candidate was really sampled at its rung) has to agree with what was sent. The instance
is built the way the product builds one saved from the Add-instance form, and called the way core
calls a bound model.

The factory is this app's own. Converse takes both in ``inferenceConfig``, and a cap is always
sent. So this drives the app's real ``boto3`` client against a fake endpoint that records the
request. The reasoning-effort levels each listed model declares are held to the request the same
way: what the catalog lists for a model is what its request carries.
"""

from __future__ import annotations

import os
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
    model_sent,
    one_call,
    sampling_sent,
)
from personalclaw.sdk.model import ProviderEntry  # noqa: E402

import provider  # noqa: E402 — app-local


def _boto3_reaches_only(monkeypatch, recording: RecordingModelServer) -> None:
    """boto3 reaches the recording endpoint and nothing else: both service endpoints (runtime and
    the control plane that lists models) are overridden, the credentials are dummies, and no AWS
    profile or config file on this machine is read."""
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", recording.url)
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK", recording.url)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "wire-test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wire-test")
    # The endpoint is on this computer, so its owner allows its host in Settings → Security →
    # Network egress, as she would for her own; every request is asked of the guard.
    owner_egress(allow_hosts=[HOST])
    for name in ("AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)


@pytest.fixture
def server(monkeypatch):
    with RecordingModelServer() as recording:
        _boto3_reaches_only(monkeypatch, recording)
        yield recording


APP_DIR = Path(__file__).resolve().parent


def _entry(url: str) -> ProviderEntry:
    """An instance saved from the Add-instance form: the form's fields, no pinned model."""
    return ProviderEntry(name="wire", type="bedrock", model="", options=form_options(APP_DIR, url))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asked", "sent"),
    [
        ({"temperature": 0.7, "max_tokens": 1234}, (0.7, 1234)),
        ({}, (None, provider._DEFAULT_MAX_TOKENS)),
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


# ── The owner's Network egress settings ───────────────────────────────────────────────────


@pytest.fixture
def denied(monkeypatch):
    """The same endpoint, with its host on Denied hosts in Settings → Security → Network egress."""
    with RecordingModelServer() as recording:
        _boto3_reaches_only(monkeypatch, recording)
        owner_egress(allow_hosts=[HOST], deny_hosts=[HOST])
        yield recording


@pytest.mark.asyncio
async def test_a_chat_to_a_host_the_owner_denied_is_never_sent(denied):
    """🔴 Red before: boto3's client sent the chat itself and asked no guard, so with the
    endpoint's host on Denied hosts the conversation was sent to it all the same. Every request a
    client of the app's AWS session sends is asked now, before it is sent: the chat fails in the
    words that name the setting and the host, and the endpoint receives nothing."""
    built = provider._factory(entry=_entry(denied.url), model=MODEL)

    with pytest.raises(Exception) as refused:
        await one_call(built)

    assert str(refused.value) == (
        f"{denied.url}/model/{MODEL}/converse-stream was not reached: {HOST} is on Denied hosts "
        "in Settings → Security → Network egress."
    )
    assert denied.requests == []


# ── A call no model is chosen for ─────────────────────────────────────────────────────────


@pytest.fixture
def listing(monkeypatch):
    """A Bedrock whose control plane lists only a model nobody chose (``LISTED``): a provider that
    picked a model from discovery would name it on the wire."""
    with RecordingModelServer(models=(LISTED,)) as recording:
        _boto3_reaches_only(monkeypatch, recording)
        yield recording


@pytest.mark.asyncio
async def test_no_model_chosen_is_refused_and_the_default_model_is_named(listing):
    """Saved with its Default Model empty and called with nothing bound, an instance is sent no
    call, and no model is picked in its place. With a Default Model, both calls name it."""
    report = await blank_model_report(
        app_dir=APP_DIR,
        entry_type="bedrock",
        factory=provider._factory,
        create_provider=provider.create_provider,
        server=listing,
    )
    assert report == blank_model_expected(APP_DIR)


# ── The runtime's per-turn note ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_after_a_tool_result_the_request_ends_on_that_turn_not_on_the_note(server):
    """The request Converse receives for the turn the note was answered in: she asked, the model
    saved it with a tool, the result came back, and core's per-turn tool note rides the request.
    Sent as a user turn of its own, that note was the newest thing she had "said", and the model
    replied to it in her chat. On the wire the last user turn is the tool result's, the result
    first, with the note after it, and no turn is the note alone."""
    from personalclaw.sdk.model import CACHE_HINT_KEY, VOLATILE_KEY

    note = "<system-note>\nThe runtime added this note.\n\n[tool catalog] listed\n</system-note>"
    built = provider._factory(entry=_entry(server.url), model=MODEL)
    await built.start()
    try:
        messages = [
            {"role": "user", "content": "Remember: never quote consignee names."},
            {"role": "system", "content": note, VOLATILE_KEY: True},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "function": {"name": "remember", "arguments": "{}"}}],
                CACHE_HINT_KEY: {"generation": 0},
            },
            {"role": "tool", "tool_call_id": "t1", "content": "Saved."},
        ]
        tools = [
            {
                "type": "function",
                "function": {"name": "remember", "description": "Save a lesson.", "parameters": {}},
            }
        ]
        async for _event in built.complete(messages, tools=tools):
            pass
    finally:
        await built.shutdown()

    [call] = server.calls()
    sent = call["body"]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"][-1] == {"cachePoint": {"type": "default"}}
    assert sent[2]["content"] == [
        {"toolResult": {"toolUseId": "t1", "content": [{"text": "Saved."}]}},
        {"text": note},
    ]
    assert not [m for m in sent if all(b.get("text") == note for b in m["content"])]
    assert "system" not in call["body"] or note not in str(call["body"]["system"])


# ── Reasoning efforts: the levels a model is listed with are the levels its request carries ──

#: Every level a native chat can hold, lowest first.
LADDER = ("low", "medium", "high", "xhigh", "max")

#: One model of each kind the control plane lists, and the levels each declares: a Claude model
#: that thinks adaptively, with ``xhigh`` and without; an older Claude, which takes a thinking
#: budget this wire never sends; and models of other vendors, which take no Anthropic thinking
#: fields at all.
DECLARED = {
    "anthropic.claude-opus-4-7": ["low", "medium", "high", "xhigh", "max"],
    "anthropic.claude-sonnet-4-6": ["low", "medium", "high", "max"],
    "anthropic.claude-sonnet-4-5-20250929-v1:0": [],
    "amazon.nova-pro-v1:0": [],
    "mistral.mistral-large-2407-v1:0": [],
}


@pytest.fixture
def thinkers(monkeypatch):
    """A Bedrock whose control plane lists each model in :data:`DECLARED`. The catalog keeps a
    region's listing for minutes, so none another test read is kept."""
    provider._BEDROCK_CACHE.clear()
    with RecordingModelServer(models=tuple(DECLARED)) as recording:
        _boto3_reaches_only(monkeypatch, recording)
        yield recording
    provider._BEDROCK_CACHE.clear()


def _reasoning_sent(call: dict) -> object:
    """What one recorded Converse call asked of the model's reasoning: the request's
    ``additionalModelRequestFields``, or ``None`` when it carries none."""
    return call["body"].get("additionalModelRequestFields")


@pytest.mark.asyncio
async def test_each_listed_model_is_sent_exactly_the_levels_it_declares(thinkers):
    """🔴 Red before: every row was listed with no levels, so the composer offered none, while a
    turn's stored level reached Converse as adaptive thinking for every model, Nova, Mistral and the
    older Claude models included, each of which refuses those fields. Each chat model the catalog
    lists now declares the levels its request carries, and only those: every level of the ladder,
    asked of each model through the catalog and the wire, goes out verbatim when the model declares
    it, and a model that declares none is sent no thinking fields at any level."""
    rows = await provider.create_catalog(form_options(APP_DIR, thinkers.url)).list_models()
    chat = [row for row in rows if "chat" in row.capabilities]
    assert {row.id: row.efforts for row in chat} == DECLARED

    sent: dict[str, dict[str, object]] = {}
    for row in chat:
        for level in LADDER:
            built = provider._factory(entry=_entry(thinkers.url), model=row.id)
            await built.start()
            try:
                async for _event in built.complete(
                    [{"role": "user", "content": "hi"}], reasoning_effort=level
                ):
                    pass
            finally:
                await built.shutdown()
            sent.setdefault(row.id, {})[level] = _reasoning_sent(thinkers.calls()[-1])

    assert sent == {
        row.id: {
            level: (
                {"thinking": {"type": "adaptive"}, "output_config": {"effort": level}}
                if level in row.efforts
                else None
            )
            for level in LADDER
        }
        for row in chat
    }


# ── A metered agent turn caches its stable prefix ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_planner_turns_on_a_metered_call_cache_one_identical_prefix(server):
    """A Code loop's planner runs its model behind core's spend guard, like every turn nobody is
    watching. The guard hid this app's explicit cache posture, so no planner request carried a
    checkpoint and every call re-read its whole prompt: 25 calls, 536,804 input tokens, none read
    from the cache. Two consecutive planner turns now each carry one ``cachePoint``, and what
    the first one cached reaches the second byte for byte."""
    import json

    from personalclaw.agents.native.runtime import NativeAgentRuntime
    from personalclaw.agents.provider import AgentRuntimeDefinition
    from personalclaw.guardrails.model_call import ModelCallGuard

    built = provider._factory(entry=_entry(server.url), model=MODEL)
    guarded = ModelCallGuard(built, use_case="loops", provider_name="wire", model=MODEL)
    planner = NativeAgentRuntime(
        definition=AgentRuntimeDefinition(name="planner", provider="native", model=MODEL),
        model_provider=guarded,
        tool_providers=[],
    )
    await planner.start()
    for nudge in ("Plan step one for the issue.", "Plan step two."):
        async for _event in planner.stream(nudge):
            pass

    first, second = (call["body"] for call in server.calls())
    point = {"cachePoint": {"type": "default"}}

    def cached_span(body: dict) -> list[dict]:
        """The messages before the request's one checkpoint, as Converse caches them."""
        marked = [
            (i, j)
            for i, m in enumerate(body["messages"])
            for j, block in enumerate(m["content"])
            if block == point
        ]
        assert len(marked) == 1, f"one cachePoint a request, got {len(marked)}"
        i, j = marked[0]
        return [*body["messages"][:i], {**body["messages"][i], "content": body["messages"][i]["content"][:j]}]

    span = cached_span(first)
    assert cached_span(second)[: len(span)] == span
    assert json.dumps(second["messages"][: len(span)], sort_keys=True) == json.dumps(span, sort_keys=True)
    assert first.get("system") == second.get("system")
