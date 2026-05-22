"""HttpMCPProvider — calls an external MCP server over JSON-RPC HTTP.

EXT-5: this provider is the concrete implementation behind `MCPProvider`.
It uses the same Streamable-HTTP transport that Tiri itself exposes for
EXT-4 — a single POST endpoint that accepts JSON-RPC 2.0 requests.

The httpx client is injectable for testability (httpx.MockTransport in
tests). Same pattern as DatabricksLLMProvider, DatabricksQueryProvider.

Failure semantics (called out in providers/base.py.MCPProvider):
  - Transport failure (timeout, network, malformed envelope) → MCPProviderError
  - Tool-level error from the remote server → MCPToolResult(is_error=True)

Authentication: optional `auth_token` constructor arg. When set, every
outgoing request carries `Authorization: Bearer <token>`. The room
operator configures this per-server; per-user token forwarding for MCP
calls is intentionally out of scope (the calling user's token belongs to
the *warehouse*, not to external MCP servers).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from tiri.data_models import MCPTool, MCPToolResult
from tiri.providers.base import MCPProvider, MCPProviderError


_DEFAULT_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class HttpMCPProvider(MCPProvider):
    def __init__(
        self,
        url: str,
        *,
        auth_token: str | None = None,
        timeout: httpx.Timeout | float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not url:
            raise MCPProviderError("HttpMCPProvider requires a non-empty URL")
        self._url = url
        if client is not None:
            self._client = client
        else:
            headers: dict[str, str] = {}
            if auth_token:
                headers["Authorization"] = f"Bearer {auth_token}"
            self._client = httpx.AsyncClient(
                timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT,
                headers=headers or None,
            )

    async def list_tools(self) -> list[MCPTool]:
        result = await self._rpc("tools/list", {})
        tools_raw = result.get("tools")
        if not isinstance(tools_raw, list):
            raise MCPProviderError(
                f"tools/list response missing `tools` array: {result!r}"
            )
        tools: list[MCPTool] = []
        for t in tools_raw:
            if not isinstance(t, dict):
                continue
            tools.append(
                MCPTool(
                    name=str(t.get("name") or ""),
                    description=str(t.get("description") or ""),
                    input_schema=t.get("inputSchema") or {},
                )
            )
        return tools

    async def call_tool(
        self, name: str, arguments: dict
    ) -> MCPToolResult:
        result = await self._rpc(
            "tools/call", {"name": name, "arguments": arguments}
        )
        content_blocks = result.get("content") or []
        text_parts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        return MCPToolResult(
            tool_name=name,
            content="\n".join(text_parts),
            is_error=bool(result.get("isError")),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── internals ──────────────────────────────────────────────────────────

    async def _rpc(self, method: str, params: dict) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": params,
        }
        try:
            response = await self._client.post(self._url, json=body)
        except httpx.HTTPError as e:
            raise MCPProviderError(
                f"MCP transport failure calling {method!r} on {self._url}: {e}"
            ) from e
        if response.status_code >= 500:
            raise MCPProviderError(
                f"MCP server {self._url} returned HTTP {response.status_code}"
            )
        try:
            envelope = response.json()
        except json.JSONDecodeError as e:
            raise MCPProviderError(
                f"MCP server {self._url} returned non-JSON body: "
                f"{response.text[:200]!r}"
            ) from e

        if envelope.get("error"):
            err = envelope["error"]
            raise MCPProviderError(
                f"MCP JSON-RPC error from {self._url}: "
                f"{err.get('code')} {err.get('message')}"
            )

        result = envelope.get("result")
        if not isinstance(result, dict):
            raise MCPProviderError(
                f"MCP response from {self._url} missing `result` object"
            )
        return result
