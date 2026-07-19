"""Ollama provider — local Ollama server via the ``httpx`` HTTP client.

The ``httpx`` SDK is imported lazily inside :meth:`OllamaProvider.__init__`
to satisfy Requirement R6.5 / Property 11 (Provider SDK Lazy Import). The
module file itself is safe to import without ``httpx`` installed: only
constructing an :class:`OllamaProvider` instance triggers the SDK import.

A factory is registered with the default :class:`ProviderRegistry` on
module import — the app loader imports this module when the app is enabled,
wiring the ``ollama`` provider type without pulling the SDK into
``sys.modules``.

Ollama exposes a local HTTP server (default ``http://localhost:11434``)
with two relevant endpoints:

* ``POST /api/chat`` with ``stream=true`` returns newline-delimited JSON
  (NDJSON). Each line is one of:

  - a streaming chunk: ``{"message": {"role": "assistant", "content": "..."},
    "done": false}``
  - a final summary: ``{"done": true, "prompt_eval_count": N,
    "eval_count": M, ...}``

* ``POST /api/embed`` with ``{"model": ..., "input": [...]}`` returns
  ``{"embeddings": [[...], ...]}``.

The endpoint is overridable via ``ProviderEntry.options.endpoint``.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from personalclaw.sdk.model import (  # noqa: F401  (base)
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    CancelOutcome,
    LLMEvent,
    ModelProvider,
)
from personalclaw.sdk.model import Capability, ProviderCapability
from personalclaw.sdk.model import (  # noqa: F401  (catalog)
    ConnectionResult,
    ModelInfo,
    ModelManager,
    PullProgress,
    infer_capabilities,
)
from personalclaw.sdk.model import KIND_OUTSIDE, make_think_splitter
from personalclaw.sdk.model import Credential
from personalclaw.sdk.model import (  # noqa: F401  (registry)
    ProviderEntry,
    ProviderResolutionError,
    get_default_registry,
)

logger = logging.getLogger(__name__)

# Default endpoint when no override is supplied via entry ``options.endpoint``.
_DEFAULT_ENDPOINT = "http://localhost:11434"

# Max conversation history entries before trimming oldest. Mirrors the
# convention used by ``providers/openai.py`` and ``providers/anthropic.py``.
_MAX_HISTORY = 50

# Default request timeout (seconds). Streaming bodies are read on the
# pooled connection so the timeout applies to connect/write/pool, not to
# the total streaming duration.
_DEFAULT_TIMEOUT = 60.0


def _is_tools_unsupported_error(status_code: int, body: str) -> bool:
    """Heuristic: does this 4xx mean the model can't accept a ``tools`` schema?

    Ollama returns 400 with a message mentioning tools/function support for
    models that lack it. We match conservatively so an unrelated 400 (bad
    request, OOM) is NOT mistaken for a tools-capability problem — that would
    silently strip tools from a turn that should have kept them.
    """
    if status_code != 400:
        return False
    lowered = body.lower()
    return "tool" in lowered and (
        "support" in lowered or "does not" in lowered or "not supported" in lowered
    )


def _to_ollama_messages(messages: list[dict]) -> list[dict]:
    """Translate the loop's OpenAI-shaped history into Ollama ``/api/chat`` shape.

    The native loop emits a single canonical (OpenAI) message shape across all
    providers. Ollama's wire format differs in two ways this fixes up:

    * assistant ``tool_calls`` carry ``function.arguments`` as a JSON **object**,
      not the OpenAI JSON **string** the loop records;
    * ``role:"tool"`` results carry a ``tool_name`` (Ollama matches results to
      calls by name), which we recover from the preceding assistant turn's calls.

    Plain user / assistant text messages pass through unchanged.
    """
    id_to_name: dict[str, str] = {}
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls: list[dict] = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "")
                tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                if tc_id and name:
                    id_to_name[tc_id] = name
                calls.append({"function": {"name": name, "arguments": _as_arguments_object(fn.get("arguments"))}})
            entry: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            entry["tool_calls"] = calls
            out.append(entry)
        elif role == "tool":
            entry = {"role": "tool", "content": str(m.get("content", ""))}
            name = id_to_name.get(m.get("tool_call_id", ""))
            if name:
                entry["tool_name"] = name
            out.append(entry)
        elif isinstance(m.get("content"), list):
            # Multimodal content blocks (text + image_url data-urls), emitted by the
            # knowledge vision nodes. Ollama's /api/chat takes text in `content` and
            # images as a separate `images: [<base64>]` array (raw base64, no data-url
            # prefix) — NOT inline content blocks. Without this the list str()'s into
            # garbage and a vision model sees no image.
            text_parts, images = _split_content_blocks(m["content"])
            entry = {"role": role, "content": "\n".join(text_parts)}
            if images:
                entry["images"] = images
            out.append(entry)
        else:
            out.append(m)
    return out


def _split_content_blocks(blocks: list) -> tuple[list[str], list[str]]:
    """Split OpenAI multimodal content blocks into (text_parts, base64_images) for
    Ollama. image_url data-urls (``data:<mime>;base64,<b64>``) yield the bare base64;
    a non-data URL or unparseable block is skipped."""
    text_parts: list[str] = []
    images: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            if b:
                text_parts.append(str(b))
            continue
        if b.get("type") == "text":
            text_parts.append(str(b.get("text", "")))
        elif b.get("type") == "image_url":
            url = ((b.get("image_url") or {}) if isinstance(b.get("image_url"), dict) else {}).get("url", "")
            if isinstance(url, str) and url.startswith("data:") and "," in url:
                images.append(url.split(",", 1)[1])  # bare base64 (Ollama wants no prefix)
    return text_parts, images


def _as_arguments_object(raw: Any) -> dict:
    """Coerce tool-call ``arguments`` (a JSON string, per OpenAI shape) to a dict.

    Ollama expects the object form. Already-dict input passes through; malformed
    JSON degrades to ``{}`` so a single bad call never breaks the whole request.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _accumulate_ollama_tool_call(acc: dict[int, dict[str, Any]], idx: int, tc: dict) -> None:
    """Fold one streamed Ollama ``tool_calls`` entry into the accumulator.

    Ollama may deliver a tool call whole (in the final message) or — like the
    OpenAI delta protocol — across chunks (name first, argument fragments
    after). Object-shaped ``arguments`` replace; string fragments append, so a
    fragmented call reassembles correctly either way.
    """
    bucket = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
    if tc.get("id"):
        bucket["id"] = tc["id"]
    fn = tc.get("function", {}) or {}
    if fn.get("name"):
        bucket["name"] = fn["name"]
    args = fn.get("arguments")
    if isinstance(args, dict):
        bucket["arguments"] = json.dumps(args)
    elif isinstance(args, str) and args:
        if isinstance(bucket["arguments"], str):
            bucket["arguments"] += args
        else:
            bucket["arguments"] = args


class OllamaProvider(ModelProvider):
    """ModelProvider backed by a local Ollama HTTP server.

    The ``httpx`` SDK is imported inside ``__init__`` so the package
    ``personalclaw.providers`` can be imported without pulling the SDK into
    ``sys.modules`` (R6.5 / Property 11).
    """

    # Ollama's native ``/api/chat`` accepts a ``tools`` schema and streams back
    # ``message.tool_calls``, so the native loop can drive a tool-enabled turn
    # via complete(). Tool support is ultimately model-dependent: a model that
    # can't use tools makes Ollama 400 the request, which complete() catches and
    # transparently retries tool-less (graceful degradation), caching the result
    # on ``_tools_unsupported`` so later turns skip the failed first request.
    supports_tools: bool = True

    def __init__(
        self,
        *,
        model: str,
        credential: Credential | None = None,
        endpoint: str = _DEFAULT_ENDPOINT,
        timeout: float = _DEFAULT_TIMEOUT,
        extra_options: dict[str, object] | None = None,
    ) -> None:
        # Lazy import per R6.5 / Property 11. Do NOT lift to module top.
        import httpx  # noqa: WPS433

        # Ollama is typically unauth'd on localhost; ``credential`` is
        # accepted for parity with other providers but is not required.
        # Store it so subclasses / future remote-Ollama deployments could
        # forward a bearer token; today we ignore the secret.
        del credential

        self._httpx_module = httpx
        self._model = model
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._extra_options: dict[str, object] = dict(extra_options or {})
        self._embedding_model = str(self._extra_options.pop("embedding_model", model))
        self._client: Any = httpx.AsyncClient(base_url=self._endpoint, timeout=timeout)
        self._history: list[dict[str, Any]] = []
        self._last_context_pct: float = 0.0
        # Flipped True the first time the server rejects a tools request for
        # this model, so subsequent complete() turns skip the doomed first try.
        self._tools_unsupported: bool = False

    # ── Local-model management (the uniform download-surface contract) ─────────
    #
    # Ollama is a LOCAL downloadable model provider like faster-whisper/piper — its
    # models are pulled + managed on the user's machine. It satisfies the same contract
    # (list/download/delete + search) so the ONE download card + `/api/models/available`
    # surface it identically; the only difference is ``searchable=True`` (its catalog is
    # the ollama.com library, discovered by a search term, not a fixed list). Management
    # delegates to :class:`OllamaCatalog` (the ModelManager); download drains the
    # streaming ``pull`` into a bool for the shared byte-progress job runner.
    searchable: bool = True

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def display_name(self) -> str:
        return "Ollama"

    def _catalog(self) -> "OllamaCatalog":
        return OllamaCatalog(endpoint=self._endpoint)

    async def is_available(self) -> bool:
        return (await self._catalog().test_connection()).ok

    @staticmethod
    def _to_local(mi: "ModelInfo") -> "LocalModel":
        from personalclaw.sdk.local_model import LocalModel
        return LocalModel(
            name=mi.name,
            size_mb=round((mi.size or 0) / (1024 * 1024), 1),
            description=mi.extra.get("parameter_size", "") or mi.description,
            downloaded=True if mi.downloaded is None else bool(mi.downloaded),
            capabilities=list(mi.capabilities),
            source="ollama.com",
        )

    async def list_models(self) -> list["LocalModel"]:
        """Locally-installed ollama models (downloaded=True — these are pulled)."""
        return [self._to_local(mi) for mi in await self._catalog().list_models()]

    async def search_models(self, query: str) -> list["LocalModel"]:
        """Search the ollama.com library for installable models (the searchable axis)."""
        out = []
        for mi in await self._catalog().search_catalog(query):
            lm = self._to_local(mi)
            lm.downloaded = False  # search results are remote/installable, not local
            out.append(lm)
        return out

    async def download_model(self, model_name: str) -> bool:
        """Pull a model, draining the streaming progress into a success bool for the
        shared download-job runner. Byte-progress is tracked by on-disk polling."""
        try:
            async for frame in self._catalog().pull_model(model_name):
                if frame.error:
                    logger.warning("Ollama pull %s failed: %s", model_name, frame.error)
                    return False
            return True
        except Exception:
            logger.warning("Ollama pull %s raised", model_name, exc_info=True)
            return False

    async def delete_model(self, model_name: str) -> bool:
        try:
            await self._catalog().delete_model(model_name)
            return True
        except Exception:
            logger.warning("Ollama delete %s raised", model_name, exc_info=True)
            return False

    def cache_dir(self) -> str | None:
        # Ollama manages its own model store (~/.ollama/models); byte-progress falls
        # back to indeterminate rather than pointing at a dir core doesn't own.
        return None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the provider; auto-detect model from Ollama if not specified."""
        if not self._model or self._model == "*":
            try:
                resp = await self._client.get("/api/tags")
                resp.raise_for_status()
                models = resp.json().get("models", [])
                if models:
                    self._model = models[0]["name"]
                    if not self._embedding_model:
                        self._embedding_model = self._model
                    logger.info("Ollama auto-detected model: %s", self._model)
                else:
                    logger.warning("Ollama has no models available at %s", self._endpoint)
            except Exception:
                logger.warning("Failed to auto-detect Ollama model", exc_info=True)
        logger.info("Ollama provider ready: model=%s endpoint=%s", self._model, self._endpoint)

    async def shutdown(self) -> None:
        """Close the underlying HTTP client and clear conversation history."""
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover — defensive
            logger.warning("Ollama client aclose raised", exc_info=True)
        self._history.clear()

    # ── Streaming ─────────────────────────────────────────────────────

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        """Stream a chat turn; translate NDJSON lines to :class:`LLMEvent`.

        POSTs to ``/api/chat`` with ``stream=true`` and reads the response
        body as newline-delimited JSON. Each non-final chunk's
        ``message.content`` becomes an :data:`EVENT_TEXT_CHUNK`; the final
        ``{"done": true, ...}`` chunk produces an :data:`EVENT_COMPLETE`
        event populated with ``prompt_eval_count`` / ``eval_count``.
        """
        self._history.append({"role": "user", "content": message})
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]

        body: dict[str, Any] = {
            "model": self._model,
            "messages": self._history,
            "stream": True,
        }
        # Allow ``options`` (Ollama temperature / num_ctx / etc.), ``tools``,
        # and other top-level fields to flow through unchanged.
        for key, value in self._extra_options.items():
            body.setdefault(key, value)

        assistant_text = ""
        # Self-gating inline <think> splitter (see openai.py stream()).
        splitter = make_think_splitter()
        input_tokens = 0
        output_tokens = 0

        async with self._client.stream("POST", "/api/chat", json=body) as response:
            if response.status_code >= 400:
                err_body = await response.aread()
                logger.error("Ollama %d: %s (model=%r, msg_count=%d)", response.status_code, err_body.decode(errors="replace")[:200], self._model, len(self._history))
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Ollama stream returned non-JSON line: %r", line[:200])
                    continue

                msg = chunk.get("message") or {}
                text_delta = msg.get("content") or ""
                if text_delta:
                    for seg in splitter.feed(text_delta):
                        if seg.kind == KIND_OUTSIDE:
                            assistant_text += seg.text
                            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=seg.text)
                        else:
                            yield LLMEvent(kind=EVENT_THINKING_CHUNK, text=seg.text)

                if chunk.get("done"):
                    input_tokens = int(chunk.get("prompt_eval_count", 0) or 0)
                    output_tokens = int(chunk.get("eval_count", 0) or 0)
                    break

        for seg in splitter.flush():
            if seg.kind == KIND_OUTSIDE:
                assistant_text += seg.text
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=seg.text)
            else:
                yield LLMEvent(kind=EVENT_THINKING_CHUNK, text=seg.text)

        if assistant_text:
            self._history.append({"role": "assistant", "content": assistant_text})

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
        reasoning_effort: str = "",  # accepted for interface parity; Ollama has no effort axis
    ) -> AsyncIterator[LLMEvent]:
        """Stream a stateless multi-message chat turn, tools included.

        Unlike :meth:`stream`, this NEVER touches ``self._history`` — the
        native loop owns conversation state and passes the full ``messages``
        list (OpenAI-shaped: user / assistant-with-``tool_calls`` /
        ``role:"tool"`` results). Those are translated to Ollama's ``/api/chat``
        shape by :func:`_to_ollama_messages`.

        When ``tools`` is supplied (and the model hasn't already refused them
        this session), the OpenAI-shaped tool schema is forwarded; Ollama
        streams any tool intent back as ``message.tool_calls`` (arguments as a
        JSON object), which we emit as :data:`EVENT_TOOL_CALL`. A model that
        can't use tools makes the server 400 the request — we catch that once,
        cache it on ``_tools_unsupported``, and retry tool-less so the turn
        still completes.
        """
        send_tools = bool(tools) and not self._tools_unsupported
        ollama_messages = _to_ollama_messages(messages)

        body: dict[str, Any] = {
            "model": model or self._model,
            "messages": ollama_messages,
            "stream": True,
        }
        if send_tools:
            body["tools"] = tools
        # Allow ``options`` (temperature / num_ctx / etc.) to flow through.
        for key, value in self._extra_options.items():
            body.setdefault(key, value)

        input_tokens = 0
        output_tokens = 0
        # Accumulate tool calls by index — Ollama may stream them across chunks
        # (name first, then argument fragments) like the OpenAI delta protocol,
        # or deliver a complete list in the final message. Either way we emit
        # one EVENT_TOOL_CALL per call after the stream ends.
        tool_calls: dict[int, dict[str, Any]] = {}
        # Self-gating inline <think> splitter (see openai.py stream()).
        splitter = make_think_splitter()

        async with self._client.stream("POST", "/api/chat", json=body) as response:
            if response.status_code >= 400:
                err_body = await response.aread()
                err_text = err_body.decode(errors="replace")
                # A model that can't use tools rejects the tools request. Retry
                # once without tools so the turn still completes, and remember
                # it so later turns don't pay the failed round-trip again.
                if send_tools and _is_tools_unsupported_error(response.status_code, err_text):
                    logger.info(
                        "Ollama model %r does not support tools; retrying tool-less",
                        model or self._model,
                    )
                    self._tools_unsupported = True
                    async for ev in self.complete(messages, tools=None, model=model):
                        yield ev
                    return
                logger.error(
                    "Ollama %d: %s (model=%r, msg_count=%d)",
                    response.status_code,
                    err_text[:200],
                    model or self._model,
                    len(messages),
                )
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Ollama stream returned non-JSON line: %r", line[:200])
                    continue

                msg = chunk.get("message") or {}
                text_delta = msg.get("content") or ""
                if text_delta:
                    for seg in splitter.feed(text_delta):
                        yield LLMEvent(
                            kind=EVENT_TEXT_CHUNK if seg.kind == KIND_OUTSIDE else EVENT_THINKING_CHUNK,
                            text=seg.text,
                        )

                for idx, tc in enumerate(msg.get("tool_calls") or []):
                    _accumulate_ollama_tool_call(tool_calls, idx, tc)

                if chunk.get("done"):
                    input_tokens = int(chunk.get("prompt_eval_count", 0) or 0)
                    output_tokens = int(chunk.get("eval_count", 0) or 0)
                    break

        for seg in splitter.flush():
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK if seg.kind == KIND_OUTSIDE else EVENT_THINKING_CHUNK,
                text=seg.text,
            )

        for idx in sorted(tool_calls):
            bucket = tool_calls[idx]
            name = bucket.get("name") or ""
            if not name:
                continue
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=bucket.get("id") or f"call-{idx}",
                title=name,
                tool_input=bucket.get("arguments", ""),
            )

        yield LLMEvent(
            kind=EVENT_COMPLETE,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_usage_pct=self._last_context_pct,
            cost_usd=0.0,
        )

    # ── Embeddings ────────────────────────────────────────────────────

    async def embed(self, inputs: list[str]) -> list[list[float]]:
        """Return embedding vectors for ``inputs`` via ``POST /api/embed``.

        The model defaults to the chat model and can be overridden via
        the ``embedding_model`` key in ``extra_options``.
        """
        if not inputs:
            return []
        response = await self._client.post(
            "/api/embed",
            json={"model": self._embedding_model, "input": inputs},
        )
        response.raise_for_status()
        payload = response.json()
        embeddings = payload.get("embeddings") or []
        return [list(vec) for vec in embeddings]

    # ── Tool approval (no-op) ─────────────────────────────────────────

    async def approve_tool(self, request_id: str | int) -> None:
        """No-op: Ollama tool calls are not interactive at this layer."""
        return None

    async def reject_tool(self, request_id: str | int) -> None:
        """No-op: Ollama tool calls are not interactive at this layer."""
        return None

    # ── Status ────────────────────────────────────────────────────────

    def context_usage_pct(self) -> float:
        return self._last_context_pct

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        """Cancel is a no-op for now; later phases can wire abort plumbing."""
        return "no_turn"


# ── Capability descriptor ────────────────────────────────────────────────
#
# Per design § A.5, Ollama supports streaming and embeddings fully. Tools are
# advertised here (Ollama's /api/chat forwards a tools schema and streams back
# tool_calls) but remain model-dependent: complete() degrades to a tool-less
# turn when a given model rejects the tools request. Vision stays partial, so
# the descriptor leaves it off; deployments pin a known-capable model and
# declare extra capabilities on the entry.
OLLAMA_CAPABILITY = ProviderCapability(
    type="ollama",
    capabilities=frozenset(
        {
            Capability.CHAT,
            Capability.CODE_TOOLS,
            Capability.SUMMARIZATION,
            Capability.STREAMING,
            Capability.EMBEDDING,
            Capability.VISION,
        }
    ),
    supports_streaming=True,
    supports_tools=True,
    supports_embeddings=True,
    # Vision is model-dependent (moondream/llava/qwen-vl yes, llama3/mistral no). The
    # TYPE advertises VISION so a vision model bound in Settings→Models resolves + can
    # receive images (_to_ollama_messages emits the `images` array); picking a text-only
    # model for a vision role is a user choice the model itself will no-op on.
    supports_vision=True,
    max_context_tokens=0,  # model-dependent
    notes="Local Ollama server via httpx; tool use degrades per model, vision model-dependent.",
)


# ── Factory ──────────────────────────────────────────────────────────────


def _factory(
    *,
    entry: ProviderEntry,
    session_key: str | None = None,
    **kwargs: object,
) -> ModelProvider:
    """Construct an :class:`OllamaProvider` from a :class:`ProviderEntry`.

    ``session_key`` is accepted for parity with the registry contract but
    Ollama is stateless — we ignore it.

    Ollama typically runs unauth'd on localhost, so a credential is
    optional. If the entry declares one, it is resolved through the
    optional ``credential_store`` keyword forwarded by
    :meth:`ProviderRegistry.build` and stashed on the provider for
    forward compatibility (e.g. a remote Ollama behind a reverse proxy).
    """
    del session_key  # unused — Ollama provider is stateless.

    cred: Credential | None = None
    if entry.credential:
        store = kwargs.get("credential_store")
        if store is not None:
            # ``CredentialStore`` is duck-typed here to keep the registry
            # from depending on a concrete class.
            cred = store.resolve(entry.credential)  # type: ignore[attr-defined]

    options = dict(entry.options or {})
    endpoint_value = options.pop("endpoint", None)
    endpoint = str(endpoint_value) if endpoint_value is not None else _DEFAULT_ENDPOINT
    timeout_value = options.pop("timeout", None)
    timeout = float(timeout_value) if isinstance(timeout_value, (int, float)) else _DEFAULT_TIMEOUT

    # A ``model`` kwarg (threaded by ``registry.build(name, model=…)``) overrides the
    # entry's pinned model — a per-use-case caller (e.g. one_shot_completion's
    # reasoning axis) must be able to pin the active model, or it would silently use
    # the entry default.
    _model_override = kwargs.get("model")
    model = str(_model_override) if _model_override else entry.model

    # The embedding use-case binding arrives as a build kwarg — the embedder
    # constructs its provider WITH the bound model (embed() takes no per-call model).
    _emb_model = kwargs.get("embedding_model")
    if _emb_model:
        options["embedding_model"] = str(_emb_model)

    logger.debug("Ollama factory: model=%r endpoint=%r", model, endpoint)
    return OllamaProvider(
        model=model,
        credential=cred,
        endpoint=endpoint,
        timeout=timeout,
        extra_options=options,
    )


def create_provider(config: dict | None = None) -> "OllamaProvider":
    """Extension factory: build an OllamaProvider from a settings dict.

    Used by the bundled ``ollama-models`` model extension. Chat and embedding
    both run through this single provider — embedding uses the ``embedding_model``
    option, chat uses ``default_model``.
    """
    cfg = dict(config or {})
    endpoint = str(cfg.get("endpoint") or _DEFAULT_ENDPOINT)
    timeout_value = cfg.get("timeout_secs")
    timeout = float(timeout_value) if isinstance(timeout_value, (int, float)) else _DEFAULT_TIMEOUT
    extra: dict[str, object] = {}
    if cfg.get("embedding_model"):
        extra["embedding_model"] = cfg["embedding_model"]
    return OllamaProvider(
        model=str(cfg.get("default_model") or ""),
        endpoint=endpoint,
        timeout=timeout,
        extra_options=extra,
    )


# ── Catalog / management (the reference ModelManager implementer) ──────────
#
# Ollama owns local model lifecycle (list/pull/delete/show + remote-catalog
# search), so it implements the full ModelManager axis. Core registers this via
# the same register_catalog seam an installed app uses — ollama is core-native
# only in that this module is eager-imported, not architecturally special. The
# HTTP handlers call these methods generically; no handler names "ollama".

_CATALOG_TIMEOUT = 10  # seconds — list/connectivity probes
_SHOW_TIMEOUT = 15
_DELETE_TIMEOUT = 30
_PULL_TIMEOUT = 1800  # a large model download can take a long time
_SEARCH_UA = "Mozilla/5.0 (compatible; PersonalClaw/1.0)"


class OllamaCatalog(ModelManager):
    """Discovery + full local model management for an Ollama endpoint.

    Pure function of the entry's ``endpoint`` option — never opens a chat
    session. All network calls are fail-soft for the read paths (list/search
    return ``[]`` on error); the write paths (pull/delete/show) surface errors so
    the UI can report them.
    """

    def __init__(self, endpoint: str = _DEFAULT_ENDPOINT) -> None:
        self._endpoint = (endpoint or _DEFAULT_ENDPOINT).rstrip("/")

    # ── Discovery ──────────────────────────────────────────────────────
    async def list_models(self) -> list[ModelInfo]:
        """List locally-installed models via ``GET /api/tags``."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._endpoint}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=_CATALOG_TIMEOUT),
                ) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()
        except Exception:  # noqa: BLE001 — discovery is fail-soft
            return []

        out: list[ModelInfo] = []
        for m in data.get("models", []):
            name = m.get("name", "")
            if not name:
                continue
            details = m.get("details", {}) if isinstance(m.get("details"), dict) else {}
            families = details.get("families") or []
            size = m.get("size", 0)
            out.append(ModelInfo(
                id=name,
                name=name,
                capabilities=infer_capabilities(name, families),
                size=size or None,
                extra={
                    k: v for k, v in {
                        "size_human": _humanize_bytes(size),
                        "modified_at": m.get("modified_at", ""),
                        "parameter_size": details.get("parameter_size", ""),
                        "quantization": details.get("quantization_level", ""),
                        "family": details.get("family", ""),
                    }.items() if v
                },
            ))
        return out

    async def test_connection(self) -> ConnectionResult:
        """Probe reachability via ``/api/tags`` (a cheap local call)."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._endpoint}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=_CATALOG_TIMEOUT),
                ) as r:
                    if r.status != 200:
                        return ConnectionResult(ok=False, detail=f"Ollama returned {r.status}")
                    data = await r.json()
        except Exception as exc:  # noqa: BLE001
            return ConnectionResult(ok=False, detail=str(exc)[:200])
        return ConnectionResult(ok=True, model_count=len(data.get("models", [])))

    # ── Management ─────────────────────────────────────────────────────
    async def search_catalog(self, query: str) -> list[ModelInfo]:
        """Search the ollama.com library for installable models (+ their tags).

        Scrapes the public search + per-model tags pages (Ollama exposes no
        JSON search API). Returns up to 20 ``name:tag`` candidates. Fail-soft.
        """
        import re as _re
        import asyncio as _asyncio
        import aiohttp

        q = (query or "").strip()
        if not q:
            return []
        # ollama.com/search indexes by model BASE NAME (``qwen2.5``), not ``name:tag``.
        # A user naturally types the full tag they want (``qwen2.5:0.5b``) → strip the
        # ``:tag`` so the search matches; the exact tag still surfaces via the per-model
        # tags enumeration below. Without this, a full-tag query returns zero results.
        search_q = q.split(":", 1)[0]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://ollama.com/search",
                    params={"q": search_q},
                    timeout=aiohttp.ClientTimeout(total=_CATALOG_TIMEOUT),
                    headers={"User-Agent": _SEARCH_UA},
                ) as r:
                    if r.status != 200:
                        return []
                    html = await r.text()
        except Exception:  # noqa: BLE001
            return []

        seen: set[str] = set()
        unique_models: list[str] = []
        for model_name in _re.findall(r'href="/library/([^"]+)"', html):
            if model_name in seen:
                continue
            seen.add(model_name)
            unique_models.append(model_name)
            if len(unique_models) >= 8:
                break

        async def _fetch_tags(sess: Any, base_name: str) -> list[str]:
            try:
                async with sess.get(
                    f"https://ollama.com/library/{base_name}/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                    headers={"User-Agent": _SEARCH_UA},
                ) as tr:
                    if tr.status != 200:
                        return ["latest"]
                    tag_html = await tr.text()
                    tags = _re.findall(rf'href="/library/{_re.escape(base_name)}:([^"]+)"', tag_html)
                    seen_tags: set[str] = set()
                    unique_tags: list[str] = []
                    for t in tags:
                        if t not in seen_tags:
                            seen_tags.add(t)
                            unique_tags.append(t)
                    return unique_tags[:5] if unique_tags else ["latest"]
            except Exception:  # noqa: BLE001
                return ["latest"]

        async with aiohttp.ClientSession() as sess:
            tag_results = await _asyncio.gather(*[_fetch_tags(sess, m) for m in unique_models])

        out: list[ModelInfo] = []
        for base_name, tags in zip(unique_models, tag_results):
            for tag in tags:
                full = f"{base_name}:{tag}"
                out.append(ModelInfo(id=full, name=full, capabilities=infer_capabilities(full)))
                if len(out) >= 20:
                    return out
        return out

    async def pull_model(self, model_id: str) -> AsyncIterator[PullProgress]:
        """Download a model via ``POST /api/pull`` (stream=true), yielding
        progress frames. Raises up front for a bad request; a mid-stream failure
        yields a terminal :class:`PullProgress` with ``error`` set.

        The generator closes its upstream connection when the consumer stops
        iterating (GeneratorExit) — that is what cancels the Ollama-side download.
        """
        import aiohttp

        model = (model_id or "").strip()
        if not model:
            raise ValueError("model is required")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._endpoint}/api/pull",
                json={"name": model, "stream": True},
                timeout=aiohttp.ClientTimeout(total=_PULL_TIMEOUT),
            ) as r:
                if r.status != 200:
                    detail = (await r.text())[:200]
                    yield PullProgress(status="", error=f"Ollama pull returned {r.status}: {detail}")
                    return
                async for line in r.content:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if frame.get("error"):
                        yield PullProgress(status="", error=str(frame["error"])[:200])
                        return
                    yield PullProgress(
                        status=str(frame.get("status", "")),
                        completed=frame.get("completed"),
                        total=frame.get("total"),
                        digest=str(frame.get("digest", "") or ""),
                    )

    async def delete_model(self, model_id: str) -> None:
        """Delete a local model via ``DELETE /api/delete``. Raises on failure."""
        import aiohttp

        model = (model_id or "").strip()
        if not model:
            raise ValueError("model is required")
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{self._endpoint}/api/delete",
                json={"name": model},
                timeout=aiohttp.ClientTimeout(total=_DELETE_TIMEOUT),
            ) as r:
                if r.status != 200:
                    text = (await r.text())[:200]
                    raise RuntimeError(f"Ollama returned {r.status}: {text}")

    async def show_model(self, model_id: str) -> ModelInfo:
        """Return rich metadata via ``POST /api/show``. Raises on failure.

        Maps Ollama's ``details`` + ``model_info`` into a :class:`ModelInfo` whose
        ``extra`` carries the decision-relevant fields the UI renders
        (family / parameter_size / quantization / format / context_length /
        capabilities / license_short)."""
        import aiohttp

        model = (model_id or "").strip()
        if not model:
            raise ValueError("model is required")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._endpoint}/api/show", json={"name": model},
                timeout=aiohttp.ClientTimeout(total=_SHOW_TIMEOUT),
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"Ollama returned {r.status}")
                data = await r.json()

        details = data.get("details", {}) if isinstance(data, dict) else {}
        model_info = data.get("model_info", {}) if isinstance(data, dict) else {}
        ctx = next((v for k, v in model_info.items() if k.endswith(".context_length")), None)
        license_raw = str(data.get("license", "") or "")
        # ``capabilities`` here is Ollama's OWN self-reported list (completion/tools/
        # vision/…) surfaced verbatim for the detail view — NOT the inferred
        # chat/embedding tags. Carry it in extra so the /show response keeps its
        # historical shape; ModelInfo.capabilities stays the inferred tag set.
        extra = {
            k: v for k, v in {
                "family": details.get("family", ""),
                "parameter_size": details.get("parameter_size", ""),
                "quantization": details.get("quantization_level", ""),
                "format": details.get("format", ""),
                "context_length": ctx or 0,
                "capabilities": data.get("capabilities", []) if isinstance(data, dict) else [],
                "license_short": license_raw.splitlines()[0][:80] if license_raw else "",
            }.items() if v
        }
        return ModelInfo(id=model, name=model, capabilities=[], extra=extra)


def _humanize_bytes(n: int) -> str:
    """Bytes → a compact human size (e.g. ``4.7 GB``). 0/negative → ``""``."""
    if not n or n < 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def create_catalog(options: dict | None = None, *, model: str = "") -> OllamaCatalog:
    """Catalog factory (registry contract): build an OllamaCatalog from an entry's
    options bag. Only ``endpoint`` is relevant for discovery/management."""
    del model  # unused — model lifecycle is endpoint-scoped, not per pinned model
    endpoint = str((options or {}).get("endpoint") or _DEFAULT_ENDPOINT)
    return OllamaCatalog(endpoint=endpoint)


# ── Registration ─────────────────────────────────────────────────────────
#
# Register on import — the app loader imports this module when the app is
# enabled, wiring the type into the default registry. The ``try``/``except``
# makes the registration idempotent against module reload in tests; the
# registry itself remains strict and rejects duplicate types in normal use.
try:
    get_default_registry().register_type(OLLAMA_CAPABILITY, _factory)
except ProviderResolutionError:
    logger.debug("ollama provider type already registered with default registry")

# The catalog axis (register_catalog is idempotent — last wins — so no guard).
get_default_registry().register_catalog("ollama", create_catalog)
