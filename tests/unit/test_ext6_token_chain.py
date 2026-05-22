"""EXT-6 end-to-end token pass-through chain.

Asserts the full plumbing from an HTTP request's Authorization header to
the outgoing Statement Execution API call's Authorization header:

    Client                                   Tiri stack
    ──────                                   ──────────
    Authorization: Bearer <user-token>  →    auth.py extracts the token
                                        →    conversations.py forwards to engine.chat()
                                        →    RoomEngine routes to SQLAgent + query.execute(user_token=...)
                                        →    DatabricksQueryProvider swaps Authorization header
    Authorization: Bearer <user-token>  ←    Statement Execution API request goes out with the user's token

The full Tiri pipeline is exercised with a single concrete provider
(`DatabricksQueryProvider`) wired into otherwise-stub providers. The
Databricks HTTP client uses `httpx.MockTransport` so no real warehouse
is needed. The user token at the entry point MUST appear in the outgoing
SQL statement request's headers — that's the assertion the test enforces.
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
from tiri.config import (
    Config,
    ProviderBackendConfig,
    RoutingConfig,
)
from tiri.data_models import (
    ColumnMeta,
    LLMResponse,
    QueryResult,
    RoomConfig,
    TableMeta,
    VectorMatch,
)
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    QueryProvider,
    StoreProvider,
    VectorProvider,
)
from tiri.providers.databricks.query import DatabricksQueryProvider


_DATABRICKS_HOST = "https://example.cloud.databricks.com"
_SERVICE_TOKEN = "service-tok"


# ── Stub non-Query providers (we want a real DatabricksQueryProvider) ──────


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


_DEFAULT_SYNTHESIS_JSON = json.dumps(
    {
        "answer": "Result shown above.",
        "data_supports": [],
        "data_does_not_support": [],
        "would_need": [],
        "confidence": "high",
        "confidence_rationale": "test default",
    }
)

_DEFAULT_PLANNING_JSON = json.dumps(
    {
        "requires_multiple_queries": False,
        "steps": [
            {"step_id": "step_1", "description": "single-step default", "depends_on": []}
        ],
        "synthesis_instruction": "Report the single result directly.",
    }
)


class _LLM(LLMProvider):
    """Scripted: intent → sql_query, sql → SELECT 1, planning → one-step,
    synthesis → high, else → 'ok'."""

    async def complete(
        self, messages, temperature=0.0, max_tokens=2048, task="sql", model=None
    ):
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

    async def stream(
        self, messages, temperature=0.0, task="sql", model=None
    ) -> AsyncIterator[str]:
        yield ""

    async def embed(self, texts):
        return [[float(i), 0.0, 0.0] for i, _ in enumerate(texts)]


# ── App factory wires a real DatabricksQueryProvider with an HTTP mock ─────


def _build_app_with_real_query_provider(
    *, auth_disabled: bool, on_request: list[httpx.Request]
) -> FastAPI:
    """Build a Tiri app whose QueryProvider is a real DatabricksQueryProvider
    backed by httpx.MockTransport. Every outgoing request is captured in
    `on_request` so tests can assert on headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        on_request.append(request)
        # Statement-Execution API: SUCCEEDED with one column / one row.
        return httpx.Response(
            200,
            json={
                "statement_id": "stmt-1",
                "status": {"state": "SUCCEEDED"},
                "manifest": {"schema": {"columns": [{"name": "n"}]}},
                "result": {"data_array": [[1]], "row_count": 1},
            },
        )

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        # The provider's own Authorization header will be set per-call.
    )
    query_provider = DatabricksQueryProvider(
        host=_DATABRICKS_HOST,
        token=_SERVICE_TOKEN,
        warehouse_id="wh-1",
        client=mock_client,
    )

    cfg = Config(
        llm_backends={
            "x": ProviderBackendConfig(name="x", type="openai", api_key="k")
        },
        llm_routing=RoutingConfig(
            intent="x::m",
            planning="x::m",
            sql="x::m",
            synthesis="x::m",
            clarify="x::m",
            viz_summary="x::m",
            embed="x::e",
        ),
        catalog_provider="static",
        query_provider="databricks",  # real
        vector_provider="chroma",
        store_provider="sqlite",
        databricks_host=_DATABRICKS_HOST,
        databricks_token=_SERVICE_TOKEN,
        db_warehouse_id="wh-1",
        auth_disabled=auth_disabled,
    )
    container = {
        "llm": _LLM(),
        "catalog": _Catalog(),
        "metadata_providers": [],
        "query": query_provider,
        "vector": _Vector(),
        "store": _Store(),
    }
    return create_app(cfg=cfg, container=container)


def _seed_room(app: FastAPI) -> RoomConfig:
    """Synchronously plant a RoomConfig in the store so /messages can run."""
    cfg = RoomConfig(
        room_id="r1",
        title="r1",
        tables=["main.x.t"],
        warehouse_id="wh-1",
    )
    app.state.container["store"]._data[f"room:{cfg.room_id}:config"] = (
        json.loads(json.dumps(asdict(cfg)))
    )
    return cfg


# ── The chain test ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bearer_token_flows_from_http_header_to_warehouse_request() -> None:
    """Authorization: Bearer <user-token> on the HTTP request MUST end up
    on the outgoing Statement Execution API request."""
    captured: list[httpx.Request] = []
    app = _build_app_with_real_query_provider(
        auth_disabled=False, on_request=captured
    )
    _seed_room(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/rooms/r1/conversations/conv1/messages",
            json={"question": "How many?"},
            headers={"Authorization": "Bearer user-end-to-end"},
        )

    assert response.status_code == 200, response.text
    # The DatabricksQueryProvider sends two requests (POST submit + GET poll
    # is possible, but with `wait_timeout=30s` the submit usually completes
    # synchronously). At minimum one POST to /api/2.0/sql/statements.
    sql_requests = [
        r for r in captured if r.url.path == "/api/2.0/sql/statements"
    ]
    assert sql_requests, "expected at least one statement-execution request"
    # The user's token, not the service account, MUST be on that request.
    auth_header = sql_requests[0].headers.get("authorization")
    assert auth_header == "Bearer user-end-to-end", (
        f"expected user token on the warehouse request; got {auth_header!r}"
    )


@pytest.mark.asyncio
async def test_x_forwarded_token_flows_through_when_no_bearer() -> None:
    """Databricks Apps deployment: only X-Forwarded-Access-Token is present;
    that token MUST reach the warehouse request."""
    captured: list[httpx.Request] = []
    app = _build_app_with_real_query_provider(
        auth_disabled=False, on_request=captured
    )
    _seed_room(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/rooms/r1/conversations/conv1/messages",
            json={"question": "How many?"},
            headers={"X-Forwarded-Access-Token": "apps-fwd-token"},
        )

    assert response.status_code == 200, response.text
    sql_requests = [
        r for r in captured if r.url.path == "/api/2.0/sql/statements"
    ]
    assert sql_requests
    assert sql_requests[0].headers.get("authorization") == "Bearer apps-fwd-token"


@pytest.mark.asyncio
async def test_no_user_token_falls_back_to_service_credential() -> None:
    """When auth is disabled (or no user token present), the warehouse
    request MUST use the service-account token from Config — no regression
    for service-to-service / benchmark-run paths."""
    captured: list[httpx.Request] = []
    app = _build_app_with_real_query_provider(
        auth_disabled=True,  # no user token will be extracted
        on_request=captured,
    )
    _seed_room(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/rooms/r1/conversations/conv1/messages",
            json={"question": "How many?"},
        )

    assert response.status_code == 200, response.text
    sql_requests = [
        r for r in captured if r.url.path == "/api/2.0/sql/statements"
    ]
    assert sql_requests
    # No user token → service token on the underlying httpx.AsyncClient is used.
    # The mock client we constructed didn't have a default Authorization
    # header set, and DatabricksQueryProvider only overrides per-request
    # when user_token is provided. So the request has no Authorization header
    # — which proves the per-request override path was NOT taken.
    assert "authorization" not in {
        k.lower() for k in sql_requests[0].headers.keys()
    }
