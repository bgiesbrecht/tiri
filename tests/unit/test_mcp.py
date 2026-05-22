"""MCP server endpoint tests — EXT-4.

Covers docs/extensions.md EXT-4 cases:
  1. tiri_query with valid room+question → MCP tool-result format
  2. tiri_list_rooms → returns configured rooms
  3. tiri_query with conversation_id → maintains context across calls
  4. MCP mountable alongside REST without conflict
  5. Unauthenticated MCP call → MCP error (HTTP 200 + JSON-RPC error),
     NOT HTTP 401

Plus the user-required coexistence test (same client session hits both
/rooms and /mcp without interference) and an end-to-end auth
pass-through test (the same Bearer token reaches QueryProvider.execute
via the MCP path, satisfying EXT-6 RBAC).
"""

from __future__ import annotations

import json
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
    LLMResponse,
    QueryResult,
    RoomConfig,
    TableMeta,
)
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    QueryProvider,
    StoreProvider,
    VectorProvider,
)


# ── Test doubles ───────────────────────────────────────────────────────────


_DEFAULT_PLANNING_JSON = json.dumps(
    {
        "requires_multiple_queries": False,
        "steps": [
            {"step_id": "step_1", "description": "single-step default", "depends_on": []}
        ],
        "synthesis_instruction": "Report the single result directly.",
    }
)

_DEFAULT_SYNTHESIS_JSON = json.dumps(
    {
        "answer": "The answer is here.",
        "data_supports": ["one fact from the data"],
        "data_does_not_support": [],
        "would_need": [],
        "confidence": "high",
        "confidence_rationale": "Direct query.",
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
        return TableMeta(
            full_name=full_name,
            columns=[ColumnMeta(name="id", data_type="BIGINT")],
        )

    async def list_tables(self, c, s):
        return []

    async def list_schemas(self, c):
        return []

    async def search_tables(self, q, limit=10):
        return []


class _Vector(VectorProvider):
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    async def upsert(self, id, vector, payload):
        self._data[id] = {"vector": vector, "payload": dict(payload)}

    async def query(self, vector, top_k=5, filter=None):
        return []

    async def delete(self, id):
        self._data.pop(id, None)

    async def list_ids(self, filter=None):
        return list(self._data.keys())


class _Query(QueryProvider):
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.executed_with_token: list[str | None] = []

    async def execute(self, sql, limit=10_000, user_token=None):
        self.executed.append(sql)
        self.executed_with_token.append(user_token)
        return QueryResult(
            columns=["n"], rows=[{"n": 1}], row_count=1, truncated=False, duration_ms=1
        )

    async def validate(self, sql, user_token=None):
        return (True, None)


class _LLM(LLMProvider):
    """Scripted: intent → sql_query; planning → one-step default;
    sql → SELECT 1; synthesis → high; everything else → 'ok'."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, temperature=0.0, max_tokens=2048, task="sql", model=None):
        self.calls.append({"task": task})
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
                usage={},
                raw=None,
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
        return [[float(i), 0.0, 0.0] for i, _ in enumerate(texts)]


# ── Fixtures ──────────────────────────────────────────────────────────────


def _config(*, auth_disabled: bool = True) -> Config:
    return Config(
        llm_backends={"x": ProviderBackendConfig(name="x", type="openai", api_key="k")},
        llm_routing=RoutingConfig(
            intent="x::m", planning="x::m", sql="x::m", synthesis="x::m",
            clarify="x::m", viz_summary="x::m", embed="x::e",
        ),
        catalog_provider="static",
        query_provider="duckdb",
        vector_provider="chroma",
        store_provider="sqlite",
        auth_disabled=auth_disabled,
    )


def _build_app(*, auth_disabled: bool = True) -> tuple[FastAPI, dict[str, Any]]:
    container = {
        "llm": _LLM(),
        "catalog": _Catalog(),
        "metadata_providers": [],
        "query": _Query(),
        "vector": _Vector(),
        "store": _Store(),
    }
    return create_app(cfg=_config(auth_disabled=auth_disabled), container=container), container


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def _seed_room(container: dict, room_id: str, title: str = "test room") -> None:
    config = RoomConfig(
        room_id=room_id,
        title=title,
        tables=["main.x.t"],
        warehouse_id="wh",
        text_instruction=f"Domain description for {room_id}.",
    )
    container["store"]._data[f"room:{room_id}:config"] = json.loads(
        json.dumps(asdict(config))
    )


def _rpc(method: str, params: dict | None = None, rpc_id: int = 1) -> dict:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


# ═══════════════════════════════════════════════════════════════════════════
# Case 4: MCP mounts alongside REST without conflict
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_and_rest_coexist_in_same_client_session() -> None:
    """User-required: hitting /rooms and /mcp from the same client must
    work — no shared state, no route shadowing."""
    app, container = _build_app()
    _seed_room(container, "r1")

    async with _client(app) as c:
        # REST GET first
        rest_response = await c.get("/rooms/r1")
        assert rest_response.status_code == 200
        assert rest_response.json()["room_id"] == "r1"

        # MCP POST in the same session
        mcp_response = await c.post("/mcp", json=_rpc("tools/list"))
        assert mcp_response.status_code == 200
        body = mcp_response.json()
        assert body["jsonrpc"] == "2.0"
        names = [t["name"] for t in body["result"]["tools"]]
        assert {"tiri_query", "tiri_list_rooms", "tiri_room_schema"} <= set(names)

        # REST POST after MCP — no state pollution
        list_resp = await c.get("/rooms/r1")
        assert list_resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Initialize + tools/list
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_initialize_returns_protocol_handshake() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post("/mcp", json=_rpc("initialize"))
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["serverInfo"]["name"] == "tiri"
    assert "protocolVersion" in body["result"]


@pytest.mark.asyncio
async def test_tools_list_returns_all_three_tools() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post("/mcp", json=_rpc("tools/list"))
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"tiri_query", "tiri_list_rooms", "tiri_room_schema"}
    # Each tool MUST have an inputSchema (MCP spec).
    for t in tools:
        assert "inputSchema" in t
        assert t["inputSchema"]["type"] == "object"


# ═══════════════════════════════════════════════════════════════════════════
# Case 1: tiri_query returns MCP tool-result format
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tiri_query_returns_mcp_content_block_format() -> None:
    app, container = _build_app()
    _seed_room(container, "r1")
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "tiri_query", "arguments": {"room_id": "r1", "question": "How many?"}},
            ),
        )
    body = r.json()
    assert "result" in body
    result = body["result"]
    # MCP content block shape: list of {"type": "text", "text": "..."}
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert isinstance(result["content"][0]["text"], str)
    # Structured payload alongside text for programmatic clients
    assert "structuredContent" in result
    assert result["structuredContent"]["sql"] == "SELECT 1"
    assert result["structuredContent"]["row_count"] == 1
    assert "conversation_id" in result["structuredContent"]
    # isError MUST be false for a successful tool call
    assert result["isError"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Case 2: tiri_list_rooms
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tiri_list_rooms_returns_all_rooms() -> None:
    app, container = _build_app()
    _seed_room(container, "r1", title="Sales")
    _seed_room(container, "r2", title="Supply")
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc("tools/call", {"name": "tiri_list_rooms", "arguments": {}}),
        )
    result = r.json()["result"]
    rooms = result["structuredContent"]["rooms"]
    ids = [room["room_id"] for room in rooms]
    assert ids == ["r1", "r2"]  # sorted
    titles = {room["room_id"]: room["title"] for room in rooms}
    assert titles == {"r1": "Sales", "r2": "Supply"}


@pytest.mark.asyncio
async def test_tiri_list_rooms_empty_store_returns_empty_list() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc("tools/call", {"name": "tiri_list_rooms", "arguments": {}}),
        )
    assert r.json()["result"]["structuredContent"]["rooms"] == []


# ═══════════════════════════════════════════════════════════════════════════
# Case 3: tiri_query with conversation_id preserves context
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tiri_query_with_conversation_id_persists_turn_history() -> None:
    """When a client passes a conversation_id, the second turn lands in the
    same conversation index — verifying multi-turn context."""
    app, container = _build_app()
    _seed_room(container, "r1")
    async with _client(app) as c:
        # First call: no conversation_id → server generates one
        r1 = await c.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "tiri_query", "arguments": {"room_id": "r1", "question": "first?"}},
            ),
        )
        conv_id = r1.json()["result"]["structuredContent"]["conversation_id"]
        assert conv_id  # generated and returned

        # Second call: pass the same conversation_id
        r2 = await c.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "tiri_query",
                    "arguments": {
                        "room_id": "r1",
                        "question": "follow-up?",
                        "conversation_id": conv_id,
                    },
                },
                rpc_id=2,
            ),
        )
        assert r2.status_code == 200
        assert r2.json()["result"]["structuredContent"]["conversation_id"] == conv_id

    # The store's conversation index for conv_id should now have 2 turn_ids.
    index = container["store"]._data.get(f"conv:{conv_id}:index")
    assert index is not None
    assert len(index["turn_ids"]) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Case 5: unauthenticated call returns MCP error, NOT HTTP 401
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unauthenticated_call_returns_jsonrpc_error_not_http_401() -> None:
    """User-emphasized: MCP clients expect protocol-level errors. An auth
    failure must arrive as HTTP 200 + JSON-RPC error, not HTTP 401."""
    app, container = _build_app(auth_disabled=False)
    _seed_room(container, "r1")
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "tiri_query", "arguments": {"room_id": "r1", "question": "q"}},
            ),
        )
    assert r.status_code == 200  # NOT 401
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == -32001
    assert "Authentication required" in body["error"]["message"]


@pytest.mark.asyncio
async def test_authenticated_call_with_bearer_succeeds() -> None:
    app, container = _build_app(auth_disabled=False)
    _seed_room(container, "r1")
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "tiri_query", "arguments": {"room_id": "r1", "question": "q"}},
            ),
            headers={"Authorization": "Bearer user-tok"},
        )
    assert r.status_code == 200
    body = r.json()
    assert "result" in body
    # EXT-6 plumbing: the Bearer token reached QueryProvider.execute through
    # the MCP path, same as it does through the REST path.
    assert container["query"].executed_with_token == ["user-tok"]


@pytest.mark.asyncio
async def test_x_forwarded_token_accepted_when_no_bearer() -> None:
    app, container = _build_app(auth_disabled=False)
    _seed_room(container, "r1")
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "tiri_query", "arguments": {"room_id": "r1", "question": "q"}},
            ),
            headers={"X-Forwarded-Access-Token": "fwd-tok"},
        )
    assert r.status_code == 200
    assert "result" in r.json()
    assert container["query"].executed_with_token == ["fwd-tok"]


# ═══════════════════════════════════════════════════════════════════════════
# Error paths
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found_error() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post("/mcp", json=_rpc("nonsense/method"))
    body = r.json()
    assert body["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_unknown_tool_returns_method_not_found_error() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc("tools/call", {"name": "tiri_nonsense", "arguments": {}}),
        )
    body = r.json()
    assert body["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_tiri_query_missing_room_id_returns_invalid_params() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc("tools/call", {"name": "tiri_query", "arguments": {"question": "q"}}),
        )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "room_id" in body["error"]["message"]


@pytest.mark.asyncio
async def test_tiri_query_missing_room_returns_tool_error_not_protocol_error() -> None:
    """RoomNotFoundError is a tool-level error (the room doesn't exist),
    not a protocol error. Surfaces as isError=true in the result, not as
    a JSON-RPC error object."""
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc(
                "tools/call",
                {"name": "tiri_query", "arguments": {"room_id": "ghost", "question": "q"}},
            ),
        )
    body = r.json()
    assert "result" in body
    assert body["result"]["isError"] is True
    assert "Room not found" in body["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_invalid_jsonrpc_body_returns_parse_error() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/mcp", content="not json", headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_tiri_room_schema_returns_tables_and_description() -> None:
    app, container = _build_app()
    _seed_room(container, "r1", title="Sales")
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            json=_rpc("tools/call", {"name": "tiri_room_schema", "arguments": {"room_id": "r1"}}),
        )
    structured = r.json()["result"]["structuredContent"]
    assert structured["room_id"] == "r1"
    assert structured["title"] == "Sales"
    assert structured["tables"] == ["main.x.t"]
    assert "Domain description" in structured["description"]
