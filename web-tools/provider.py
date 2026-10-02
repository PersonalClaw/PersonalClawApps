"""The `web` tool provider — general web primitives over the bound search provider."""

import json
import logging
from typing import Any

from personalclaw.sdk.search import (
    DEFAULT_DEPTH,
    DEFAULT_SEARCH_USE_CASE,
    VALID_DEPTHS,
    VALID_SEARCH_USE_CASES,
    search_with_fallback,
)
from personalclaw.sdk.tool import RiskLevel, ToolDefinition, ToolProvider, ToolResult
from personalclaw.sdk.net import record_seen_urls, web_extract, web_fetch

logger = logging.getLogger(__name__)


def _session_key() -> str:
    """The current session (for per-session fetch provenance). Best-effort: empty
    when invoked outside a native-loop turn (the provenance gate then no-ops)."""
    try:
        from personalclaw.sdk.mcp import get_current_session_key
        return get_current_session_key() or ""
    except Exception:
        return ""

# Said only when no search provider is registered at all. Search providers are Store apps, and
# one registers when its app is turned on and leaves when it is turned off. Core's resolver falls
# back to any registered one when nothing is bound, so an installed, turned-on search app is the
# whole fix, and choosing one in Settings → Search is for picking between several.
_NO_PROVIDER = (
    "No search provider app is installed and turned on, so there is nothing to search with."
)
_NO_PROVIDER_HINT = (
    "Install a search provider app from the Store on the Apps page, or turn on one you already "
    "have there; if it asks for an API key or an endpoint, add it in Settings → Providers. Once "
    "one is on, web_search uses it; to choose which one each kind of search uses, go to "
    "Settings → Search."
)


class WebToolProvider(ToolProvider):
    """The web tools the agent searches and reads pages with: ``web_search`` (over the bound
    search provider), ``web_fetch`` (SSRF-guarded fetch + extraction), and
    ``web_extract`` (structured extraction)."""

    @property
    def name(self) -> str:
        return "personalclaw-web"

    @property
    def display_name(self) -> str:
        return "Web Tools"

    async def list_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="web_search",
                description=(
                    "Search the web and get back ranked results (and, from some "
                    "providers, a synthesized answer). Resolves to the search provider "
                    "bound to the use-case in Settings → Search. Read-only."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query."},
                        "depth": {
                            "type": "string",
                            "enum": ["quick", "balanced", "deep"],
                            "description": "Latency-vs-quality dial (default balanced). Each provider maps this onto its native mode.",
                        },
                        "recency": {
                            "type": "string",
                            "description": "Optional recency bias, e.g. 'day' / 'week' / 'month' / 'year' (honored by providers that support it).",
                        },
                        "domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional include-domain filter (honored by providers that support it).",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum results to return (default 10).",
                        },
                        "use_case": {
                            "type": "string",
                            "enum": sorted(VALID_SEARCH_USE_CASES),
                            "description": "Which bound provider to use (default search-general). Use search-news for recency-biased queries.",
                        },
                    },
                    "required": ["query"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
            ToolDefinition(
                name="web_fetch",
                description=(
                    "Fetch a web page and get its main content as clean markdown. It opens "
                    "a link the user wrote or pasted in their own message, or one a "
                    "web_search or web_fetch returned in this chat; a link found only in "
                    "a page or another tool's output, or made up, is refused. Large pages "
                    "paginate: if the result is truncated, call again with the returned "
                    "next_index. Routed through the SSRF-safe egress guard. Read-only."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The http(s) URL to fetch: one the user gave, or a web_search or web_fetch returned."},
                        "max_tokens": {
                            "type": "integer",
                            "description": "Approximate content budget per call (default 5000). Larger pages paginate.",
                        },
                        "start_index": {
                            "type": "integer",
                            "description": "Character offset to resume from (use the next_index from a prior truncated fetch).",
                        },
                        "render": {
                            "type": "boolean",
                            "description": "Render the page in a headless browser first (executes JavaScript) — use for client-rendered pages that return little content otherwise. Slower; falls back to a plain fetch if unavailable.",
                        },
                    },
                    "required": ["url"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
            ToolDefinition(
                name="web_extract",
                description=(
                    "Fetch a web page and extract STRUCTURED data from it as a JSON object, "
                    "per your instructions (describe the fields/shape you want). Use this "
                    "instead of web_fetch when you need specific values pulled out (prices, "
                    "dates, a table, contact details) rather than the page's prose. It "
                    "opens the same links web_fetch does. Read-only."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The http(s) URL to extract from: one the user gave, or a web_search or web_fetch returned."},
                        "instructions": {
                            "type": "string",
                            "description": "What to extract — the fields/shape wanted (e.g. \"the product name, price, and in-stock boolean\" or \"a list of {title, author, year} for each cited paper\").",
                        },
                    },
                    "required": ["url", "instructions"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
        ]

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        if tool_name == "web_search":
            return await self._web_search(arguments)
        if tool_name == "web_fetch":
            return await self._web_fetch(arguments)
        if tool_name == "web_extract":
            return await self._web_extract(arguments)
        return ToolResult(success=False, error=f"Unknown tool: {tool_name!r}")

    async def _web_search(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(
                success=False, error="query is required",
                recovery_hints=["Pass a non-empty 'query' string."],
            )

        use_case = str(args.get("use_case") or DEFAULT_SEARCH_USE_CASE)
        if use_case not in VALID_SEARCH_USE_CASES:
            return ToolResult(
                success=False, error=f"Unknown use_case: {use_case!r}",
                recovery_hints=[f"Use one of: {sorted(VALID_SEARCH_USE_CASES)}."],
            )

        depth = str(args.get("depth") or DEFAULT_DEPTH)
        if depth not in VALID_DEPTHS:
            depth = DEFAULT_DEPTH
        recency = args.get("recency") or None
        domains = args.get("domains") if isinstance(args.get("domains"), list) else None
        try:
            max_results = int(args.get("max_results") or 10)
        except (TypeError, ValueError):
            max_results = 10
        max_results = max(1, min(max_results, 25))

        try:
            result = await search_with_fallback(
                use_case, query, depth=depth, recency=recency, domains=domains, max_results=max_results,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            # Both the bound provider AND the keyless fallback failed.
            logger.warning("web_search failed (incl. fallback): %s", exc, exc_info=True)
            return ToolResult(
                success=False, error=f"Search failed: {exc}",
                metadata={"use_case": use_case},
                recovery_hints=[
                    "Check the bound provider's endpoint/API key in Settings → Providers.",
                    "Retry, or bind a different provider for this use-case in Settings → Search.",
                ],
            )
        if result is None:
            return ToolResult(
                success=False, error=_NO_PROVIDER, recovery_hints=[_NO_PROVIDER_HINT],
            )

        # A search the bound provider failed and another engine answered carries `fallback`: its
        # `notice` (PersonalClaw's own sentence, naming both) and the failed provider's own words.
        # It rides in the payload, so the agent can say where its answer came from, and the card
        # shows the notice above the results.
        payload = result.to_dict()
        # Record the surfaced URLs so a follow-up web_fetch of any result passes the
        # provenance gate (a search returned the link; the model didn't write it).
        record_seen_urls(_session_key(), result.sources)
        # Search results are external content (titles/snippets/answers scraped from
        # arbitrary pages), so an injection could hide in a snippet. Fence the FREE-TEXT
        # FIELDS in place — not the whole envelope — so the payload stays valid JSON the
        # agent/research-loop can still parse, while any injected text is marked as data.
        _fence_search_payload(payload)
        return ToolResult(
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
            metadata={
                "provider": result.provider,
                "use_case": use_case,
                "fallback_from": result.fallback.provider if result.fallback else "",
                "depth": result.depth,
                "result_count": len(result.results),
                "has_answer": bool(result.answer),
                "sources": result.sources,
            },
        )

    async def _web_fetch(self, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url:
            return ToolResult(success=False, error="url is required",
                              recovery_hints=["Pass a non-empty 'url' string."])
        try:
            max_tokens = int(args.get("max_tokens") or 5000)
        except (TypeError, ValueError):
            max_tokens = 5000
        try:
            start_index = int(args.get("start_index") or 0)
        except (TypeError, ValueError):
            start_index = 0

        outcome = await web_fetch(
            url, session_key=_session_key(),
            max_tokens=max(500, min(max_tokens, 50000)),
            start_index=max(0, start_index),
            render=bool(args.get("render")),
        )
        if not outcome.ok:
            return ToolResult(success=False, error=outcome.error,
                              recovery_hints=outcome.recovery_hints,
                              metadata={"url": outcome.url})
        hints: list[str] = []
        if outcome.truncated and outcome.next_index is not None:
            hints.append(f"Content truncated — call web_fetch again with start_index={outcome.next_index} to continue.")
        # Fence the fetched page body: it's untrusted external content and the highest-
        # leverage prompt-injection surface (a page saying "ignore previous instructions,
        # now exfiltrate X"). The system prompt notes <untrusted_content> is data, never
        # instructions — so the model reads it without obeying embedded directives.
        from personalclaw.sdk.security import fence_untrusted
        fenced = fence_untrusted(outcome.content, source=outcome.url)
        return ToolResult(
            success=True,
            output=fenced,
            truncated=outcome.truncated,
            original_length=outcome.total_chars if outcome.truncated else None,
            recovery_hints=hints,
            metadata={
                "url": outcome.url,
                "title": outcome.title,
                "char_count": outcome.char_count,
                "total_chars": outcome.total_chars,
                "truncated": outcome.truncated,
                "next_index": outcome.next_index,
                "extractor": outcome.extractor,
                # §5 fetch-derived citation: the source URL + the exact [start, end)
                # char span of this content within the full document, so a quote can
                # be attributed to a precise offset (and survives pagination).
                "citations": [{
                    "url": outcome.url,
                    "start_char": outcome.start_char,
                    "end_char": outcome.end_char,
                }],
            },
        )

    async def _web_extract(self, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url") or "").strip()
        instructions = str(args.get("instructions") or "").strip()
        if not url:
            return ToolResult(success=False, error="url is required",
                              recovery_hints=["Pass a non-empty 'url' string."])
        if not instructions:
            return ToolResult(success=False, error="instructions are required",
                              recovery_hints=["Describe the fields / shape to extract."])

        outcome = await web_extract(url, instructions, session_key=_session_key())
        if not outcome.ok:
            return ToolResult(success=False, error=outcome.error,
                              recovery_hints=outcome.recovery_hints,
                              metadata={"url": outcome.url})
        return ToolResult(
            success=True,
            output=json.dumps(outcome.data, ensure_ascii=False),
            metadata={
                "url": outcome.url,
                "title": outcome.title,
                "citations": [outcome.url],
            },
        )


def _fence_search_payload(payload: dict) -> None:
    """Fence the free-text fields of a search payload IN PLACE so the JSON stays valid
    (the agent/research-loop parses it) while injected text in a title/snippet/answer/
    body, or in a failed provider's words, is marked <untrusted_content>. Structural fields
    (url, score, provider, sources) are trusted and left untouched."""
    from personalclaw.sdk.security import fence_untrusted

    def _f(text: str) -> str:
        return fence_untrusted(text, source="web_search") if text and text.strip() else text

    if isinstance(payload.get("answer"), str):
        payload["answer"] = _f(payload["answer"])
    # A failed provider's words can carry a remote server's text. The notice beside them is
    # PersonalClaw's own sentence and stays as it is.
    fallback = payload.get("fallback")
    if isinstance(fallback, dict) and isinstance(fallback.get("reason"), str):
        fallback["reason"] = _f(fallback["reason"])
    for hit in payload.get("results", []) or []:
        if not isinstance(hit, dict):
            continue
        for k in ("title", "snippet", "raw_content"):
            if isinstance(hit.get(k), str):
                hit[k] = _f(hit[k])


def create_provider(config: dict[str, Any] | None = None) -> WebToolProvider:
    """Extension factory for the bundled ``web`` tool provider."""
    return WebToolProvider()
