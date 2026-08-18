"""Unit tests for BedrockProvider.

Cover the ModelProvider axis for Amazon Bedrock, including lazy SDK import.
``boto3`` is NOT installed in this dev environment; these tests substitute a
stub module into ``sys.modules`` before constructing the provider so the lazy
import inside ``start()`` resolves to the stub.

Key properties asserted:
- ``stream()`` emits EVENT_TEXT_CHUNKs then a terminal EVENT_COMPLETE carrying
  ``input_tokens``/``output_tokens`` from ``metadata.usage`` and a computed
  ``context_usage_pct``.
- The provider constructs and runs with NO credential (uses boto3's default
  credential chain).
- ``start()`` honors region + optional profile.
- The sync boto stream is driven off the event loop (a slow stream still yields
  incrementally, not all-at-once after a block).
"""

import sys
import types
from typing import Any

import pytest

from personalclaw.llm.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK


# ── Fake boto3 ──────────────────────────────────────────────────────────


class _FakeBedrockClient:
    """Stand-in for the ``bedrock-runtime`` client.

    Records the last ``converse_stream`` request and returns a pre-canned
    event list under the ``stream`` key (the real EventStream is a sync
    iterable of dict events).
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.last_request: dict[str, Any] | None = None

    def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        self.last_request = kwargs
        return {"stream": list(self._events)}


class _FakeSession:
    """Stand-in for ``boto3.Session`` — records region/profile."""

    last_profile: str | None = "UNSET"

    def __init__(self, profile_name: str | None = None) -> None:
        type(self).last_profile = profile_name
        self._profile = profile_name

    def client(self, service: str, region_name: str | None = None, config=None) -> _FakeBedrockClient:
        assert service == "bedrock-runtime"
        type(self).last_region = region_name
        type(self).last_config = config
        return type(self).next_client


def _install_fake_boto3(monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]) -> _FakeBedrockClient:
    """Inject a fake ``boto3`` module whose Session yields a client over *events*."""
    client = _FakeBedrockClient(events)
    _FakeSession.next_client = client  # type: ignore[attr-defined]
    _FakeSession.last_region = None  # type: ignore[attr-defined]
    _FakeSession.last_profile = "UNSET"  # type: ignore[attr-defined]
    fake = types.ModuleType("boto3")
    fake.Session = _FakeSession  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    return client


def _text_event(text: str) -> dict[str, Any]:
    return {"contentBlockDelta": {"delta": {"text": text}}}


def _usage_event(in_tok: int, out_tok: int) -> dict[str, Any]:
    return {"metadata": {"usage": {"inputTokens": in_tok, "outputTokens": out_tok}}}


# ── Tests ────────────────────────────────────────────────────────────────


def test_module_imports_without_boto3_and_registers_type() -> None:
    """Importing the app's provider module is SDK-free and registers the ``bedrock``
    type (the app loader imports provider.py, which triggers register_type)."""
    import provider  # noqa: F401  (app-local; registers on import)
    from personalclaw.sdk.model import get_default_registry

    assert get_default_registry().capability_of("bedrock").type == "bedrock"
    # boto3 must NOT be pulled in merely by importing the provider module.
    assert "boto3" not in sys.modules


@pytest.mark.asyncio
async def test_stream_emits_text_then_complete_with_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from provider import BedrockProvider

    events = [
        _text_event("Hello"),
        _text_event(", world"),
        _usage_event(120, 8),
    ]
    _install_fake_boto3(monkeypatch, events)

    # No credential argument — construction must succeed.
    provider = BedrockProvider(model="anthropic.claude-sonnet-4-20250514-v1:0", region="us-west-2")
    await provider.start()

    seen = [ev async for ev in provider.stream("hi")]

    text_chunks = [e for e in seen if e.kind == EVENT_TEXT_CHUNK]
    completes = [e for e in seen if e.kind == EVENT_COMPLETE]
    assert [e.text for e in text_chunks] == ["Hello", ", world"]
    assert len(completes) == 1
    done = completes[0]
    assert done.input_tokens == 120
    assert done.output_tokens == 8
    # 120 / 200000 * 100 = 0.06
    assert done.context_usage_pct == pytest.approx(0.06)
    assert provider.context_usage_pct() == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_start_honors_region_and_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from provider import BedrockProvider

    _install_fake_boto3(monkeypatch, [])
    provider = BedrockProvider(model="m", region="eu-central-1", profile_name="prod")
    await provider.start()
    assert _FakeSession.last_region == "eu-central-1"  # type: ignore[attr-defined]
    assert _FakeSession.last_profile == "prod"


@pytest.mark.asyncio
async def test_default_chain_when_no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from provider import BedrockProvider

    _install_fake_boto3(monkeypatch, [])
    provider = BedrockProvider(model="m")  # no profile → default chain
    await provider.start()
    # A no-profile provider must construct Session() without profile_name.
    assert _FakeSession.last_profile is None


def test_no_hardcoded_default_model_constant() -> None:
    """De-hardcode directive (2026-07-06): there is NO hardcoded default model id.
    The old ``DEFAULT_BEDROCK_MODEL`` constant (and the #32 fix's hardcoded value) are
    gone — the unpinned default is resolved from live discovery at start()."""
    import provider as prov
    assert not hasattr(prov, "DEFAULT_BEDROCK_MODEL"), "no hardcoded default id may exist"
    assert not hasattr(prov, "_BEDROCK_FALLBACK_MODELS"), "no hardcoded fallback catalog may exist"


@pytest.mark.asyncio
async def test_default_resolves_from_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unpinned provider resolves its model from the DISCOVERED list (preference
    order: sonnet → haiku → any claude → nova → first), never a baked id."""
    import provider as prov

    async def fake_resolve(region, profile):
        return "us.anthropic.claude-sonnet-4-6"  # stands in for discovery
    monkeypatch.setattr(prov, "_resolve_default_model_id", fake_resolve)
    _install_fake_boto3(monkeypatch, [])
    p = prov.BedrockProvider(model="")  # unpinned
    assert p._model_id == ""            # nothing baked at construction
    await p.start()
    assert p._model_id == "us.anthropic.claude-sonnet-4-6"  # resolved from discovery


def test_pick_default_prefers_claude_sonnet() -> None:
    """_DEFAULT_MODEL_PREFERENCE picks a sonnet-tier Claude first, by substring —
    no exact id hardcoded."""
    import asyncio

    import provider as prov

    rows = [
        {"id": "amazon.nova-lite-v1:0"},
        {"id": "global.anthropic.claude-opus-4-8"},
        {"id": "global.anthropic.claude-sonnet-4-6"},
    ]
    import unittest.mock as m
    with m.patch.object(prov, "_list_bedrock_models_sync", return_value=rows):
        picked = asyncio.run(prov._resolve_default_model_id("us-west-2", ""))
    assert picked == "global.anthropic.claude-sonnet-4-6"  # sonnet beats opus/nova per preference


@pytest.mark.asyncio
async def test_system_prompt_and_history_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    from provider import BedrockProvider

    client = _install_fake_boto3(monkeypatch, [_text_event("ok"), _usage_event(10, 2)])
    provider = BedrockProvider(model="m", system_prompt="Be terse.")
    await provider.start()
    _ = [ev async for ev in provider.stream("first")]

    req = client.last_request
    assert req is not None
    assert req["modelId"] == "m"
    assert req["system"] == [{"text": "Be terse."}]
    # User turn recorded in Converse content shape. (messages aliases the
    # provider's history list, which also gains the assistant turn after the
    # stream completes — so assert the user turn is present, not that it's last.)
    assert {"role": "user", "content": [{"text": "first"}]} in req["messages"]
    # And the assistant reply was appended to history for the next turn.
    assert {"role": "assistant", "content": [{"text": "ok"}]} in provider._history


def test_bedrock_declares_vision_capability() -> None:
    """Bedrock serves vision models (Nova, Claude, Gemma-VL, Qwen-VL) — its capability
    descriptor MUST advertise VISION, or resolve_provider_for_use_case can't build a
    provider for a bound image_modality model and vision/ocr nodes silently produce
    nothing (the video-extraction bug)."""
    from provider import BEDROCK_CAPABILITY
    from personalclaw.llm.capabilities import Capability

    assert Capability.VISION in BEDROCK_CAPABILITY.capabilities
    assert BEDROCK_CAPABILITY.supports_vision is True


def test_translate_messages_converts_image_blocks() -> None:
    """The multimodal content-block shape the knowledge vision nodes emit
    ({type:text} + {type:image_url, image_url:{url:data-url}}) must become Converse
    content blocks (text + {image:{format,source:{bytes}}}) — else a stringified list
    reaches the model as garbage and vision returns nothing."""
    import base64

    from provider import _translate_messages

    raw = b"\xff\xd8\xff\xe0jpegbytes"
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}]
    _system, out = _translate_messages(messages)
    assert len(out) == 1
    blocks = out[0]["content"]
    assert {"text": "describe"} in blocks
    img = [b for b in blocks if "image" in b]
    assert len(img) == 1
    assert img[0]["image"]["format"] == "jpeg"
    assert img[0]["image"]["source"]["bytes"] == raw  # decoded, not the data-url string


def test_translate_messages_plain_string_unchanged() -> None:
    """A plain string content (the common case) still becomes one text block."""
    from provider import _translate_messages

    _system, out = _translate_messages([{"role": "user", "content": "hello"}])
    assert out == [{"role": "user", "content": [{"text": "hello"}]}]


def test_data_url_to_converse_image_normalises_and_rejects() -> None:
    from provider import _data_url_to_converse_image

    # jpg → jpeg
    import base64
    b = base64.b64encode(b"x").decode()
    assert _data_url_to_converse_image(f"data:image/jpg;base64,{b}")["image"]["format"] == "jpeg"
    # a non-data URL is rejected (None), never a corrupt block
    assert _data_url_to_converse_image("https://example.com/x.png") is None
    assert _data_url_to_converse_image("") is None


@pytest.mark.asyncio
async def test_factory_requires_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry factory builds a Bedrock provider with no credential_store."""
    from provider import _factory
    from personalclaw.llm.registry import ProviderEntry

    _install_fake_boto3(monkeypatch, [])
    entry = ProviderEntry(
        name="bedrock",
        type="bedrock",
        model="anthropic.claude-sonnet-4-20250514-v1:0",
        options={"region": "us-west-2", "profile": "dev"},
    )
    # No credential_store kwarg — must not raise CredentialMissing.
    provider = _factory(entry=entry)
    await provider.start()
    assert _FakeSession.last_region == "us-west-2"  # type: ignore[attr-defined]
    assert _FakeSession.last_profile == "dev"


@pytest.mark.asyncio
async def test_stream_always_sends_max_tokens_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured provider STILL sends ``inferenceConfig.maxTokens``.

    Omitting it lets Converse apply its low per-model default output cap, which
    truncates large streamed tool-call JSON (e.g. write_file with a big content)
    — the args then parse to ``{}`` and the tool reports a missing argument.
    """
    from provider import _DEFAULT_MAX_TOKENS, BedrockProvider

    client = _install_fake_boto3(monkeypatch, [_text_event("ok"), _usage_event(10, 2)])
    provider = BedrockProvider(model="m")  # no max_tokens configured
    await provider.start()
    _ = [ev async for ev in provider.stream("hi")]

    req = client.last_request
    assert req is not None
    assert req["inferenceConfig"]["maxTokens"] == _DEFAULT_MAX_TOKENS


@pytest.mark.asyncio
async def test_complete_sends_max_tokens_and_accumulates_large_tool_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """complete() (native-loop path) sends the cap AND accumulates a tool call's
    streamed input JSON across many fragments into one complete EVENT_TOOL_CALL."""
    from personalclaw.llm.base import EVENT_TOOL_CALL
    from provider import _DEFAULT_MAX_TOKENS, BedrockProvider

    # A write_file call whose `content` arrives as many input-JSON fragments —
    # the exact shape that truncates under a low default cap.
    big = "x" * 5000
    full_args = f'{{"path": "PLAN.md", "content": "{big}"}}'
    frags = [full_args[i : i + 64] for i in range(0, len(full_args), 64)]
    events: list[dict[str, Any]] = [
        {"contentBlockStart": {"contentBlockIndex": 0,
                               "start": {"toolUse": {"toolUseId": "tu1", "name": "write_file"}}}},
    ]
    events += [{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": f}}}}
               for f in frags]
    events += [
        {"contentBlockStop": {"contentBlockIndex": 0}},
        _usage_event(10, 2),
    ]
    client = _install_fake_boto3(monkeypatch, events)
    provider = BedrockProvider(model="m")
    await provider.start()

    seen = [ev async for ev in provider.complete([{"role": "user", "content": "build"}],
                                                  tools=[{"name": "write_file",
                                                          "description": "w",
                                                          "parameters": {"type": "object"}}])]

    assert client.last_request["inferenceConfig"]["maxTokens"] == _DEFAULT_MAX_TOKENS
    calls = [e for e in seen if e.kind == EVENT_TOOL_CALL]
    assert len(calls) == 1
    assert calls[0].tool_input == full_args  # reassembled intact, not truncated


# ── Friendly error mapping (data-retention policy restriction) ──

def test_friendly_bedrock_error_maps_data_retention():
    """A data-retention ValidationException becomes an actionable message naming
    the model (no per-request fix exists; the user must pick another model)."""
    from provider import _friendly_bedrock_error
    from personalclaw.llm.registry import ProviderResolutionError

    raw = Exception(
        "An error occurred (ValidationException) when calling the ConverseStream "
        "operation: The model returned the following errors: data retention mode "
        "'default' is not available for this model"
    )
    mapped = _friendly_bedrock_error(raw, "global.anthropic.claude-fable-5")
    assert isinstance(mapped, ProviderResolutionError)
    assert "global.anthropic.claude-fable-5" in str(mapped)
    assert "data-retention" in str(mapped) or "data retention" in str(mapped)


def test_friendly_bedrock_error_passes_through_other_errors():
    from provider import _friendly_bedrock_error

    raw = ValueError("some other failure")
    assert _friendly_bedrock_error(raw, "amazon.nova-pro-v1:0") is raw


# ── Provider-qualified model id stripping (active_models.json ref form) ──

def test_bare_model_id_strips_provider_prefix():
    """A chat session stores its model as "Provider:bare-id"; the SDK must get
    the bare id, or AWS replies "model identifier is invalid"."""
    from provider import _bare_model_id
    assert _bare_model_id("Bedrock:global.anthropic.claude-opus-4-8", "fb") == "global.anthropic.claude-opus-4-8"
    assert _bare_model_id("Bedrock:us.anthropic.claude-3-7-sonnet-20250219-v1:0", "fb") == "us.anthropic.claude-3-7-sonnet-20250219-v1:0"


def test_bare_model_id_preserves_real_ids_and_versions():
    """Never mangle a real id whose first colon is the version suffix, or a
    bare/unprefixed id."""
    from provider import _bare_model_id
    # version colon (no dotted vendor namespace before it) is preserved
    assert _bare_model_id("anthropic.claude-sonnet-4-20250514-v1:0", "fb") == "anthropic.claude-sonnet-4-20250514-v1:0"
    assert _bare_model_id("global.anthropic.claude-opus-4-8", "fb") == "global.anthropic.claude-opus-4-8"


def test_bare_model_id_empty_uses_fallback():
    from provider import _bare_model_id
    assert _bare_model_id("", "the-fallback") == "the-fallback"
    assert _bare_model_id(None, "the-fallback") == "the-fallback"


# ── MCP tool-name sanitization for Converse (#69) ──

def test_tool_names_sanitized_for_converse_and_reverse_mapped():
    """Bedrock's toolSpec.name must match [a-zA-Z0-9_-]+; MCP tools are
    slash-namespaced (mcp/GitHub/X). Names are sanitized in BOTH the toolConfig
    and historical toolUse blocks (they must agree), and the name Bedrock returns
    reverse-maps to the real tool id so the loop dispatches the right tool."""
    import re as _re
    from provider import (
        _build_tool_name_maps,
        _translate_messages,
        _translate_tools,
    )

    tools = [
        {"function": {"name": "mcp/search/fetch_page", "description": "d", "parameters": {}}},
        {"function": {"name": "mcp/search/delegate", "description": "d", "parameters": {}}},
        {"function": {"name": "read_file", "description": "d", "parameters": {}}},
    ]
    fwd, rev = _build_tool_name_maps(tools)

    cfg = _translate_tools(tools, fwd)
    names = [t["toolSpec"]["name"] for t in cfg["tools"]]
    for n in names:
        assert _re.fullmatch(r"[a-zA-Z0-9_-]+", n), f"illegal Bedrock tool name: {n!r}"
    # round-trip: every safe name reverses to a real name
    assert rev[fwd["mcp/search/fetch_page"]] == "mcp/search/fetch_page"
    assert fwd["read_file"] == "read_file"  # already-legal name untouched

    # historical toolUse uses the SAME safe name as the config (Bedrock rejects
    # a toolUse whose name isn't in the toolConfig)
    _sys, msgs = _translate_messages(
        [{"role": "assistant", "tool_calls": [
            {"id": "t1", "function": {"name": "mcp/search/fetch_page", "arguments": "{}"}}]}],
        fwd,
    )
    hist_names = [b["toolUse"]["name"] for m in msgs for b in m["content"] if "toolUse" in b]
    assert hist_names == [fwd["mcp/search/fetch_page"]]


def test_tool_name_sanitize_disambiguates_collisions():
    """Two distinct names that sanitize to the same string stay distinct."""
    from provider import _build_tool_name_maps
    fwd, rev = _build_tool_name_maps([
        {"function": {"name": "mcp/a/x"}},
        {"function": {"name": "mcp:a:x"}},  # also → mcp_a_x
    ])
    assert fwd["mcp/a/x"] != fwd["mcp:a:x"], "collision not disambiguated"
    assert len(rev) == 2  # both reversible


def test_orphan_tooluse_gets_synthetic_result():
    """An assistant toolUse with no following tool result (turn cancelled
    mid-inference) is paired with a synthetic toolResult so Converse accepts the
    replayed history instead of raising 'Expected toolResult blocks at …'."""
    from provider import _translate_messages

    # Goal-loop history shape: a cancelled turn left a dangling tool call, then
    # the next cycle's nudge appended a fresh user message.
    messages = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "do the thing"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tooluse_9ddQqs", "type": "function",
                 "function": {"name": "fs_read", "arguments": "{}"}}
            ],
        },
        {"role": "user", "content": "next cycle nudge"},  # orphaned the toolUse
    ]
    _sys, msgs = _translate_messages(messages)

    # The toolUse turn must be immediately followed by a user turn whose first
    # block is the matching toolResult.
    tooluse_idx = next(
        i for i, m in enumerate(msgs)
        if any("toolUse" in b for b in m["content"])
    )
    nxt = msgs[tooluse_idx + 1]
    assert nxt["role"] == "user"
    result_ids = [
        b["toolResult"]["toolUseId"] for b in nxt["content"] if "toolResult" in b
    ]
    assert "tooluse_9ddQqs" in result_ids
    # The original nudge text survives as its own later turn.
    assert any(
        any(b.get("text") == "next cycle nudge" for b in m["content"])
        for m in msgs
    )


def test_orphan_toolresult_is_dropped():
    """A toolResult that answers no preceding toolUse is dropped (Converse
    rejects an unmatched toolResult too)."""
    from provider import _translate_messages

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "ghost", "content": "stale result"},
        {"role": "assistant", "content": "ok"},
    ]
    _sys, msgs = _translate_messages(messages)
    for m in msgs:
        for b in m.get("content", []):
            assert "toolResult" not in b, "orphan toolResult should be dropped"


def test_wellformed_tool_pair_unchanged():
    """A properly paired toolUse/toolResult history is left intact (no spurious
    synthetic results)."""
    from provider import _translate_messages

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "fs_read", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "file contents"},
        {"role": "assistant", "content": "done"},
    ]
    _sys, msgs = _translate_messages(messages)
    results = [
        b["toolResult"] for m in msgs for b in m["content"] if "toolResult" in b
    ]
    assert len(results) == 1
    assert results[0]["toolUseId"] == "t1"
    assert results[0]["content"][0]["text"] == "file contents"


def test_translate_messages_never_emits_empty_text_blocks() -> None:
    """Converse rejects the whole request when ANY text block is "" —
    ("text content blocks must be non-empty"). Empty texts occur legitimately
    (a tool that printed nothing, an empty user/assistant turn), so every
    emission site must map "" to a non-empty placeholder."""
    from provider import _translate_messages

    messages = [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": None},
        {"role": "user", "content": [{"type": "text", "text": ""}]},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "fs_read", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": ""},
    ]
    _sys, msgs = _translate_messages(messages)
    for m in msgs:
        for b in m["content"]:
            if "text" in b:
                assert b["text"].strip(), f"empty text block leaked: {m}"
            if "toolResult" in b:
                for tb in b["toolResult"]["content"]:
                    if "text" in tb:
                        assert tb["text"].strip(), f"empty toolResult text leaked: {m}"


# ── Prompt caching: the Converse cachePoint translation (PCS-8) ───────────────
#
# Core places a NEUTRAL `_cache_hint` marker on exactly one message and never learns
# Converse's syntax; this app turns that marker into a `cachePoint` content block. These
# tests pin the translation at the payload level, which is the substitute for the live
# multi-turn Bedrock drive the atom's validation clause asks for (see the plan's
# execution log: no usable Bedrock credentials in this environment).

_CACHE_POINT = {"cachePoint": {"type": "default"}}


def _blocks_with_cache_point(payload: object) -> int:
    """How many cachePoint blocks appear anywhere inside ``payload``."""
    if isinstance(payload, dict):
        return sum(
            (1 if key == "cachePoint" else 0) + _blocks_with_cache_point(value)
            for key, value in payload.items()
        )
    if isinstance(payload, list):
        return sum(_blocks_with_cache_point(item) for item in payload)
    return 0


def test_unhinted_messages_emit_no_cache_point() -> None:
    """The byte-identity floor: with no marker present, the translation is exactly what
    it was before PCS-8 — an undeclared turn must not silently start paying for writes."""
    from provider import _translate_messages

    system, out = _translate_messages(
        [
            {"role": "system", "content": "stable head"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
    )
    assert system == [{"text": "stable head"}]
    assert out == [
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi"}]},
    ]
    assert _blocks_with_cache_point(system) + _blocks_with_cache_point(out) == 0


def test_hinted_message_gets_a_cache_point_and_only_that_message() -> None:
    """The checkpoint follows the hinted message's blocks — and nothing else's."""
    from personalclaw.sdk.model import CACHE_HINT_KEY
    from provider import _translate_messages

    _system, out = _translate_messages(
        [
            {"role": "user", "content": "first", CACHE_HINT_KEY: {"generation": 0}},
            {"role": "assistant", "content": "second"},
        ]
    )
    assert out[0]["content"] == [{"text": "first"}, _CACHE_POINT]
    assert out[1]["content"] == [{"text": "second"}]
    assert _blocks_with_cache_point(out) == 1


def test_the_checkpoint_is_last_not_first() -> None:
    """Everything BEFORE a cachePoint is cached, so a checkpoint that led the block list
    would cache nothing at all."""
    from personalclaw.sdk.model import CACHE_HINT_KEY
    from provider import _translate_messages

    _system, out = _translate_messages(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                CACHE_HINT_KEY: {"generation": 3},
            }
        ]
    )
    blocks = out[0]["content"]
    assert blocks[-1] == _CACHE_POINT
    assert "cachePoint" not in blocks[0]
    assert blocks[:-1] == [{"text": "a"}, {"text": "b"}]


def test_hinted_system_message_puts_the_checkpoint_at_the_end_of_system() -> None:
    """Converse serves ``system`` ahead of ``messages[0]``, so a checkpoint there caches
    the whole stable head."""
    from personalclaw.sdk.model import CACHE_HINT_KEY
    from provider import _translate_messages

    system, out = _translate_messages(
        [
            {"role": "system", "content": "stable head", CACHE_HINT_KEY: {"generation": 0}},
            {"role": "user", "content": "go"},
        ]
    )
    assert system == [{"text": "stable head"}, _CACHE_POINT]
    assert _blocks_with_cache_point(out) == 0


def test_hinted_assistant_tool_calls_message_marks_its_last_block() -> None:
    from personalclaw.sdk.model import CACHE_HINT_KEY
    from provider import _translate_messages

    _system, out = _translate_messages(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "calling",
                "tool_calls": [{"id": "t1", "function": {"name": "ls", "arguments": "{}"}}],
                CACHE_HINT_KEY: {"generation": 0},
            },
            {"role": "tool", "tool_call_id": "t1", "content": "out"},
        ]
    )
    assistant = next(m for m in out if m["role"] == "assistant")
    assert assistant["content"][-1] == _CACHE_POINT
    assert "toolUse" in assistant["content"][-2]


@pytest.mark.parametrize("content", ["", None])
def test_a_hint_on_an_empty_span_is_a_no_op(content) -> None:
    """No block to follow ⇒ no checkpoint. Marking an empty system span would charge a
    cache write for nothing."""
    from personalclaw.sdk.model import CACHE_HINT_KEY
    from provider import _translate_messages

    system, _out = _translate_messages(
        [{"role": "system", "content": content, CACHE_HINT_KEY: {"generation": 0}}]
    )
    assert system == []


def test_a_hint_on_a_tool_result_is_a_no_op_and_keeps_the_group_contiguous() -> None:
    """Core never hints a tool result. If one ever arrived, degrading to "no checkpoint"
    costs a cache hit; splitting the merged toolResult turn would cost the request."""
    from personalclaw.sdk.model import CACHE_HINT_KEY
    from provider import _translate_messages

    _system, out = _translate_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "t1", "function": {"name": "ls", "arguments": "{}"}},
                    {"id": "t2", "function": {"name": "cat", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "a", CACHE_HINT_KEY: {"generation": 0}},
            {"role": "tool", "tool_call_id": "t2", "content": "b"},
        ]
    )
    results = [m for m in out if m["role"] == "user"]
    assert len(results) == 1, "the two tool results must stay in ONE contiguous user turn"
    assert all("toolResult" in b for b in results[0]["content"])
    assert _blocks_with_cache_point(out) == 0


def test_the_volatile_turn_note_is_relocated_to_the_tail_not_hoisted_into_system() -> None:
    """Converse serves ``system`` first, so a note whose content changes every turn must
    not sit in the cacheable head — otherwise the prefix differs every turn and no
    checkpoint is ever read. The note moves POSITION, never existence."""
    from provider import _translate_messages

    system, out = _translate_messages(
        [
            {"role": "system", "content": "stable head"},
            {"role": "user", "content": "go"},
            {"role": "system", "content": "turn 7 note", "_volatile": True},
        ]
    )
    assert system == [{"text": "stable head"}], "the volatile note must NOT be in system"
    assert out[-1] == {"role": "user", "content": [{"text": "turn 7 note"}]}
    # Still reaches the model exactly once — relocated, not dropped.
    assert sum(1 for m in out for b in m["content"] if b.get("text") == "turn 7 note") == 1


def test_multiple_volatile_notes_ship_once_each_in_order() -> None:
    from provider import _translate_messages

    _system, out = _translate_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "system", "content": "note A", "_volatile": True},
            {"role": "system", "content": "note B", "_volatile": True},
        ]
    )
    assert [b["text"] for m in out[-2:] for b in m["content"]] == ["note A", "note B"]


# ── Posture declaration + cache-usage reporting ──────────────────────────────


def test_bedrock_declares_explicit_on_both_the_capability_and_the_instance() -> None:
    """The declarative twin and the instance the factory returns must agree: a capability
    promising cache reads while the provider places no marker is worse than NONE."""
    from personalclaw.sdk.model import PromptCache
    from provider import BEDROCK_CAPABILITY, BedrockProvider

    assert BEDROCK_CAPABILITY.prompt_cache is PromptCache.EXPLICIT
    assert BedrockProvider.prompt_cache is PromptCache.EXPLICIT
    assert BedrockProvider(model="m").prompt_cache is PromptCache.EXPLICIT


def test_read_cache_usage_maps_converse_fields_and_tolerates_junk() -> None:
    from provider import _read_cache_usage

    assert _read_cache_usage(
        {"inputTokens": 9, "cacheReadInputTokens": 1234, "cacheWriteInputTokens": 56}
    ) == (56, 1234)
    assert _read_cache_usage({}) == (0, 0)  # an uncached turn omits the keys
    assert _read_cache_usage({"cacheReadInputTokens": None}) == (0, 0)
    assert _read_cache_usage({"cacheReadInputTokens": "nope"}) == (0, 0)
    assert _read_cache_usage({"cacheReadInputTokens": -5}) == (0, 0)


@pytest.mark.asyncio
async def test_complete_sends_the_cache_point_on_the_wire_and_reports_cache_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end at the payload level: a marked multi-turn history reaches Converse
    carrying exactly one ``cachePoint``, and the terminal event reports the cache read
    Converse returned. This is what a live Bedrock-Anthropic drive would show, asserted
    against the request the provider actually built."""
    from personalclaw.sdk.model import CACHE_HINT_KEY
    from provider import BedrockProvider

    events: list[dict[str, Any]] = [
        _text_event("second turn"),
        {
            "metadata": {
                "usage": {
                    "inputTokens": 12,
                    "outputTokens": 3,
                    "cacheReadInputTokens": 4096,
                    "cacheWriteInputTokens": 0,
                }
            }
        },
    ]
    client = _install_fake_boto3(monkeypatch, events)
    provider = BedrockProvider(model="anthropic.claude-sonnet-4-v1:0")
    await provider.start()

    # A second turn: stable head + a completed first exchange, marker on the trailing
    # stable message, plus the per-turn volatile note core appends every turn.
    messages = [
        {"role": "system", "content": "stable assembled context"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question", CACHE_HINT_KEY: {"generation": 0}},
        {"role": "system", "content": "turn 2 note", "_volatile": True},
    ]
    seen = [ev async for ev in provider.complete(messages)]

    request = client.last_request
    assert request is not None
    assert _blocks_with_cache_point(request) == 1, "exactly one checkpoint per request"
    assert request["messages"][-2]["content"][-1] == _CACHE_POINT
    # The volatile note rides at the tail, AFTER the checkpoint, so it is outside the
    # cached span — and the system head stayed stable.
    assert request["system"] == [{"text": "stable assembled context"}]
    assert request["messages"][-1] == {"role": "user", "content": [{"text": "turn 2 note"}]}

    done = [e for e in seen if e.kind == EVENT_COMPLETE]
    assert len(done) == 1
    assert done[0].cache_read_tokens == 4096
    assert done[0].cache_creation_tokens == 0
