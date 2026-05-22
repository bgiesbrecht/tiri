"""MCPResolver — orchestrates external MCP tool calls for context enrichment.

EXT-5: for rooms with `mcp_servers` configured, the resolver discovers
tools on each authorized server and calls the most-relevant one with the
user's question, collecting results as additional context for downstream
agents.

Design rules from the user:
  - The room's `mcp_servers` list is the authorization boundary. A provider
    not in that list MUST NOT be called, even if registered with the engine.
  - MCP calls MUST NOT block the pipeline. Timeouts, transport errors,
    and tool-level errors are caught and logged; the resolver returns what
    it could collect (possibly an empty list).
  - When the room has no `mcp_servers`, the resolver is never invoked —
    zero overhead vs. pre-EXT-5 behavior. This is enforced by the engine,
    not the resolver itself.

The current heuristic for tool selection is intentionally simple: call the
first tool listed by each authorized server with `{"query": question}`.
A future iteration could use the LLM to pick the right tool from each
server's list_tools() output — but the MVP behavior of "call the first
tool, hand it the question" is what most search/lookup MCP servers (Glean,
Confluence search, Brave search) expect anyway.
"""

from __future__ import annotations

import asyncio
import logging

from tiri.data_models import MCPToolResult
from tiri.providers.base import MCPProvider, MCPProviderError


_log = logging.getLogger("tiri.knowledge.mcp_resolver")

_DEFAULT_PER_CALL_TIMEOUT = 5.0


class MCPResolver:
    def __init__(
        self,
        providers: dict[str, MCPProvider],
        *,
        per_call_timeout: float = _DEFAULT_PER_CALL_TIMEOUT,
    ) -> None:
        self._providers = dict(providers)
        self._per_call_timeout = per_call_timeout

    async def resolve(
        self, question: str, allowed_urls: list[str]
    ) -> list[str]:
        """For each URL in `allowed_urls`, look up its provider, call the
        first tool with `{"query": question}`, and collect the result.

        Returns a list of "tool_name: <result text>" strings — one per
        successful call. Failures are logged and skipped, NEVER raised.
        An empty `allowed_urls` returns `[]` without doing any work.
        """
        if not allowed_urls:
            return []
        results: list[str] = []
        for url in allowed_urls:
            provider = self._providers.get(url)
            if provider is None:
                _log.warning(
                    "Room declares MCP server %r but no provider is "
                    "registered; skipping",
                    url,
                )
                continue
            try:
                entry = await asyncio.wait_for(
                    self._call_first_tool(provider, question),
                    timeout=self._per_call_timeout,
                )
            except asyncio.TimeoutError:
                _log.warning("MCP server %r timed out; skipping", url)
                continue
            except MCPProviderError as e:
                _log.warning(
                    "MCP server %r transport failure: %s; skipping", url, e
                )
                continue
            except Exception:
                # Defensive: don't let any unexpected provider exception take
                # down the pipeline. The whole point of EXT-5 graceful
                # degradation is that MCP can never block a turn.
                _log.exception(
                    "Unexpected error calling MCP server %r; skipping", url
                )
                continue
            if entry is not None:
                results.append(entry)
        return results

    async def _call_first_tool(
        self, provider: MCPProvider, question: str
    ) -> str | None:
        tools = await provider.list_tools()
        if not tools:
            return None
        tool = tools[0]
        result: MCPToolResult = await provider.call_tool(
            tool.name, {"query": question}
        )
        if result.is_error:
            _log.warning(
                "MCP tool %r returned error: %s", tool.name, result.content
            )
            return None
        content = (result.content or "").strip()
        if not content:
            return None
        return f"{tool.name}: {content}"
