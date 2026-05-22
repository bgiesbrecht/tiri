"""EXT-5 tests — MCP tool consumption.

Three layers of test coverage:
  1. HttpMCPProvider — speaks JSON-RPC 2.0 to a mocked server (httpx.MockTransport)
  2. MCPResolver — orchestration: timeout/error fallback, authorization
  3. RoomEngine integration — mcp_servers gating, mcp_context propagation,
     no-regression for rooms without MCP

Doc test cases (docs/extensions.md EXT-5):
  1. Room with mcp_servers configured → MCP tools available
  2. Room with no mcp_servers → behaves identically to current behavior
  3. MCP tool call timeout → fall back gracefully, pipeline continues
  4. MCP tool call error → log + continue without tool result
  5. MCP tool result → included in ContextPackage, visible in reasoning trace
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tiri.api.main import create_app
from tiri.config import Config, ProviderBackendConfig, RoutingConfig
from tiri.data_models import (
    ColumnMeta,
    ContextPackage,
    LLMResponse,
    MCPTool,
    MCPToolResult,
    QueryResult,
    RoomConfig,
    TableMeta,
)
from tiri.knowledge.mcp_resolver import MCPResolver
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    MCPProvider,
    MCPProviderError,
    QueryProvider,
    StoreProvider,
    VectorProvider,
)
from tiri.providers.local.mcp_http import HttpMCPProvider


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1: HttpMCPProvider (JSON-RPC over httpx)
# ═══════════════════════════════════════════════════════════════════════════


def _envelope(result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": "ignored", "result": result}


def _error_envelope(code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": "ignored", "error": {"code": code, "message": message}}


@pytest.mark.asyncio
async def test_http_provider_list_tools_round_trips_json_rpc() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        assert body["method"] == "tools/list"
        return httpx.Response(
            200,
            json=_envelope(
                {
                    "tools": [
                        {
                            "name": "search",
                            "description": "Search Confluence",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                }
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpMCPProvider(url="https://mcp.example/mcp", client=client)
    tools = await provider.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "search"
    assert tools[0].description == "Search Confluence"


@pytest.mark.asyncio
async def test_http_provider_call_tool_extracts_text_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "search"
        assert body["params"]["arguments"] == {"query": "what is ARR?"}
        return httpx.Response(
            200,
            json=_envelope(
                {
                    "content": [
                        {"type": "text", "text": "ARR = annual recurring revenue."}
                    ],
                    "isError": False,
                }
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpMCPProvider(url="https://mcp.example/mcp", client=client)
    result = await provider.call_tool("search", {"query": "what is ARR?"})
    assert result.tool_name == "search"
    assert result.content == "ARR = annual recurring revenue."
    assert result.is_error is False


@pytest.mark.asyncio
async def test_http_provider_call_tool_propagates_is_error_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_envelope(
                {
                    "content": [{"type": "text", "text": "search index unavailable"}],
                    "isError": True,
                }
            ),
        )

    provider = HttpMCPProvider(
        url="https://mcp.example/mcp",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await provider.call_tool("search", {"query": "x"})
    assert result.is_error is True


@pytest.mark.asyncio
async def test_http_provider_protocol_error_raises_mcp_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_error_envelope(-32001, "auth required"))

    provider = HttpMCPProvider(
        url="https://mcp.example/mcp",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(MCPProviderError, match="auth required"):
        await provider.list_tools()


@pytest.mark.asyncio
async def test_http_provider_5xx_raises_mcp_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    provider = HttpMCPProvider(
        url="https://mcp.example/mcp",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(MCPProviderError, match="HTTP 503"):
        await provider.list_tools()


@pytest.mark.asyncio
async def test_http_provider_sends_authorization_header_when_configured() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_envelope({"tools": []}))

    # Default constructor wires the auth header into the client.
    provider = HttpMCPProvider(
        url="https://mcp.example/mcp", auth_token="svc-tok"
    )
    # Swap in a captured transport AFTER the auth header has been baked in.
    provider._client._transport = httpx.MockTransport(handler)
    await provider.list_tools()
    assert captured[0].headers["authorization"] == "Bearer svc-tok"


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2: MCPResolver — orchestration + failure modes
# ═══════════════════════════════════════════════════════════════════════════


class _StubProvider(MCPProvider):
    """In-memory MCPProvider for resolver tests."""

    def __init__(
        self,
        *,
        tools: list[MCPTool] | None = None,
        result: MCPToolResult | None = None,
        raise_on_list: Exception | None = None,
        raise_on_call: Exception | None = None,
        sleep_seconds: float = 0.0,
    ) -> None:
        self._tools = tools or []
        self._result = result
        self._raise_on_list = raise_on_list
        self._raise_on_call = raise_on_call
        self._sleep_seconds = sleep_seconds
        self.call_log: list[tuple[str, dict]] = []

    async def list_tools(self) -> list[MCPTool]:
        if self._raise_on_list:
            raise self._raise_on_list
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict) -> MCPToolResult:
        self.call_log.append((name, dict(arguments)))
        if self._sleep_seconds:
            await asyncio.sleep(self._sleep_seconds)
        if self._raise_on_call:
            raise self._raise_on_call
        return self._result or MCPToolResult(
            tool_name=name, content="ok", is_error=False
        )


@pytest.mark.asyncio
async def test_resolver_empty_allowed_urls_returns_empty_list_without_work() -> None:
    """Case 2 root invariant: no allowed URLs → zero work."""
    stub = _StubProvider(tools=[MCPTool("t", "", {})])
    resolver = MCPResolver({"https://x/mcp": stub})
    assert await resolver.resolve("q", []) == []
    assert stub.call_log == []


@pytest.mark.asyncio
async def test_resolver_returns_formatted_results_from_each_allowed_server() -> None:
    """Case 5: tool result MUST be included in the returned list and
    formatted as 'tool_name: <content>'."""
    p1 = _StubProvider(
        tools=[MCPTool("search", "", {})],
        result=MCPToolResult(tool_name="search", content="ARR=annual revenue.", is_error=False),
    )
    p2 = _StubProvider(
        tools=[MCPTool("lookup", "", {})],
        result=MCPToolResult(tool_name="lookup", content="MRR=monthly revenue.", is_error=False),
    )
    resolver = MCPResolver(
        {"https://a/mcp": p1, "https://b/mcp": p2}
    )
    results = await resolver.resolve(
        "what is ARR?", ["https://a/mcp", "https://b/mcp"]
    )
    assert results == [
        "search: ARR=annual revenue.",
        "lookup: MRR=annual revenue.".replace("annual", "monthly"),
    ]


@pytest.mark.asyncio
async def test_resolver_timeout_skips_server_and_continues() -> None:
    """Case 3: a timeout must NOT block the pipeline. The slow server's
    result is dropped; other servers' results still flow through."""
    slow = _StubProvider(
        tools=[MCPTool("search", "", {})],
        sleep_seconds=1.0,
    )
    fast = _StubProvider(
        tools=[MCPTool("lookup", "", {})],
        result=MCPToolResult(tool_name="lookup", content="ok", is_error=False),
    )
    resolver = MCPResolver(
        {"https://slow/mcp": slow, "https://fast/mcp": fast},
        per_call_timeout=0.05,
    )
    results = await resolver.resolve("q", ["https://slow/mcp", "https://fast/mcp"])
    assert results == ["lookup: ok"]


@pytest.mark.asyncio
async def test_resolver_provider_error_skips_server_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Case 4: MCP error → log + continue without tool result."""
    failing = _StubProvider(
        tools=[MCPTool("search", "", {})],
        raise_on_call=MCPProviderError("network down"),
    )
    working = _StubProvider(
        tools=[MCPTool("lookup", "", {})],
        result=MCPToolResult(tool_name="lookup", content="ok", is_error=False),
    )
    resolver = MCPResolver(
        {"https://fail/mcp": failing, "https://ok/mcp": working}
    )
    with caplog.at_level(logging.WARNING, logger="tiri.knowledge.mcp_resolver"):
        results = await resolver.resolve(
            "q", ["https://fail/mcp", "https://ok/mcp"]
        )
    assert results == ["lookup: ok"]
    assert any("transport failure" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_resolver_unauthorized_url_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A URL listed in the room config but absent from the engine's
    provider registry MUST be skipped with a warning, not raised."""
    resolver = MCPResolver({})  # no providers registered
    with caplog.at_level(logging.WARNING, logger="tiri.knowledge.mcp_resolver"):
        results = await resolver.resolve("q", ["https://unregistered/mcp"])
    assert results == []
    assert any("no provider is registered" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_resolver_tool_level_error_drops_result_but_does_not_raise() -> None:
    """An MCPToolResult with is_error=True must be filtered (not propagated
    as if it were a real definition)."""
    p = _StubProvider(
        tools=[MCPTool("search", "", {})],
        result=MCPToolResult(tool_name="search", content="not found", is_error=True),
    )
    resolver = MCPResolver({"https://x/mcp": p})
    assert await resolver.resolve("q", ["https://x/mcp"]) == []


@pytest.mark.asyncio
async def test_resolver_empty_tool_list_skips_server() -> None:
    p = _StubProvider(tools=[])  # server lists zero tools
    resolver = MCPResolver({"https://x/mcp": p})
    assert await resolver.resolve("q", ["https://x/mcp"]) == []


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3: RoomEngine integration — mcp_servers gating + propagation
# ═══════════════════════════════════════════════════════════════════════════


_DEFAULT_PLANNING_JSON = json.dumps(
    {
        "requires_multiple_queries": False,
        "steps": [
            {"step_id": "step_1", "description": "default", "depends_on": []}
        ],
        "synthesis_instruction": "Report the single result directly.",
    }
)

_DEFAULT_SYNTHESIS_JSON = json.dumps(
    {
        "answer": "x",
        "data_supports": [],
        "data_does_not_support": [],
        "would_need": [],
        "confidence": "high",
        "confidence_rationale": "t",
    }
)


class _Store(StoreProvider):
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    async def get(self, key):
        v = self._data.get(key)
        return None if v is None else json.loads(json.dumps(v))

    async def put(self, key, value):
        self._data[key] = json.loads(json.dumps(value))

    async def list_keys(self, prefix):
        return sorted(k for k in self._data if k.startswith(prefix))

    async def delete(self, key):
        self._data.pop(key, None)


class _Catalog(CatalogProvider):
    async def get_table_meta(self, full_name):
        return TableMeta(full_name=full_name, columns=[ColumnMeta(name="id", data_type="BIGINT")])

    async def list_tables(self, c, s):
        return []

    async def list_schemas(self, c):
        return []

    async def search_tables(self, q, limit=10):
        return []


class _Vector(VectorProvider):
    def __init__(self):
        self._data = {}

    async def upsert(self, id, vector, payload):
        self._data[id] = {"vector": vector, "payload": dict(payload)}

    async def query(self, vector, top_k=5, filter=None):
        return []

    async def delete(self, id):
        self._data.pop(id, None)

    async def list_ids(self, filter=None):
        return list(self._data.keys())


class _Query(QueryProvider):
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, sql, limit=10_000, user_token=None):
        self.executed.append(sql)
        return QueryResult(
            columns=["n"], rows=[{"n": 1}], row_count=1, truncated=False, duration_ms=1
        )

    async def validate(self, sql, user_token=None):
        return (True, None)


class _LLM(LLMProvider):
    """Captures the system prompts so tests can assert mcp_context is
    interpolated into them."""

    def __init__(self):
        self.prompts_by_task: dict[str, list[str]] = {}

    async def complete(self, messages, temperature=0.0, max_tokens=2048, task="sql", model=None):
        self.prompts_by_task.setdefault(task, []).append(messages[0].content)
        if task == "intent":
            return LLMResponse(
                content=json.dumps(
                    {
                        "intent": "sql_query",
                        "relevant_tables": ["main.x.t"],
                        "relevant_snippets": [],
                        "confidence": 0.95,
                        "reasoning": "ok",
                    }
                ),
                usage={}, raw=None,
            )
        if task == "planning":
            return LLMResponse(content=_DEFAULT_PLANNING_JSON, usage={}, raw=None)
        if task == "sql":
            return LLMResponse(content="SELECT 1", usage={}, raw=None)
        if task == "synthesis":
            return LLMResponse(content=_DEFAULT_SYNTHESIS_JSON, usage={}, raw=None)
        return LLMResponse(content="ok", usage={}, raw=None)

    async def stream(self, messages, temperature=0.0, task="sql", model=None) -> AsyncIterator[str]:
        yield ""

    async def embed(self, texts):
        return [[float(i)] for i, _ in enumerate(texts)]


def _config() -> Config:
    return Config(
        llm_backends={"x": ProviderBackendConfig(name="x", type="openai", api_key="k")},
        llm_routing=RoutingConfig(
            intent="x::m", planning="x::m", sql="x::m", synthesis="x::m",
            clarify="x::m", viz_summary="x::m", embed="x::e",
        ),
        catalog_provider="static", query_provider="duckdb",
        vector_provider="chroma", store_provider="sqlite",
        auth_disabled=True,
    )


def _seed_room(container, *, mcp_servers: list[str]) -> None:
    config = RoomConfig(
        room_id="r1", title="r", tables=["main.x.t"], warehouse_id="wh",
        mcp_servers=mcp_servers,
    )
    container["store"]._data["room:r1:config"] = json.loads(
        json.dumps(asdict(config))
    )


def _build_app(*, mcp_providers: dict[str, MCPProvider] | None = None) -> tuple[FastAPI, dict]:
    container = {
        "llm": _LLM(),
        "catalog": _Catalog(),
        "metadata_providers": [],
        "query": _Query(),
        "vector": _Vector(),
        "store": _Store(),
        "mcp_providers": mcp_providers or {},
    }
    app = create_app(cfg=_config(), container=container)
    return app, container


# Case 2 — THE most important regression test (user-emphasized)


@pytest.mark.asyncio
async def test_room_with_no_mcp_servers_makes_zero_mcp_calls() -> None:
    """When a room declares no mcp_servers, MCPResolver MUST NOT be invoked
    and no provider in the registry MUST be touched — guarantees zero
    overhead vs. pre-EXT-5 behavior."""
    tracker = _StubProvider(tools=[MCPTool("search", "", {})])
    # Provider IS registered with the engine — but the room doesn't authorize it.
    from tiri.engine.room_engine import RoomEngine
    container = {
        "llm": _LLM(),
        "catalog": _Catalog(),
        "metadata_providers": [],
        "query": _Query(),
        "vector": _Vector(),
        "store": _Store(),
    }
    _seed_room(container, mcp_servers=[])
    engine = RoomEngine(
        llm=container["llm"],
        catalog=container["catalog"],
        metadata_providers=[],
        query=container["query"],
        vector=container["vector"],
        store=container["store"],
        mcp_providers={"https://x/mcp": tracker},
    )
    turn = await engine.chat("r1", "c1", "How many?")
    assert turn.error is None
    assert tracker.call_log == []  # zero MCP calls


# Case 1 — mcp_servers configured → MCP tools available + invoked


@pytest.mark.asyncio
async def test_room_with_mcp_servers_invokes_resolver_and_populates_context() -> None:
    """Case 1 + 5: when the room authorizes a server AND the engine has a
    provider for it, the resolver MUST be called and its result MUST flow
    into the agents' prompts."""
    p = _StubProvider(
        tools=[MCPTool("search", "Search", {})],
        result=MCPToolResult(
            tool_name="search",
            content="ARR is annual recurring revenue.",
            is_error=False,
        ),
    )
    from tiri.engine.room_engine import RoomEngine
    container = {
        "llm": _LLM(),
        "catalog": _Catalog(),
        "metadata_providers": [],
        "query": _Query(),
        "vector": _Vector(),
        "store": _Store(),
    }
    _seed_room(container, mcp_servers=["https://confluence/mcp"])
    engine = RoomEngine(
        llm=container["llm"],
        catalog=container["catalog"],
        metadata_providers=[],
        query=container["query"],
        vector=container["vector"],
        store=container["store"],
        mcp_providers={"https://confluence/mcp": p},
    )
    await engine.chat("r1", "c1", "What is ARR for our company?")
    assert p.call_log == [("search", {"query": "What is ARR for our company?"})]
    # The MCP result must be visible in all three downstream agent prompts.
    intent_prompt = container["llm"].prompts_by_task["intent"][0]
    sql_prompt = container["llm"].prompts_by_task["sql"][0]
    synthesis_prompt = container["llm"].prompts_by_task["synthesis"][0]
    for prompt in (intent_prompt, sql_prompt, synthesis_prompt):
        assert "search: ARR is annual recurring revenue." in prompt


# Case 3 — timeout doesn't block the pipeline


@pytest.mark.asyncio
async def test_mcp_timeout_does_not_block_pipeline() -> None:
    """The room declares an MCP server; that server is unreachable. The
    pipeline MUST still produce a normal turn with empty mcp_context."""
    slow = _StubProvider(
        tools=[MCPTool("search", "", {})], sleep_seconds=1.0
    )
    from tiri.engine.room_engine import RoomEngine
    container = {
        "llm": _LLM(),
        "catalog": _Catalog(),
        "metadata_providers": [],
        "query": _Query(),
        "vector": _Vector(),
        "store": _Store(),
    }
    _seed_room(container, mcp_servers=["https://slow/mcp"])
    engine = RoomEngine(
        llm=container["llm"],
        catalog=container["catalog"],
        metadata_providers=[],
        query=container["query"],
        vector=container["vector"],
        store=container["store"],
        mcp_providers={"https://slow/mcp": slow},
    )
    # Patch the resolver default timeout via dependency on slow provider.
    # The resolver applies its own per-call timeout — set engine-level
    # MCP behavior to time out fast by replacing the resolver:
    import tiri.engine.room_engine as engine_module
    original_resolver = engine_module.MCPResolver
    engine_module.MCPResolver = lambda providers: MCPResolver(
        providers, per_call_timeout=0.05
    )
    try:
        turn = await engine.chat("r1", "c1", "q")
    finally:
        engine_module.MCPResolver = original_resolver

    assert turn.error is None  # pipeline completed despite MCP timeout
    assert turn.sql == "SELECT 1"
    # mcp_context should be empty (timeout fallback)
    intent_prompt = container["llm"].prompts_by_task["intent"][0]
    assert "(none)" in intent_prompt  # mcp_context section rendered as "(none)"
