"""Amazon Bedrock provider — Converse streaming via ``boto3``.

A ModelProvider (stateless inference) sibling of :mod:`personalclaw.llm.openai`,
backed by the ``bedrock-runtime`` Converse API. ``boto3`` is imported lazily
inside :meth:`BedrockProvider.start` (Property 11 / Provider SDK Lazy Import) so
this module is safe to import without ``boto3`` installed; only starting a
provider triggers the SDK import.

Authentication is delegated entirely to boto3's standard AWS credential chain
(environment, ``~/.aws`` profiles, SSO cache, container/instance metadata) and
SigV4 signing happens inside botocore. PersonalClaw's ``CredentialStore`` is NOT
used — Bedrock takes only a region, a model, and an optional AWS profile name;
no AWS secret is ever read into or persisted by PersonalClaw.

``converse_stream`` is synchronous and returns a blocking ``EventStream``. To
avoid stalling the aiohttp event loop, the blocking call + iteration run in a
worker thread (:func:`asyncio.to_thread`) that pushes deltas onto an
:class:`asyncio.Queue` the async generator drains — token streaming stays
non-blocking under a multi-session gateway.
"""

import asyncio
import base64
import binascii
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from personalclaw.sdk.model import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    CancelOutcome,
    LLMEvent,
    ModelProvider,
)
from personalclaw.sdk.model import Capability, ProviderCapability
from personalclaw.sdk.model import (
    ConnectionResult,
    ModelCatalog,
    ModelInfo,
    ProviderEntry,
    ProviderResolutionError,
    get_default_registry,
)

logger = logging.getLogger(__name__)

# NO hardcoded model id (user directive 2026-07-06): Bedrock supports dynamic
# discovery (control plane list_foundation_models + list_inference_profiles), so the
# unpinned default is RESOLVED FROM LIVE DISCOVERY at start() — never a baked id
# (the old bug #32 was a hardcoded default that this account rejected). See
# _pick_default_model_id. When a model IS pinned (the common case — the chat binding
# supplies e.g. Bedrock:global.anthropic.claude-opus-4-8), no default is needed.
DEFAULT_REGION = "us-west-2"

# Preference order for auto-picking an unpinned default from the discovered list:
# a mid-tier Claude (sonnet) first, then any Claude, then any Nova, then anything.
# Substring match against discovered ids — no exact id is hardcoded.
_DEFAULT_MODEL_PREFERENCE = ("claude-sonnet", "claude-haiku", "claude", "nova")

# Max conversation history entries before trimming oldest (mirrors openai.py).
_MAX_HISTORY = 50

# Fallback context window when the model is absent from ``model_tokens.json``.
# Claude-on-Bedrock is 200k; Nova is 300k. Pick the conservative Claude value
# so the percentage estimate skews high rather than hiding usage.
_DEFAULT_CONTEXT_WINDOW = 200_000

# Default output cap when none is configured. Bedrock Converse applies a LOW
# per-model default (historically 512 for Anthropic) when ``maxTokens`` is
# omitted — that silently TRUNCATES the streamed tool-call JSON of any large
# tool argument (e.g. a write_file with a big ``content``), so the accumulated
# arguments are incomplete JSON and parse to ``{}`` ("missing required
# argument"). The sibling Anthropic provider always sends a default (4096), so
# mirror that here: a generous cap large enough for substantial file writes.
_DEFAULT_MAX_TOKENS = 8192

# Sentinel pushed onto the bridge queue when the worker thread finishes.
_STREAM_DONE = object()

# Streaming-read timeout (seconds). botocore's default is 60s applied PER socket
# read — and during a ``converse_stream`` that fires on the GAP BETWEEN streamed
# events, not just time-to-first-byte. Reasoning-class models (Opus) routinely go
# quiet far longer than 60s mid-turn (extended internal reasoning, or a slow tool
# round-trip the model is waiting on) while the turn is perfectly healthy, so the
# default silently kills long-but-live turns with a bare ``Read timed out``. Give
# the stream generous headroom; the supervisor/watchdog owns true stall recovery.
# (Repro: a heavy SPEC-writing loop turn streamed 66 chunks then died at ~82s on
# an inter-chunk gap; short turns never tripped it.) connect stays tight.
_STREAM_READ_TIMEOUT = 600
_CONNECT_TIMEOUT = 15


def _bare_model_id(model: str | None, fallback: str) -> str:
    """Return a clean AWS Bedrock model id from a possibly provider-qualified ref.

    Upstream callers may hand the model as the ``active_models.json`` ref form
    (``"Bedrock:global.anthropic.claude-opus-4-8"`` — provider name + ':' +
    bare id). Bedrock model ids themselves contain colons (``…-v1:0``), so only
    strip a SINGLE leading segment, and only when what follows still looks like
    a Bedrock id (contains a vendor '.' namespace, e.g. ``anthropic.``/
    ``amazon.``/``global.``/``us.``). Defense-in-depth at the AWS boundary so no
    upstream path can send an invalid identifier. Empty → fallback.
    """
    mid = (model or "").strip() or fallback
    if ":" in mid:
        head, rest = mid.split(":", 1)
        # Strip only an obvious "<Provider>:" prefix — the remainder must look
        # like a Bedrock model id (has a dotted vendor namespace) so we never
        # mangle a real id whose first colon is part of the version (…-v1:0).
        if "." in rest.split(":", 1)[0]:
            mid = rest
    return mid


def _friendly_bedrock_error(error: Exception, model_id: str) -> Exception:
    """Map an opaque botocore Bedrock error to an actionable message.

    Some models carry account/policy restrictions that no request parameter can
    satisfy — e.g. a model whose mandatory data-retention policy isn't enabled
    for this AWS account fails ``Converse``/``ConverseStream`` with
    ``ValidationException: data retention mode 'default' is not available for
    this model``. There's no per-request fix; surface a clear message telling
    the user to pick a different Bedrock model rather than a raw botocore dump.
    Other errors pass through unchanged.
    """
    msg = str(error)
    if "data retention" in msg and "not available for this model" in msg:
        return ProviderResolutionError(
            f"The Bedrock model '{model_id}' can't be used from this AWS account: "
            f"it requires a data-retention policy that isn't enabled here. "
            f"Choose a different Bedrock model in Settings → Models (most models "
            f"work with no extra setup)."
        )
    return error


# Model → context window tokens (shared JSON, same file openai.py/anthropic.py read).
from personalclaw.sdk.model import model_context_window as _model_window


# ── OpenAI-shape → Bedrock Converse translation ───────────────────────────
#
# The native loop sends one uniform (OpenAI-shaped) message + tool format
# across all ModelProviders; each adapts. Converse uses its own envelope:
# the system prompt is a top-level ``system`` list, tool calls are ``toolUse``
# content blocks, tool results are ``toolResult`` blocks in a user turn, and
# tool schemas live under ``toolConfig.tools[].toolSpec``. These helpers map
# the loop's shapes so :meth:`BedrockProvider.complete` accepts them unchanged.


# Bedrock Converse constrains ``toolSpec.name`` to ``[a-zA-Z0-9_-]+`` (≤64 chars).
# MCP tools are namespaced with slashes (e.g. ``mcp/github/search_issues``),
# which Bedrock rejects. We sanitize names before sending them (in BOTH the
# toolConfig and the history ``toolUse`` blocks, which must agree) and reverse-map
# the name Bedrock returns back to the real tool id before the loop dispatches it.
_TOOL_NAME_ILLEGAL_RE = re.compile(r"[^a-zA-Z0-9_-]")
_TOOL_NAME_MAX = 64


def _sanitize_tool_name(name: object) -> str:
    """Coerce a tool name into Bedrock's ``[a-zA-Z0-9_-]+`` (≤64) constraint.

    ``name`` is coerced to ``str`` first: a malformed tool schema can deliver a
    non-string here (observed: a dict), and ``re.sub`` on a non-str raises
    "expected string or bytes-like object, got 'dict'" — crashing the whole turn
    before any tool runs. Defensive str() keeps a garbled name from breaking the
    stream; valid string names are unaffected."""
    safe = _TOOL_NAME_ILLEGAL_RE.sub("_", str(name) if name else "")[:_TOOL_NAME_MAX]
    return safe or "tool"


def _build_tool_name_maps(tools: list[dict] | None) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(forward, reverse)`` maps between real tool names and the
    Bedrock-safe names. Collisions after sanitizing are disambiguated with a
    numeric suffix so the forward map stays 1:1 (and thus reversible)."""
    forward: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name", "") or ""
        if not name or name in forward:
            continue
        safe = _sanitize_tool_name(name)
        if safe in reverse:  # collision — keep names distinct
            i = 2
            base = safe[: _TOOL_NAME_MAX - 3]
            while f"{base}_{i}" in reverse:
                i += 1
            safe = f"{base}_{i}"
        forward[name] = safe
        reverse[safe] = name
    return forward, reverse


def _translate_tools(tools: list[dict], name_map: dict[str, str]) -> dict:
    """Map OpenAI ``tools`` to a Converse ``toolConfig`` dict.

    Each OpenAI entry ``{"type": "function", "function": {name, description,
    parameters}}`` becomes ``{"toolSpec": {name, description,
    inputSchema: {"json": parameters}}}``. ``name_map`` (real→Bedrock-safe) is
    applied so namespaced MCP names satisfy Bedrock's name constraint.
    """
    specs: list[dict] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        raw_name = fn.get("name", "") or ""
        specs.append(
            {
                "toolSpec": {
                    "name": name_map.get(raw_name, _sanitize_tool_name(raw_name)),
                    "description": fn.get("description", "") or "",
                    "inputSchema": {
                        "json": fn.get("parameters")
                        or {"type": "object", "properties": {}}
                    },
                }
            }
        )
    return {"tools": specs}


def _translate_messages(
    messages: list[dict], name_map: dict[str, str] | None = None
) -> tuple[list[dict], list[dict]]:
    """Split OpenAI-shaped ``messages`` into ``(system_blocks, converse_messages)``.

    * ``role: "system"`` → entries in the returned ``system`` block list.
    * ``role: "assistant"`` with ``tool_calls`` → content blocks mixing an
      optional ``{"text": ...}`` block and one ``{"toolUse": {...}}`` per call.
    * ``role: "tool"`` → a ``{"toolResult": {...}}`` block; consecutive tool
      results merge into a single user turn (Converse groups them).
    * Plain ``user``/``assistant`` strings → ``{role, content: [{"text": ...}]}``.

    ``name_map`` (real→Bedrock-safe tool name) is applied to historical
    ``toolUse`` blocks so a replayed assistant turn names tools exactly as the
    toolConfig does (Bedrock rejects a toolUse whose name is not in the config).
    """
    name_map = name_map or {}
    system_blocks: list[dict] = []
    out: list[dict] = []

    # Converse rejects the whole request when ANY text block is empty OR
    # whitespace-only ("text content blocks must contain non-whitespace text") —
    # and empty texts legitimately occur upstream (a tool that printed nothing, a
    # bare assistant turn around tool calls). Substitute a visible placeholder.
    def _text(v: object) -> dict:
        s = "" if v is None else str(v)
        return {"text": s if s.strip() else "(empty)"}

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if content:
                system_blocks.append({"text": str(content)})
            continue

        if role == "tool":
            block = {
                "toolResult": {
                    "toolUseId": str(msg.get("tool_call_id", "") or ""),
                    "content": [_text(content)],
                }
            }
            if (
                out
                and out[-1].get("role") == "user"
                and isinstance(out[-1].get("content"), list)
                and all("toolResult" in b for b in out[-1]["content"])
            ):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict] = []
            if content:
                blocks.append({"text": str(content)})
            for call in msg["tool_calls"]:
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                raw_args = fn.get("arguments", "") or ""
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (json.JSONDecodeError, ValueError):
                    parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {}
                raw_name = fn.get("name", "") or ""
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": str(call.get("id", "") or ""),
                            "name": name_map.get(raw_name, _sanitize_tool_name(raw_name)),
                            "input": parsed,
                        }
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue

        # A list content is the multimodal content-block shape (text + images), used by
        # vision use-cases. Translate each block to a Converse block; a plain string
        # (the common case) stays a single text block.
        if isinstance(content, list):
            out.append({"role": role, "content": _content_blocks_to_converse(content)})
        else:
            out.append({"role": role, "content": [_text(content)]})

    return system_blocks, _repair_tool_pairs(out)


def _content_blocks_to_converse(blocks: list) -> list[dict]:
    """Translate OpenAI-style multimodal content blocks into Converse content blocks.

    Handles the two shapes the knowledge vision nodes emit (see pipeline/nodes/_llm.py):
    ``{"type": "text", "text": ...}`` → ``{"text": ...}`` and
    ``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<...>"}}`` →
    ``{"image": {"format": <fmt>, "source": {"bytes": <raw>}}}``. Bytes are decoded
    from the data URL; a block we can't parse is dropped rather than corrupting the turn.
    Always returns at least one block (Converse rejects empty content)."""
    out: list[dict] = []
    for b in blocks:
        if not isinstance(b, dict):
            if b:
                out.append({"text": str(b)})
            continue
        btype = b.get("type")
        if btype == "text":
            t = str(b.get("text", ""))
            out.append({"text": t if t.strip() else "(empty)"})
        elif btype == "image_url":
            url = ((b.get("image_url") or {}) if isinstance(b.get("image_url"), dict) else {}).get("url", "")
            img = _data_url_to_converse_image(url)
            if img:
                out.append(img)
    return out or [{"text": "(empty)"}]


def _data_url_to_converse_image(url: str) -> dict | None:
    """``data:image/jpeg;base64,<b64>`` → a Converse ``{"image": {...}}`` block, or None."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        header, b64 = url.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "image/png"  # strip "data:"
        fmt = mime.split("/", 1)[1].lower() if "/" in mime else "png"
        # Converse accepts png/jpeg/gif/webp; normalise jpg→jpeg.
        fmt = {"jpg": "jpeg"}.get(fmt, fmt)
        raw = base64.b64decode(b64)
    except (ValueError, binascii.Error):
        return None
    if not raw:
        return None
    return {"image": {"format": fmt, "source": {"bytes": raw}}}


# Synthetic result fed to Bedrock when an assistant ``toolUse`` has no matching
# ``toolResult`` in history — see :func:`_repair_tool_pairs`.
_ORPHAN_TOOL_RESULT_TEXT = (
    "[tool result unavailable — the previous turn was interrupted before this "
    "tool finished; treat it as no-op and continue]"
)


def _repair_tool_pairs(messages: list[dict]) -> list[dict]:
    """Make ``toolUse``/``toolResult`` pairing valid for Converse.

    Converse rejects a request (``Expected toolResult blocks at messages.N…``)
    when an assistant ``toolUse`` block isn't answered by a ``toolResult`` (same
    ``toolUseId``) in the immediately-following user turn, and likewise rejects a
    ``toolResult`` that answers no preceding ``toolUse``. A turn cancelled
    mid-inference (watchdog wedged-turn recovery / circuit-breaker) records the
    assistant's tool calls but no results, leaving such orphans in the loop's
    persisted history; on the next cycle (or Resume) the whole history replays
    and the request fails.

    This boundary repair, applied to every Converse request, heals such history
    in place: each unanswered ``toolUse`` gets a synthetic ``toolResult`` (marked
    as an interrupted no-op) injected into the following user turn, and any
    ``toolResult`` with no matching prior ``toolUse`` is dropped. Well-formed
    history is returned unchanged.
    """
    # Pass 1: drop toolResult blocks that answer no emitted toolUse id.
    emitted: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for b in msg.get("content") or []:
                tu = b.get("toolUse") if isinstance(b, dict) else None
                if tu is not None:
                    emitted.add(str(tu.get("toolUseId", "") or ""))
    cleaned: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if (
            msg.get("role") == "user"
            and isinstance(content, list)
            and any("toolResult" in b for b in content)
        ):
            kept = [
                b
                for b in content
                if "toolResult" not in b
                or str(b["toolResult"].get("toolUseId", "") or "") in emitted
            ]
            if not kept:
                continue  # whole turn was orphan results — drop it
            cleaned.append({**msg, "content": kept})
        else:
            cleaned.append(msg)

    # Pass 2: every assistant toolUse must be answered in the NEXT user turn;
    # inject synthetic results for any that aren't.
    out: list[dict] = []
    for i, msg in enumerate(cleaned):
        out.append(msg)
        if msg.get("role") != "assistant":
            continue
        ids = [
            str(b["toolUse"].get("toolUseId", "") or "")
            for b in (msg.get("content") or [])
            if isinstance(b, dict) and "toolUse" in b
        ]
        if not ids:
            continue
        nxt = cleaned[i + 1] if i + 1 < len(cleaned) else None
        answered: set[str] = set()
        nxt_is_results = (
            nxt is not None
            and nxt.get("role") == "user"
            and isinstance(nxt.get("content"), list)
            and any("toolResult" in b for b in nxt["content"])
        )
        if nxt_is_results:
            answered = {
                str(b["toolResult"].get("toolUseId", "") or "")
                for b in nxt["content"]
                if "toolResult" in b
            }
        missing = [tid for tid in ids if tid not in answered]
        if not missing:
            continue
        synthetic = [
            {
                "toolResult": {
                    "toolUseId": tid,
                    "content": [{"text": _ORPHAN_TOOL_RESULT_TEXT}],
                }
            }
            for tid in missing
        ]
        if nxt_is_results:
            # Prepend so the synthetic results sit alongside the real ones.
            nxt["content"] = synthetic + list(nxt["content"])
        else:
            out.append({"role": "user", "content": synthetic})
    return out


class BedrockProvider(ModelProvider):
    """ModelProvider backed by the Bedrock Converse streaming API.

    The legacy :meth:`stream` path is text-only. :meth:`complete` (the
    native-loop contract) additionally supports multi-message history and
    tool calling: the Converse API natively accepts a ``toolConfig`` and
    emits ``toolUse`` content blocks, which :meth:`complete` translates to
    the same :data:`EVENT_TOOL_CALL` shape the other providers emit. ``boto3``
    is imported in :meth:`start` to keep module import SDK-free (Property 11).
    """

    # Bedrock Converse supports tools + multi-message; complete() drives them
    # by translating the loop's OpenAI-shaped messages/tools into Converse.
    supports_tools: bool = True

    def __init__(
        self,
        *,
        model: str,
        region: str = DEFAULT_REGION,
        profile_name: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        # NO credential parameter — boto3's chain authenticates (G-AUTH).
        # Empty ⇒ resolve from live discovery at start() (no hardcoded default id).
        self._model_id = model or ""
        self._region = region or DEFAULT_REGION
        self._profile = profile_name or None
        self._system_prompt = (system_prompt or "").strip()
        # Always send an output cap (see _DEFAULT_MAX_TOKENS): omitting it lets
        # Converse truncate large tool-call JSON mid-stream.
        self._max_tokens = max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS
        self._client: Any = None
        # Converse message shape: [{"role": "user"|"assistant",
        #                           "content": [{"text": "..."}]}]
        self._history: list[dict[str, Any]] = []
        self._last_context_pct: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Build the ``bedrock-runtime`` client via boto3's credential chain.

        boto3 session + client construction is SYNCHRONOUS and can be slow — for an
        SSO profile it resolves (and may refresh) cached credentials on the calling
        thread. Doing that inline on the event loop froze the whole gateway for
        ~0.5-1.5s on the first chat turn, which stalled other tabs' WebSocket
        handshakes and emptied the composer model list (the "warmup blocks the
        websocket" symptom). So the blocking build runs in a worker thread.
        """

        def _build_client() -> Any:
            # Lazy import per Property 11. Do NOT lift to module top.
            import boto3  # noqa: PLC0415
            from botocore.config import Config  # noqa: PLC0415

            # Long read timeout so a healthy-but-quiet reasoning stream isn't killed
            # mid-turn (see _STREAM_READ_TIMEOUT). Retries OFF: PersonalClaw owns
            # retry and stall recovery at the loop/watchdog layer, and a botocore
            # retry of a streaming call would replay a partially consumed turn.
            boto_config = Config(
                read_timeout=_STREAM_READ_TIMEOUT,
                connect_timeout=_CONNECT_TIMEOUT,
                retries={"max_attempts": 0, "mode": "standard"},
                tcp_keepalive=True,
            )
            session = (
                boto3.Session(profile_name=self._profile)
                if self._profile else boto3.Session()
            )
            return session.client(
                "bedrock-runtime", region_name=self._region, config=boto_config
            )

        self._client = await asyncio.to_thread(_build_client)
        # No model pinned → resolve the default from LIVE discovery (no hardcoded id).
        # _resolve_default_model_id already runs its boto calls via to_thread.
        if not self._model_id:
            self._model_id = await _resolve_default_model_id(self._region, self._profile)
        logger.info(
            "Bedrock provider ready: model=%s region=%s profile=%s",
            self._model_id or "<unresolved>",
            self._region,
            self._profile or "<default-chain>",
        )

    async def shutdown(self) -> None:
        """Release the boto3 client and clear conversation history."""
        # botocore clients hold a connection pool but expose no async close;
        # dropping the reference lets it be GC'd. History is cleared eagerly.
        self._client = None
        self._history.clear()

    # ── Streaming ─────────────────────────────────────────────────────

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        """Stream a Converse completion, bridging the sync boto stream.

        The blocking ``converse_stream`` call and its ``EventStream``
        iteration run in a worker thread; text deltas and the final usage
        record are pushed onto an :class:`asyncio.Queue` this generator
        drains, so the event loop is never blocked by boto I/O.
        """
        if self._client is None:
            await self.start()

        self._history.append({"role": "user", "content": [{"text": message if message.strip() else "(empty)"}]})
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]

        request: dict[str, Any] = {
            "modelId": self._model_id,
            "messages": self._history,
        }
        if self._system_prompt:
            request["system"] = [{"text": self._system_prompt}]
        if self._max_tokens is not None:
            request["inferenceConfig"] = {"maxTokens": self._max_tokens}

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def _pump() -> None:
            """Run in a worker thread: drive the sync boto stream onto the queue."""
            try:
                response = self._client.converse_stream(**request)
                for event in response.get("stream", []):
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"].get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, ("text", text))
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                        loop.call_soon_threadsafe(queue.put_nowait, ("usage", usage))
            except Exception as exc:  # surface to the consumer, never crash the thread
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

        worker = asyncio.ensure_future(asyncio.to_thread(_pump))

        assistant_text = ""
        input_tokens = 0
        output_tokens = 0
        error: Exception | None = None
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_DONE:
                    break
                kind, payload = item
                if kind == "text":
                    assistant_text += payload
                    yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=payload)
                elif kind == "usage":
                    input_tokens = int(payload.get("inputTokens", input_tokens) or input_tokens)
                    output_tokens = int(payload.get("outputTokens", output_tokens) or output_tokens)
                elif kind == "error":
                    error = payload
        finally:
            await worker  # ensure the thread is joined even on cancellation

        if error is not None:
            raise _friendly_bedrock_error(error, self._model_id)

        if input_tokens > 0:
            ctx = _model_window(self._model_id, _DEFAULT_CONTEXT_WINDOW)
            self._last_context_pct = (input_tokens / ctx) * 100

        if assistant_text:
            self._history.append({"role": "assistant", "content": [{"text": assistant_text}]})

        yield LLMEvent(
            kind=EVENT_COMPLETE,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_usage_pct=self._last_context_pct,
        )

    # ── Stateless completion (native loop) ────────────────────────────

    async def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        reasoning_effort: str = "",
    ) -> AsyncIterator[LLMEvent]:
        """Stream a stateless Converse turn for the full ``messages`` list.

        Unlike :meth:`stream`, this NEVER touches ``self._history`` — the
        native loop owns conversation state and sends OpenAI-shaped messages
        + tools uniformly; :func:`_translate_messages` / :func:`_translate_tools`
        map them into Converse's envelope (system list, ``toolUse`` /
        ``toolResult`` blocks, ``toolConfig``).

        Converse streams ``toolUse`` calls as a ``contentBlockStart`` (carrying
        ``toolUseId`` + ``name``) followed by ``contentBlockDelta`` input JSON
        fragments and a ``contentBlockStop``; the completed block emits one
        :data:`EVENT_TOOL_CALL`. The blocking boto stream is bridged onto an
        :class:`asyncio.Queue` so the event loop is never stalled (mirrors
        :meth:`stream`).
        """
        if self._client is None:
            await self.start()

        # Bedrock-safe tool names (real↔safe). Built from the live `tools` list;
        # reused to rename historical toolUse blocks so config + history agree.
        tool_name_fwd, tool_name_rev = _build_tool_name_maps(tools)
        system_blocks, converse_messages = _translate_messages(messages, tool_name_fwd)

        request: dict[str, Any] = {
            "modelId": _bare_model_id(model, self._model_id),
            "messages": converse_messages,
        }
        if system_blocks:
            request["system"] = system_blocks
        elif self._system_prompt:
            request["system"] = [{"text": self._system_prompt}]
        if tools:
            request["toolConfig"] = _translate_tools(tools, tool_name_fwd)
        if self._max_tokens is not None:
            request["inferenceConfig"] = {"maxTokens": self._max_tokens}

        # Extended thinking (Anthropic-on-Bedrock): map reasoning effort via
        # additionalModelRequestFields. Newer Claude models (Opus 4.x on Bedrock)
        # take adaptive thinking + an effort LEVEL (output_config.effort) directly —
        # which fits the "no canonical scale" model: the effort token is forwarded
        # as-is. Older models take a fixed budget_tokens; we send the level shape
        # and fall back on the ValidationException path is avoided by using the
        # documented adaptive form. "" = no thinking (model default).
        _eff = (reasoning_effort or "").strip()
        if _eff:
            request["additionalModelRequestFields"] = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": _eff},
            }

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def _pump() -> None:
            """Run in a worker thread: drive the sync boto stream onto the queue."""
            try:
                response = self._client.converse_stream(**request)
                for event in response.get("stream", []):
                    if "contentBlockStart" in event:
                        start = event["contentBlockStart"]
                        block_index = start.get("contentBlockIndex", 0)
                        tool_use = start.get("start", {}).get("toolUse")
                        if tool_use is not None:
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                ("tool_start", (block_index, tool_use)),
                            )
                    elif "contentBlockDelta" in event:
                        block = event["contentBlockDelta"]
                        block_index = block.get("contentBlockIndex", 0)
                        delta = block.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, ("text", text))
                        tool_delta = delta.get("toolUse")
                        if tool_delta is not None:
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                ("tool_delta", (block_index, tool_delta.get("input", ""))),
                            )
                    elif "contentBlockStop" in event:
                        block_index = event["contentBlockStop"].get("contentBlockIndex", 0)
                        loop.call_soon_threadsafe(queue.put_nowait, ("block_stop", block_index))
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                        loop.call_soon_threadsafe(queue.put_nowait, ("usage", usage))
            except Exception as exc:  # surface to the consumer, never crash the thread
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

        worker = asyncio.ensure_future(asyncio.to_thread(_pump))

        # Per content-block-index accumulators for toolUse blocks.
        tool_blocks: dict[int, dict[str, str]] = {}
        emitted_tool_calls: set[int] = set()
        input_tokens = 0
        output_tokens = 0
        error: Exception | None = None
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_DONE:
                    break
                kind, payload = item
                if kind == "text":
                    yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=payload)
                elif kind == "tool_start":
                    block_index, tool_use = payload
                    tool_blocks[block_index] = {
                        "id": str(tool_use.get("toolUseId", "") or ""),
                        "name": str(tool_use.get("name", "") or ""),
                        "arguments": "",
                    }
                elif kind == "tool_delta":
                    block_index, frag = payload
                    bucket = tool_blocks.get(block_index)
                    if bucket is not None and frag:
                        bucket["arguments"] += frag
                elif kind == "block_stop":
                    bucket = tool_blocks.get(payload)
                    if bucket is not None and payload not in emitted_tool_calls:
                        emitted_tool_calls.add(payload)
                        yield LLMEvent(
                            kind=EVENT_TOOL_CALL,
                            tool_call_id=bucket["id"],
                            # Reverse-map the Bedrock-safe name back to the real
                            # tool id so the loop dispatches the actual tool.
                            title=tool_name_rev.get(bucket["name"], bucket["name"]),
                            tool_input=bucket["arguments"],
                        )
                elif kind == "usage":
                    input_tokens = int(payload.get("inputTokens", input_tokens) or input_tokens)
                    output_tokens = int(payload.get("outputTokens", output_tokens) or output_tokens)
                elif kind == "error":
                    error = payload
        finally:
            await worker  # ensure the thread is joined even on cancellation

        if error is not None:
            raise _friendly_bedrock_error(error, model or self._model_id)

        # Defensive flush — emit any unfinalized tool blocks.
        for block_index, bucket in tool_blocks.items():
            if block_index in emitted_tool_calls:
                continue
            emitted_tool_calls.add(block_index)
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=bucket["id"],
                title=tool_name_rev.get(bucket["name"], bucket["name"]),
                tool_input=bucket["arguments"],
            )

        context_pct = 0.0
        if input_tokens > 0:
            ctx = _model_window(model or self._model_id, _DEFAULT_CONTEXT_WINDOW)
            context_pct = (input_tokens / ctx) * 100

        yield LLMEvent(
            kind=EVENT_COMPLETE,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_usage_pct=context_pct,
            cost_usd=0.0,
        )

    # ── Tool approval (no-op — text-only) ─────────────────────────────

    async def approve_tool(self, request_id: str | int) -> None:
        """No-op: Bedrock is text-only at this layer (no interactive tools)."""
        return None

    async def reject_tool(self, request_id: str | int) -> None:
        """No-op: Bedrock is text-only at this layer (no interactive tools)."""
        return None

    # ── Status ────────────────────────────────────────────────────────

    def context_usage_pct(self) -> float:
        return self._last_context_pct

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        """No turn-level abort wired yet; the worker is joined per-stream."""
        return "no_turn"


# ── Capability descriptor ────────────────────────────────────────────────
BEDROCK_CAPABILITY = ProviderCapability(
    type="bedrock",
    capabilities=frozenset({Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING, Capability.VISION}),
    supports_streaming=True,
    supports_tools=True,
    supports_embeddings=False,
    supports_vision=True,
    max_context_tokens=0,  # model-dependent
    notes=(
        "Amazon Bedrock Converse via boto3; AWS credential chain. The native-loop "
        "complete() path supports multi-message + tools + image content blocks (vision "
        "models like Nova, Claude, Gemma-VL, Qwen-VL); legacy stream() is text-only."
    ),
)


# ── Catalog (discovery + connectivity via the AWS Bedrock control plane) ──
#
# Model discovery for Bedrock is NOT an HTTP /v1/models call — it queries the AWS
# control plane via boto3 (list_foundation_models + list_inference_profiles). This
# logic used to live in core's discovery handler (coupling core to boto3 + a
# hardcoded fallback catalog); it belongs with the provider. boto3 stays lazily
# imported (Property 11) so importing this module is SDK-free.

# NO hardcoded fallback catalog (user directive 2026-07-06): Bedrock is discovered
# from the control plane. If discovery can't run (no boto3/creds/permission), the
# model list is EMPTY (the UI shows "no models discovered — check AWS creds/region")
# rather than fake ids that may not be invocable. Discovery is authoritative.


async def _resolve_default_model_id(region: str, profile: str | None) -> str:
    """Pick an unpinned default from LIVE discovery — no hardcoded id.

    Discovers the account's invocable models (foundation + inference profiles) and
    returns the first that matches ``_DEFAULT_MODEL_PREFERENCE`` (a mid-tier Claude,
    else any Claude, else Nova, else the first discovered). Returns "" when nothing
    is discoverable (no creds/permission) — the caller then errors clearly at call
    time rather than invoking a bogus baked id (bug #32's failure mode)."""
    try:
        rows = await asyncio.to_thread(_list_bedrock_models_sync, region, profile or "")
    except Exception:
        logger.debug("Bedrock default resolution: discovery failed", exc_info=True)
        return ""
    ids = [str(r.get("id", "")) for r in rows if r.get("id")]
    if not ids:
        return ""
    for needle in _DEFAULT_MODEL_PREFERENCE:
        match = next((i for i in ids if needle in i.lower()), None)
        if match:
            logger.info("Bedrock: auto-selected default %r (matched %r) from discovery", match, needle)
            return match
    return ids[0]  # nothing preferred matched → first discovered (still not hardcoded)


def _list_bedrock_models_sync(region: str, profile: str) -> list[dict[str, Any]]:
    """Query the Bedrock control plane for every text-capable model + inference
    profile. Blocking boto3 calls — run via ``asyncio.to_thread``.

    Combines two sources so the dropdown shows what's actually invocable:
      * ``list_foundation_models(byOutputModality="TEXT")`` — base model ids that
        support ON_DEMAND throughput (direct ``modelId`` invocation).
      * ``list_inference_profiles()`` — cross-region / system profiles (the
        ``us.*`` ids) which are the only way to call models that don't offer
        ON_DEMAND (e.g. newer Claude). Each profile id is directly invocable.
    """
    import boto3  # noqa: PLC0415 — lazy per Property 11

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    client = session.client("bedrock", region_name=region or "us-east-1")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # ── Foundation models (ON_DEMAND text generators) ──
    try:
        resp = client.list_foundation_models(byOutputModality="TEXT")
        for m in resp.get("modelSummaries", []):
            model_id = m.get("modelId", "")
            if not model_id or model_id in seen:
                continue
            # Only models invocable directly (ON_DEMAND); the rest need a profile
            # and surface via list_inference_profiles below.
            if "ON_DEMAND" not in (m.get("inferenceTypesSupported") or []):
                continue
            lifecycle = (m.get("modelLifecycle") or {}).get("status", "ACTIVE")
            if lifecycle != "ACTIVE":
                continue
            in_modalities = m.get("inputModalities") or []
            out_modalities = m.get("outputModalities") or []
            # Voxtral (SPEECH-input/TEXT-output) is an AUDIO MODALITY model — it
            # understands audio in a chat context (like vision models understand
            # images). It is NOT an STT model: STT is Amazon Transcribe (a dedicated
            # deterministic transcription service, not a chat LLM). Nova Sonic
            # (SPEECH-in/SPEECH+TEXT-out) is excluded entirely — it requires the
            # bidirectional-streaming protocol this registry can't drive.
            if "SPEECH" in in_modalities and "SPEECH" not in out_modalities:
                caps = ["chat", "audio_modality"]
            elif "SPEECH" in in_modalities:
                continue  # Nova Sonic — skip (bidirectional only)
            else:
                caps = ["chat"]
                if "IMAGE" in in_modalities:
                    caps.append("image_modality")
            provider = m.get("providerName", "")
            label = m.get("modelName", model_id)
            seen.add(model_id)
            out.append({
                "id": model_id,
                "name": f"{label}" + (f" ({provider})" if provider and provider not in label else ""),
                "capabilities": caps,
            })
    except Exception:
        logger.debug("Bedrock list_foundation_models failed", exc_info=True)

    # ── Foundation models (EMBEDDING output) ──
    # Embedding models (Titan Embed, Cohere Embed) have output modality EMBEDDING,
    # not TEXT, so they're missed by the TEXT query above. Query separately.
    try:
        resp = client.list_foundation_models(byOutputModality="EMBEDDING")
        for m in resp.get("modelSummaries", []):
            model_id = m.get("modelId", "")
            if not model_id or model_id in seen:
                continue
            if "ON_DEMAND" not in (m.get("inferenceTypesSupported") or []):
                continue
            lifecycle = (m.get("modelLifecycle") or {}).get("status", "ACTIVE")
            if lifecycle != "ACTIVE":
                continue
            provider = m.get("providerName", "")
            label = m.get("modelName", model_id)
            seen.add(model_id)
            out.append({
                "id": model_id,
                "name": f"{label}" + (f" ({provider})" if provider and provider not in label else ""),
                "capabilities": ["embedding"],
            })
    except Exception:
        logger.debug("Bedrock list_foundation_models(EMBEDDING) failed", exc_info=True)

    # ── Inference profiles (cross-region / system — the us.* invocable ids) ──
    try:
        paginator_kwargs: dict[str, Any] = {}
        while True:
            resp = client.list_inference_profiles(**paginator_kwargs)
            for p in resp.get("inferenceProfileSummaries", []):
                pid = p.get("inferenceProfileId", "")
                if not pid or pid in seen:
                    continue
                if p.get("status", "ACTIVE") != "ACTIVE":
                    continue
                seen.add(pid)
                out.append({
                    "id": pid,
                    "name": p.get("inferenceProfileName", pid),
                    "capabilities": ["chat"],
                })
            token = resp.get("nextToken")
            if not token:
                break
            paginator_kwargs = {"nextToken": token}
    except Exception:
        logger.debug("Bedrock list_inference_profiles failed", exc_info=True)

    return out


# Short TTL cache keyed by (region, profile): the control-plane catalog is stable
# and Settings discovery is hit on every dropdown open, so we avoid two AWS
# round-trips per request. Only successful non-empty results are cached.
_BEDROCK_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_BEDROCK_CACHE_TTL = 300.0  # seconds


class BedrockCatalog(ModelCatalog):
    """Discovers Bedrock models from the AWS control plane (boto3 chain auth),
    cached for ``_BEDROCK_CACHE_TTL`` seconds. Falls back to a curated catalog on
    any failure (boto3 missing, no creds, permission/region error) so the dropdown
    is never empty. Config-only: reads region/profile from the entry options."""

    def __init__(self, region: str = "", profile: str = "") -> None:
        self._region = region or ""
        self._profile = profile or ""

    async def list_models(self) -> list[ModelInfo]:
        import time

        key = (self._region, self._profile)
        cached = _BEDROCK_CACHE.get(key)
        if cached and (time.monotonic() - cached[0]) < _BEDROCK_CACHE_TTL:
            rows = cached[1]
        else:
            try:
                rows = await asyncio.to_thread(_list_bedrock_models_sync, self._region, self._profile)
            except Exception:
                logger.debug("Bedrock dynamic discovery failed", exc_info=True)
                rows = []
            rows.sort(key=lambda m: m.get("name", "").lower())
            if rows:
                _BEDROCK_CACHE[key] = (time.monotonic(), rows)
            # Discovery failed/empty → return [] (no hardcoded floor). The catalog is
            # authoritative; a keyless/misconfigured account shows an empty list, not
            # fake ids.
        return [
            ModelInfo(id=r["id"], name=r.get("name", r["id"]), capabilities=list(r.get("capabilities", ["chat"])))
            for r in rows
        ]

    async def test_connection(self) -> ConnectionResult:
        # A successful control-plane list is the connectivity signal. The fallback
        # catalog is non-empty even without creds, so distinguish real discovery
        # (cached/live rows) from the fallback by re-checking the cache after list.
        import time

        models = await self.list_models()
        key = (self._region, self._profile)
        cached = _BEDROCK_CACHE.get(key)
        live = bool(cached and (time.monotonic() - cached[0]) < _BEDROCK_CACHE_TTL)
        if live:
            return ConnectionResult(ok=True, model_count=len(models))
        return ConnectionResult(
            ok=False,
            detail="Could not reach the AWS Bedrock control plane (check credentials/region); showing a fallback catalog.",
            model_count=len(models),
        )


def create_catalog(options: dict[str, Any] | None = None, *, model: str = "") -> BedrockCatalog:
    """Catalog factory (registry contract) — build discovery from entry options."""
    del model
    opts = options or {}
    return BedrockCatalog(region=str(opts.get("region") or ""), profile=str(opts.get("profile") or ""))


# ── Extension factory (named by the bundled manifest's `implementation`) ──


def create_provider(config: dict[str, Any]) -> BedrockProvider:
    """Build a :class:`BedrockProvider` from a model-Extension instance config.

    Reads ``region`` / ``default_model`` (or ``model``) / ``profile`` /
    ``system_prompt`` / ``max_tokens`` from the instance settings. No
    credential is resolved — boto3's chain authenticates (G-AUTH).
    """
    max_tokens_value = config.get("max_tokens")
    max_tokens = int(max_tokens_value) if isinstance(max_tokens_value, int) else None
    return BedrockProvider(
        # Empty when unpinned → resolved from live discovery at start() (no baked id).
        model=config.get("model") or config.get("default_model") or "",
        region=config.get("region") or DEFAULT_REGION,
        profile_name=config.get("profile") or None,
        system_prompt=config.get("system_prompt") or None,
        max_tokens=max_tokens,
    )


# ── Registry factory ──────────────────────────────────────────────────────


def _factory(
    *,
    entry: ProviderEntry,
    session_key: str | None = None,
    **kwargs: object,
) -> ModelProvider:
    """Construct a :class:`BedrockProvider` from a :class:`ProviderEntry`.

    ``session_key`` is accepted for registry-contract parity but ignored —
    Bedrock is stateless. No credential is resolved (G-AUTH): the entry's
    options carry only region/model/profile.

    A ``model`` kwarg (threaded by ``registry.build(name, model=…)``) overrides the
    entry's pinned model. The config.json Bedrock entry usually has NO pinned model
    — the active model lives in ``active_models.json`` and is resolved per use-case
    (e.g. ``Bedrock:global.anthropic.claude-opus-4-8``) — so a caller that builds
    the provider for a specific model (one_shot_completion's reasoning axis) MUST be
    able to pass it, or the provider would silently fall back to the on-demand
    default and ignore the user's selection.
    """
    del session_key  # unused — Bedrock provider is stateless.

    options = dict(entry.options or {})
    region = str(options.get("region") or DEFAULT_REGION)
    profile_value = options.get("profile")
    profile = str(profile_value) if profile_value else None
    system_value = options.get("system_prompt")
    system_prompt = str(system_value) if system_value else None
    max_tokens_value = options.get("max_tokens")
    max_tokens = int(max_tokens_value) if isinstance(max_tokens_value, int) else None

    model_override = kwargs.get("model")
    # Unpinned (no override, no entry.model) → "" → resolved from live discovery at
    # start(). No hardcoded default id.
    model = str(model_override) if model_override else (entry.model or "")

    return BedrockProvider(
        model=model,
        region=region,
        profile_name=profile,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
    )


# ── Registration ─────────────────────────────────────────────────────────
# Register on import — the app loader imports this module when the app is
# enabled, wiring the type into the default registry without pulling boto3
# into ``sys.modules`` (lazy SDK import). Idempotent against module reload
# in tests.
try:
    get_default_registry().register_type(BEDROCK_CAPABILITY, _factory)
except ProviderResolutionError:
    logger.debug("bedrock provider type already registered with default registry")

# The discovery/connectivity axis (register_catalog is idempotent — last wins).
get_default_registry().register_catalog("bedrock", create_catalog)


# ═══════════════════════════════════════════════════════════════════════════════
# ADDITIONAL PROVIDERS: Embedding, Image Generation, Video Generation, STT
# ═══════════════════════════════════════════════════════════════════════════════
#
# Each provider is independent of the chat ``BedrockProvider`` above and targets a
# different capability axis. They share the same boto3 credential chain and region
# resolution, and wrap all synchronous boto3 calls in ``asyncio.to_thread`` /
# ``run_in_executor`` so the event loop is never blocked.

import os
import tempfile
import time as _time

from personalclaw.sdk.embedding import EmbeddingProvider
from personalclaw.sdk.image import (
    ImageGenError,
    ImageGenModel,
    ImageGenProvider,
    ImageResult,
)
from personalclaw.sdk.video import (
    VideoGenError,
    VideoGenModel,
    VideoGenProvider,
    VideoResult,
)
from personalclaw.sdk.stt import SttProvider, TranscriptResult


def _resolve_region(config: dict | None) -> str:
    """Region from config → env → default."""
    return str((config or {}).get("region", "") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def _resolve_profile(config: dict | None) -> str | None:
    """AWS profile from config (None = default chain)."""
    p = str((config or {}).get("profile", "") or "")
    return p or None


# ── Shared credential check (never blocks the event loop) ────────────────────
#
# boto3's ``Session.get_credentials()`` does synchronous disk / SSO-cache I/O
# (and can trigger a network refresh for an SSO profile). Calling it directly in
# an async ``is_available()`` blocks the aiohttp event loop — and the media
# registries probe ``is_available()`` on every ``/api/models/available`` call,
# so the loop stalls (symptoms: the composer's model list empties on send, new
# tabs spin). Run it on a worker thread AND cache the boolean per (profile,
# region) so repeated probes are instant.

_cred_cache: dict[tuple[str, str], bool] = {}


async def _creds_ok(region: str, profile: str | None) -> bool:
    """Whether the AWS credential chain resolves for this profile — cached,
    and run off the event loop so it never blocks."""
    key = (profile or "", region or "")
    if key in _cred_cache:
        return _cred_cache[key]

    def _probe() -> bool:
        try:
            import boto3  # noqa: PLC0415

            session = boto3.Session(profile_name=profile) if profile else boto3.Session()
            return session.get_credentials() is not None
        except Exception:
            return False

    ok = await asyncio.to_thread(_probe)
    _cred_cache[key] = ok
    return ok


# ── Bedrock Embedding Provider ───────────────────────────────────────────────


_EMBEDDING_MODELS = [
    ("amazon.titan-embed-text-v2:0", 1024),
    ("amazon.titan-embed-text-v1", 1536),
    ("cohere.embed-v4:0", 1024),
]
_DEFAULT_EMBED_MODEL = "amazon.titan-embed-text-v2:0"


class BedrockEmbeddingProvider(EmbeddingProvider):
    """Embedding via Bedrock ``invoke_model`` (Titan Embeddings / Cohere Embed).

    boto3 is lazily imported inside methods (Property 11). All blocking calls
    run via ``asyncio.to_thread`` so the event loop stays unblocked.
    """

    def __init__(self, *, region: str = "us-east-1", profile: str | None = None,
                 name: str = "bedrock") -> None:
        self._region = region
        self._profile = profile
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Amazon Bedrock (embedding)"

    def _get_client(self):
        """Build a fresh bedrock-runtime client (lazy boto3 import)."""
        import boto3  # noqa: PLC0415

        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return session.client("bedrock-runtime", region_name=self._region)

    async def is_available(self) -> bool:
        """True if the AWS credential chain resolves (cached, off-loop)."""
        return await _creds_ok(self._region, self._profile)

    def _invoke_embed_sync(self, text: str, model: str) -> list[float] | None:
        """Blocking invoke_model for embedding — run via to_thread."""
        client = self._get_client()
        model_id = model or _DEFAULT_EMBED_MODEL

        # Build request body per model family
        if model_id.startswith("cohere"):
            body = json.dumps({"texts": [text], "input_type": "search_document"})
        else:
            # Titan Embed
            body = json.dumps({"inputText": text, "dimensions": 1024, "normalize": True})

        response = client.invoke_model(modelId=model_id, body=body)
        result = json.loads(response["body"].read())

        if model_id.startswith("cohere"):
            embeddings = result.get("embeddings", {})
            # Cohere v4 returns float embeddings under "float" key
            floats = embeddings.get("float", [])
            return floats[0] if floats else None
        else:
            return result.get("embedding")

    async def embed(self, text: str, model: str = "") -> list[float] | None:
        """Embed a single text string."""
        try:
            return await asyncio.to_thread(self._invoke_embed_sync, text, model)
        except Exception:
            logger.debug("Bedrock embedding failed", exc_info=True)
            return None

    async def embed_batch(self, texts: list[str], model: str = "") -> list[list[float]]:
        """Embed multiple texts (sequential calls — Bedrock has no native batch)."""
        results: list[list[float]] = []
        for text in texts:
            vec = await self.embed(text, model)
            results.append(vec if vec is not None else [])
        return results


# ── Bedrock Image Generation Provider ────────────────────────────────────────


_IMAGE_MODELS = [
    ImageGenModel(
        name="amazon.nova-canvas-v1:0",
        description="Amazon Nova Canvas — text-to-image generation",
        sizes=["1024x1024", "1280x720", "720x1280"],
        supports_edit=False,
    ),
]


class BedrockImageProvider(ImageGenProvider):
    """Image generation via Bedrock ``invoke_model`` (Nova Canvas).

    Generates images synchronously via the Bedrock runtime. The blocking
    invoke_model call runs in a thread pool.
    """

    def __init__(self, *, region: str = "us-east-1", profile: str | None = None,
                 name: str = "bedrock") -> None:
        self._region = region
        self._profile = profile
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Amazon Bedrock (image)"

    def _get_client(self):
        import boto3  # noqa: PLC0415

        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return session.client("bedrock-runtime", region_name=self._region)

    async def is_available(self) -> bool:
        """True if the AWS credential chain resolves (cached, off-loop)."""
        return await _creds_ok(self._region, self._profile)

    async def list_models(self) -> list[ImageGenModel]:
        return list(_IMAGE_MODELS)

    def _generate_sync(self, prompt: str, model: str, size: str, n: int) -> list[dict]:
        """Blocking image generation — run via to_thread."""
        client = self._get_client()
        model_id = model or "amazon.nova-canvas-v1:0"

        # Parse size
        width, height = 1024, 1024
        if size and "x" in size.lower():
            parts = size.lower().split("x")
            try:
                width, height = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                pass

        body = json.dumps({
            "taskType": "TEXT_IMAGE",
            "textToImageParams": {"text": prompt},
            "imageGenerationConfig": {
                "numberOfImages": n,
                "width": width,
                "height": height,
            },
        })

        response = client.invoke_model(modelId=model_id, body=body)
        result = json.loads(response["body"].read())
        return result.get("images", [])

    async def generate(
        self,
        prompt: str,
        *,
        model: str = "",
        size: str = "",
        n: int = 1,
        **opts: Any,
    ) -> list[ImageResult]:
        """Generate images from a text prompt via Nova Canvas."""
        try:
            images_b64 = await asyncio.to_thread(self._generate_sync, prompt, model, size, n)
        except Exception as exc:
            raise ImageGenError(f"Bedrock image generation failed: {exc}") from exc

        results: list[ImageResult] = []
        for b64_str in images_b64:
            results.append(ImageResult(b64=b64_str, mime="image/png"))
        return results

    async def edit(
        self,
        prompt: str,
        *,
        source_image: str,
        mask: str = "",
        model: str = "",
        size: str = "",
        n: int = 1,
        **opts: Any,
    ) -> list[ImageResult]:
        """Edit is not supported by Bedrock Nova Canvas."""
        raise ImageGenError("Image editing is not supported by Amazon Bedrock Nova Canvas.")


# ── Bedrock Video Generation Provider ────────────────────────────────────────


_VIDEO_MODELS = [
    VideoGenModel(
        name="amazon.nova-reel-v1:1",
        description="Amazon Nova Reel — text-to-video generation",
        aspect_ratios=["16:9"],
        max_duration_s=6,
    ),
]

_VIDEO_POLL_INTERVAL = 10  # seconds
_VIDEO_POLL_TIMEOUT = 300  # seconds


class BedrockVideoProvider(VideoGenProvider):
    """Video generation via Bedrock async invoke (Nova Reel).

    ``generate()`` performs the full submit → poll → download cycle:
    1. ``start_async_invoke`` submits the generation job
    2. ``get_async_invoke`` polls until ``status == 'Completed'``
    3. Download the MP4 from the S3 output path

    Requires ``video_s3_bucket`` in config or ``BEDROCK_VIDEO_S3_BUCKET`` env var.
    """

    def __init__(
        self,
        *,
        region: str = "us-east-1",
        profile: str | None = None,
        s3_bucket: str = "",
        name: str = "bedrock",
    ) -> None:
        self._region = region
        self._profile = profile
        self._s3_bucket = s3_bucket or os.environ.get("BEDROCK_VIDEO_S3_BUCKET", "")
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Amazon Bedrock (video)"

    def _get_runtime_client(self):
        import boto3  # noqa: PLC0415

        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return session.client("bedrock-runtime", region_name=self._region)

    def _get_s3_client(self):
        import boto3  # noqa: PLC0415

        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return session.client("s3", region_name=self._region)

    async def is_available(self) -> bool:
        """True if the AWS credential chain resolves AND an S3 bucket is set
        (cached, off-loop). Video needs the bucket for Nova Reel output."""
        return bool(self._s3_bucket) and await _creds_ok(self._region, self._profile)

    async def list_models(self) -> list[VideoGenModel]:
        return list(_VIDEO_MODELS)

    def _generate_sync(self, prompt: str, model: str, duration_seconds: float) -> str:
        """Blocking submit → poll → download. Returns local file path to the MP4."""
        if not self._s3_bucket:
            raise VideoGenError(
                "Bedrock video generation requires an S3 bucket. Set 'video_s3_bucket' in "
                "the Bedrock provider config or set the BEDROCK_VIDEO_S3_BUCKET environment variable."
            )

        client = self._get_runtime_client()
        model_id = model or "amazon.nova-reel-v1:1"
        duration = max(6, min(int(duration_seconds), 6))  # Nova Reel supports 6s clips

        s3_prefix = f"bedrock-video/{int(_time.time())}/"
        s3_uri = f"s3://{self._s3_bucket}/{s3_prefix}"

        model_input = {
            "taskType": "TEXT_VIDEO",
            "textToVideoParams": {"text": prompt},
            "videoGenerationConfig": {
                "durationSeconds": duration,
                "fps": 24,
                "dimension": "1280x720",
            },
        }

        # Submit async invoke
        response = client.start_async_invoke(
            modelId=model_id,
            modelInput=model_input,
            outputDataConfig={"s3OutputDataConfig": {"s3Uri": s3_uri}},
        )
        invocation_arn = response["invocationArn"]

        # Poll until completed or timeout
        start = _time.monotonic()
        while (_time.monotonic() - start) < _VIDEO_POLL_TIMEOUT:
            _time.sleep(_VIDEO_POLL_INTERVAL)
            status_resp = client.get_async_invoke(invocationArn=invocation_arn)
            status = status_resp.get("status", "")
            if status == "Completed":
                break
            elif status in ("Failed", "Cancelled"):
                failure = status_resp.get("failureMessage", "Unknown error")
                raise VideoGenError(f"Bedrock video generation {status.lower()}: {failure}")
        else:
            raise VideoGenError(
                f"Bedrock video generation timed out after {_VIDEO_POLL_TIMEOUT}s."
            )

        # Download the output MP4 from S3
        s3_client = self._get_s3_client()
        # Nova Reel writes output.mp4 at the s3 prefix
        s3_key = f"{s3_prefix}output.mp4"
        local_path = os.path.join(tempfile.gettempdir(), f"bedrock_video_{int(_time.time())}.mp4")
        s3_client.download_file(self._s3_bucket, s3_key, local_path)
        return local_path

    async def generate(
        self,
        prompt: str,
        *,
        model: str = "",
        duration_seconds: float = 5.0,
        aspect_ratio: str = "",
        **opts: Any,
    ) -> list[VideoResult]:
        """Generate a video from a text prompt via Nova Reel (async invoke)."""
        try:
            local_path = await asyncio.to_thread(
                self._generate_sync, prompt, model, duration_seconds
            )
        except VideoGenError:
            raise
        except Exception as exc:
            raise VideoGenError(f"Bedrock video generation failed: {exc}") from exc

        return [VideoResult(local_path=local_path, mime="video/mp4", duration_s=6.0)]


# ── Bedrock STT Provider (Amazon Transcribe) ─────────────────────────────────

# The STT capability uses Amazon Transcribe — the purpose-built AWS speech-to-text
# service — NOT Voxtral (which is an audio-modality LLM that hallucinates).
# Voxtral is correctly classified as an audio_modality model (understands audio
# in chat context) — a different use-case from deterministic transcription.
#
# Amazon Transcribe works via a batch job: upload audio to S3 → start_transcription_job
# → poll → download transcript JSON. For short clips (< 30s, the composer mic path),
# this completes in 3-8 seconds. No hallucination, handles all formats natively.
_STT_MODELS = ["amazon-transcribe"]
_DEFAULT_STT_MODEL = "amazon-transcribe"


class BedrockSTTProvider(SttProvider):
    """Speech-to-text via Amazon Transcribe (the real AWS transcription service).

    Uploads the audio to S3, runs a Transcribe job, and returns the verbatim
    transcript. Deterministic — no hallucination, no prompt engineering needed.
    Handles wav, mp3, mp4, flac, ogg, webm natively.
    """

    def __init__(self, *, region: str = "us-east-1", profile: str | None = None,
                 name: str = "bedrock", s3_bucket: str = "") -> None:
        self._region = region
        self._profile = profile
        self._name = name
        self._s3_bucket = s3_bucket or os.environ.get("BEDROCK_VIDEO_S3_BUCKET", "")

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Amazon Transcribe"

    def _get_session(self):
        import boto3  # noqa: PLC0415
        return boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()

    async def is_available(self) -> bool:
        """True if the AWS credential chain resolves AND an S3 bucket is set."""
        return bool(self._s3_bucket) and await _creds_ok(self._region, self._profile)

    def _transcribe_sync(self, audio_path: str, model: str, language: str) -> str | None:
        """Blocking: upload → start job → poll → fetch transcript. Via to_thread."""
        import time as _time
        import urllib.request
        import uuid

        session = self._get_session()
        s3 = session.client("s3", region_name=self._region)
        transcribe = session.client("transcribe", region_name=self._region)

        # Determine media format from extension
        ext = os.path.splitext(audio_path)[1].lower().lstrip(".") or "wav"
        format_map = {"webm": "webm", "ogg": "ogg", "mp3": "mp3",
                      "mp4": "mp4", "m4a": "mp4", "flac": "flac", "wav": "wav"}
        media_format = format_map.get(ext, "wav")

        # Upload to S3
        s3_key = f"stt-transcribe/{uuid.uuid4().hex}.{ext}"
        s3.upload_file(audio_path, self._s3_bucket, s3_key)
        s3_uri = f"s3://{self._s3_bucket}/{s3_key}"

        job_name = f"pclaw-stt-{uuid.uuid4().hex[:12]}"
        try:
            # Start transcription job
            job_kwargs: dict[str, Any] = {
                "TranscriptionJobName": job_name,
                "Media": {"MediaFileUri": s3_uri},
                "MediaFormat": media_format,
            }
            if language:
                job_kwargs["LanguageCode"] = language
            else:
                job_kwargs["IdentifyLanguage"] = True

            transcribe.start_transcription_job(**job_kwargs)

            # Poll for completion (short clips finish in 3-8s)
            for _ in range(40):  # up to ~60s
                _time.sleep(1.5)
                status = transcribe.get_transcription_job(
                    TranscriptionJobName=job_name
                )
                st = status["TranscriptionJob"]["TranscriptionJobStatus"]
                if st == "COMPLETED":
                    uri = status["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
                    result = json.loads(urllib.request.urlopen(uri, timeout=10).read())
                    return result["results"]["transcripts"][0]["transcript"]
                elif st == "FAILED":
                    reason = status["TranscriptionJob"].get("FailureReason", "unknown")
                    logger.error("Amazon Transcribe job failed: %s", reason)
                    return None
            logger.error("Amazon Transcribe job timed out")
            return None
        finally:
            # Cleanup: delete S3 object + transcription job
            try:
                s3.delete_object(Bucket=self._s3_bucket, Key=s3_key)
            except Exception:
                pass
            try:
                transcribe.delete_transcription_job(TranscriptionJobName=job_name)
            except Exception:
                pass

    async def transcribe(self, audio_path: str, model: str = "", language: str = "") -> str | None:
        """Transcribe an audio file via Amazon Transcribe."""
        try:
            return await asyncio.to_thread(self._transcribe_sync, audio_path, model, language)
        except Exception:
            logger.debug("Amazon Transcribe failed", exc_info=True)
            return None

    async def transcribe_detailed(
        self,
        audio_path: str,
        *,
        model: str = "",
        language: str = "",
        bias_terms: list[str] | None = None,
    ) -> TranscriptResult | None:
        """Detailed transcription (wraps flat transcribe — no segment support)."""
        text = await self.transcribe(audio_path, model=model, language=language)
        return TranscriptResult(text=text) if text is not None else None


# ── Media-capability config scanners ─────────────────────────────────────────
#
# One Bedrock config.json entry (a single AWS profile + region) serves EVERY
# use-case Bedrock offers. Chat resolves through the LLM registry (register_type
# above). The media capabilities (embedding / image / video / STT) resolve
# through their OWN registries, which build a per-config adapter. Core knows the
# OpenAI-family built-in; Bedrock contributes its adapters via the app-owned
# ``media_scanners`` extension point — one scanner per capability, registered on
# import. Each scanner receives the config provider entries and returns a Bedrock
# adapter for each entry whose ``type`` is ``bedrock`` (or a branded alias
# collapsing to it), keyed by that entry's name so ``<name>:model`` refs resolve
# to the same AWS account that backs the entry's chat.


def _bedrock_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The config provider entries this app owns (type ``bedrock``)."""
    out = []
    for e in entries:
        ptype = str(e.get("type", ""))
        # canonical + branded-alias tolerance (the config sync may stamp _original_type)
        if ptype == "bedrock" or str((e.get("options") or {}).get("_original_type", "")) == "bedrock":
            out.append(e)
    return out


def _entry_region(e: dict[str, Any]) -> str:
    return str((e.get("options") or {}).get("region", "") or DEFAULT_REGION)


def _entry_profile(e: dict[str, Any]) -> str | None:
    p = (e.get("options") or {}).get("profile")
    return str(p) if p else None


def _scan_embedding(entries: list[dict[str, Any]]) -> list:
    # Key each adapter by the config entry name (not the generic "bedrock") so a
    # ``<name>:model`` binding resolves to the same AWS account backing the chat.
    return [
        BedrockEmbeddingProvider(
            region=_entry_region(e), profile=_entry_profile(e), name=str(e["name"]),
        )
        for e in _bedrock_entries(entries)
    ]


def _scan_image(entries: list[dict[str, Any]]) -> list:
    return [
        BedrockImageProvider(
            region=_entry_region(e), profile=_entry_profile(e), name=str(e["name"]),
        )
        for e in _bedrock_entries(entries)
    ]


def _scan_video(entries: list[dict[str, Any]]) -> list:
    out = []
    for e in _bedrock_entries(entries):
        bucket = str((e.get("options") or {}).get("video_s3_bucket", "")
                     or os.environ.get("BEDROCK_VIDEO_S3_BUCKET", ""))
        out.append(BedrockVideoProvider(
            region=_entry_region(e), profile=_entry_profile(e),
            s3_bucket=bucket, name=str(e["name"]),
        ))
    return out


def _scan_stt(entries: list[dict[str, Any]]) -> list:
    out = []
    for e in _bedrock_entries(entries):
        bucket = str((e.get("options") or {}).get("video_s3_bucket", "")
                     or os.environ.get("BEDROCK_VIDEO_S3_BUCKET", ""))
        out.append(BedrockSTTProvider(
            region=_entry_region(e), profile=_entry_profile(e),
            name=str(e["name"]), s3_bucket=bucket,
        ))
    return out


try:
    from personalclaw.sdk.model import register_scanner as _reg_scanner

    _reg_scanner("embedding", _scan_embedding)
    _reg_scanner("image_gen", _scan_image)
    _reg_scanner("video_gen", _scan_video)
    _reg_scanner("stt", _scan_stt)
except Exception:  # noqa: BLE001 — older core without the extension point
    logger.debug("media_scanners extension point unavailable", exc_info=True)
