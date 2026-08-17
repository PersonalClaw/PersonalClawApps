"""Native json-schema structured output (AUTONOMY-GUARDRAILS §2.4), ollama half.

Two things are pinned here, and they are separate claims:

1. **The request shape.** Ollama enforces a schema server-side via a top-level
   ``format`` field on ``/api/chat``. These tests assert the EXACT JSON body that
   leaves the provider, against the same doubled ``httpx`` client the rest of this
   bundle's tests use — so "native" means a measured wire field, not a docstring.
   No test in this module reaches a real ollama server.
2. **The declaration.** ``OLLAMA_CAPABILITY`` advertises the top grade of core's
   graded ``StructuredOutput`` descriptor, which until now had no provider declaring
   anything but the ``NONE`` default. The last two tests drive core's own reader of
   that grade and assert the behaviour on the far side actually changes — a
   declaration nothing consumes is an inert consent claim, not a capability.

Every positive assertion is paired with a negative one (no request ⇒ NO ``format``
key at all), because "the field is present" would pass just as well against a
provider that always sends it and thereby forced JSON onto ordinary chat.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from provider import (
    OLLAMA_CAPABILITY,
    OllamaProvider,
    StructuredOutput,
    _factory,
    native_format,
    resolve_output_format,
)

from test_provider_impl import _FakeAsyncClient, fake_httpx  # noqa: F401  (fixture)

_DONE = '{"message":{"content":"{\\"a\\":1}"},"done":true,"prompt_eval_count":3,"eval_count":4}'


def _provider(fake: types.ModuleType, **options: Any) -> OllamaProvider:
    p = OllamaProvider(model="llama3.2:1b", extra_options=dict(options) or None)
    p._client.stream_lines = [_DONE]
    return p


async def _drain_stream(p: OllamaProvider) -> None:
    async for _ in p.stream("give me json"):
        pass


def _body(p: OllamaProvider) -> dict[str, Any]:
    assert p._client.stream_calls, "the provider never issued a request"
    return p._client.stream_calls[-1]["json"]


# ── the normalizer ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (dict, {"type": "object"}),
        (list, {"type": "array"}),
        ({"type": "object", "properties": {"a": {"type": "integer"}}},
         {"type": "object", "properties": {"a": {"type": "integer"}}}),
        ("json", "json"),
        ("JSON", "json"),
        (" json ", "json"),
    ],
)
def test_native_format_accepts_the_shapes_a_caller_actually_passes(requested, expected):
    assert native_format(requested) == expected


@pytest.mark.parametrize("requested", [None, "", "text", "yaml", {}, 7, object(), str, tuple])
def test_native_format_rejects_everything_it_cannot_express(requested):
    """``None`` means "send no constraint" — core's parse-with-targeted-retry then stays
    in charge, which is the correct fallback. Forwarding an unexpressible value instead
    would fail inside the JSON encoder as an opaque TypeError from the HTTP client."""
    assert native_format(requested) is None


def test_resolve_pops_so_nothing_reaches_the_wire_unnormalized():
    """The keys must be CONSUMED, not read. Every surviving ``extra_options`` key is
    setdefault-ed onto the request body, so a leftover ``output_type`` would put a bare
    Python type into the JSON encoder."""
    options: dict[str, object] = {"output_type": dict, "format": None, "temperature": 0.1}
    assert resolve_output_format(options) == {"type": "object"}
    assert options == {"temperature": 0.1}


def test_an_explicit_wire_format_wins_over_the_generic_request():
    """An operator who wrote a real schema out-ranks "some object" — an explicit
    wire-level value is a deliberate override, not a duplicate."""
    schema = {"type": "object", "required": ["verdict"]}
    options: dict[str, object] = {"format": schema, "output_type": list}
    assert resolve_output_format(options) == schema
    assert options == {}


# ── the request shape on the wire ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_sends_a_native_json_schema_format(fake_httpx):
    p = _provider(fake_httpx, output_type=dict)
    await _drain_stream(p)
    body = _body(p)
    # Key SET, not the whole dict: ``messages`` on the recorded body is the provider's
    # live ``_history`` list, which grows with the assistant turn after the request is
    # issued — asserting the aliased list would be asserting post-request state.
    assert set(body) == {"model", "messages", "stream", "format"}
    assert body["model"] == "llama3.2:1b"
    assert body["stream"] is True
    assert body["messages"][0] == {"role": "user", "content": "give me json"}
    assert body["format"] == {"type": "object"}


@pytest.mark.asyncio
async def test_stream_sends_no_format_key_when_nothing_was_requested(fake_httpx):
    """The vacuity floor for every assertion above: an ordinary chat turn must carry
    exactly the fields it carries today. A provider that always constrained output would
    pass the positive tests and silently force JSON onto every conversation."""
    p = _provider(fake_httpx)
    await _drain_stream(p)
    body = _body(p)
    assert "format" not in body
    assert set(body) == {"model", "messages", "stream"}


@pytest.mark.asyncio
async def test_a_caller_supplied_schema_reaches_the_wire_verbatim(fake_httpx):
    schema = {
        "type": "object",
        "properties": {"reasoning": {"type": "string"}, "verdict": {"type": "string"}},
        "required": ["reasoning", "verdict"],
    }
    p = _provider(fake_httpx, format=schema)
    await _drain_stream(p)
    assert _body(p)["format"] == schema


@pytest.mark.asyncio
async def test_json_mode_rides_the_same_field(fake_httpx):
    p = _provider(fake_httpx, output_type="json")
    await _drain_stream(p)
    assert _body(p)["format"] == "json"


@pytest.mark.asyncio
async def test_an_unexpressible_request_is_dropped_not_forwarded(fake_httpx):
    """The latent bug this normalizer closes: before it, a non-serializable value in
    ``extra_options`` was setdefault-ed straight onto the body and killed the turn
    inside the JSON encoder. Now the key is consumed and the turn goes out clean."""
    p = _provider(fake_httpx, output_type=str)
    await _drain_stream(p)
    body = _body(p)
    assert "format" not in body
    assert "output_type" not in body


@pytest.mark.asyncio
async def test_complete_sends_the_same_native_format(fake_httpx):
    """The native agent loop uses ``complete()``, not ``stream()`` — a constraint honored
    by only one of them is a constraint that silently vanishes on the tool-calling path."""
    p = _provider(fake_httpx, output_type=dict)
    async for _ in p.complete([{"role": "user", "content": "hi"}]):
        pass
    assert _body(p)["format"] == {"type": "object"}


# ── the build-kwarg seam (how a per-call contract enters) ────────────────────


def test_the_factory_forwards_a_requested_contract_as_a_build_kwarg(fake_httpx):
    """A contract is decided at the CALL while the provider is built per call, so the
    build kwarg is the only channel it can enter by — the same one ``model`` and
    ``embedding_model`` already use."""
    from personalclaw.llm import ProviderEntry

    entry = ProviderEntry(name="ollama", type="ollama", model="llama3.2:1b")
    p = _factory(entry=entry, output_type=dict)
    assert p._output_format == {"type": "object"}


def test_a_per_call_contract_outranks_a_standing_entry_option(fake_httpx):
    from personalclaw.llm import ProviderEntry

    entry = ProviderEntry(
        name="ollama", type="ollama", model="llama3.2:1b", options={"format": "json"}
    )
    p = _factory(entry=entry, output_type=dict)
    assert p._output_format == {"type": "object"}


def test_an_entry_option_alone_still_applies(fake_httpx):
    from personalclaw.llm import ProviderEntry

    entry = ProviderEntry(
        name="ollama", type="ollama", model="llama3.2:1b", options={"format": "json"}
    )
    assert _factory(entry=entry)._output_format == "json"


# ── the declaration, and core's reader of it ─────────────────────────────────


def test_the_capability_declares_the_top_grade_using_cores_own_enum():
    """Not a stringly-typed stand-in: core compares the grade by identity against its
    own enum, so a look-alike would read as "declared" while dispatching as NONE."""
    from personalclaw.llm.capabilities import StructuredOutput as CoreGrade

    assert StructuredOutput is CoreGrade
    assert OLLAMA_CAPABILITY.structured_output is CoreGrade.JSON_SCHEMA
    assert OLLAMA_CAPABILITY.structured_output != CoreGrade.NONE


def test_cores_dispatch_hook_lights_up_on_this_declaration(monkeypatch):
    """Drive core's OWN reader of the graded descriptor with ollama as the only
    registered type. Without this, the declaration would be a field nothing consumes."""
    from personalclaw.llm import registry as registry_mod
    from personalclaw.llm.registry import ProviderRegistry
    from personalclaw.workflows import grounding

    only_ollama = ProviderRegistry()
    only_ollama.register_type(OLLAMA_CAPABILITY, lambda **kw: None)
    monkeypatch.setattr(registry_mod, "get_default_registry", lambda: only_ollama)

    bundle = grounding.GroundingBundle()
    assert bundle.structured_output is False  # the default core ships
    grounding._add_model_capabilities(bundle)

    assert bundle.structured_output is True
    assert "ollama=json_schema" in " ".join(bundle.model_notes)


def test_the_flipped_flag_changes_what_core_asks_the_model_for():
    """The hook's downstream effect, so this isn't a flag flipped into the void: with
    native enforcement declared, core stops appending the "return bare JSON, no fence"
    plea (the prompt-shaped workaround) and emits a real schema instead."""
    from personalclaw.workflows import generation, grounding

    plea = "Return ONE JSON object and nothing else"

    unconstrained = grounding.GroundingBundle(structured_output=False)
    constrained = grounding.GroundingBundle(structured_output=True)

    assert plea in generation.planning_prompt("goal", bundle=unconstrained)
    assert plea not in generation.planning_prompt("goal", bundle=constrained)
    # The schema core hands the constrained path is a real one (a oneOf over "a spec"
    # and the typed refusal), i.e. something a `format` field can actually enforce.
    schema = generation.spec_json_schema()
    assert [branch["type"] for branch in schema["oneOf"]] == ["object", "object"]
