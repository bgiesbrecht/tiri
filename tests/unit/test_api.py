"""Tests for tiri.api.* — FastAPI routes.

Covers test cases 1-9 from docs/api.md. Case 10 (benchmarks/run) is deferred
to Step 11; the route stub returns 501 in the meantime.
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
    LLMMessage,
    LLMResponse,
    QueryResult,
    RoomConfig,
    TableMeta,
    VectorMatch,
)
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    MetadataProvider,
    QueryProvider,
    StoreProvider,
    VectorProvider,
)


# ── Stub providers (same shape as test_room_engine) ─────────────────────────


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
        room_id = (filter or {}).get("room_id")
        results = []
        for k, v in self._data.items():
            if room_id and v["payload"].get("room_id") != room_id:
                continue
            results.append(
                VectorMatch(id=k, score=1.0, payload=dict(v["payload"]))
            )
        return results[:top_k]

    async def delete(self, id):
        self._data.pop(id, None)

    async def list_ids(self, filter=None):
        room_id = (filter or {}).get("room_id")
        if room_id is None:
            return list(self._data.keys())
        return [
            k for k, v in self._data.items()
            if v["payload"].get("room_id") == room_id
        ]


class _Query(QueryProvider):
    def __init__(self) -> None:
        self.executed_with_token: list[str | None] = []

    async def execute(self, sql, limit=10_000, user_token=None):
        self.executed_with_token.append(user_token)
        return QueryResult(
            columns=["n"], rows=[{"n": 1}], row_count=1,
            truncated=False, duration_ms=1,
        )

    async def validate(self, sql, user_token=None):
        return (True, None)


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
    """Scripted: intent responses → JSON; sql → string; viz_summary → string.

    EXT-7/EXT-1: tasks "synthesis" and "planning" transparently return safe
    defaults (high-confidence synthesis / one-step plan) unless explicitly
    scripted. Keeps pre-EXT-1 test assertions about turn shape valid without
    per-test scripting churn — the one-step plan goes through the same SQL/
    execute/viz path as the legacy pipeline.
    """

    def __init__(self, responses_by_task: dict[str, list[str]]) -> None:
        self._responses = {k: list(v) for k, v in responses_by_task.items()}
        self._counters = {k: 0 for k in responses_by_task}

    async def complete(self, messages, temperature=0.0, max_tokens=2048, task="sql", model=None):
        if task == "synthesis" and "synthesis" not in self._responses:
            return LLMResponse(content=_DEFAULT_SYNTHESIS_JSON, usage={}, raw=None)
        if task == "planning" and "planning" not in self._responses:
            return LLMResponse(content=_DEFAULT_PLANNING_JSON, usage={}, raw=None)
        idx = self._counters.get(task, 0)
        responses = self._responses.get(task) or [""]
        content = responses[min(idx, len(responses) - 1)]
        self._counters[task] = idx + 1
        return LLMResponse(content=content, usage={}, raw=None)

    async def stream(self, messages, temperature=0.0, task="sql", model=None) -> AsyncIterator[str]:
        yield ""

    async def embed(self, texts):
        return [[float(i), 0.0, 0.0] for i, _ in enumerate(texts)]


# ── Test fixtures ───────────────────────────────────────────────────────────


def _intent_json(intent: str, *, tables: list[str] | None = None, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "intent": intent,
            "relevant_tables": tables or [],
            "relevant_snippets": [],
            "confidence": confidence,
            "reasoning": "test",
        }
    )


def _config(*, auth_disabled: bool = True) -> Config:
    return Config(
        llm_backends={"x": ProviderBackendConfig(name="x", type="openai", api_key="k")},
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
        query_provider="duckdb",
        vector_provider="chroma",
        store_provider="sqlite",
        auth_disabled=auth_disabled,
    )


def _container(llm: LLMProvider | None = None) -> dict[str, Any]:
    return {
        "llm": llm or _LLM(
            {
                "intent": [_intent_json("sql_query", tables=["main.x.t"], confidence=0.9)],
                "sql": ["SELECT 1"],
                "viz_summary": ["summary"],
                "clarify": ["clarify?"],
            }
        ),
        "catalog": _Catalog(),
        "metadata_providers": [],
        "query": _Query(),
        "vector": _Vector(),
        "store": _Store(),
    }


def _build_app(
    *,
    auth_disabled: bool = True,
    llm: LLMProvider | None = None,
) -> tuple[FastAPI, dict[str, Any]]:
    cfg = _config(auth_disabled=auth_disabled)
    container = _container(llm=llm)
    app = create_app(cfg=cfg, container=container)
    return app, container


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def _valid_room_body(**overrides) -> dict[str, Any]:
    base = {
        "room_id": "r1",
        "title": "test room",
        "tables": ["main.x.t"],
        "warehouse_id": "wh-1",
    }
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# Management — cases 1, 2, 6, 7
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_rooms_with_valid_config_returns_201_with_room_id() -> None:
    """Case 1."""
    app, _container = _build_app()
    async with _client(app) as c:
        r = await c.post("/rooms", json=_valid_room_body())
    assert r.status_code == 201
    body = r.json()
    assert body["room_id"] == "r1"
    assert body["config"]["title"] == "test room"


@pytest.mark.asyncio
async def test_post_rooms_missing_tables_returns_422() -> None:
    """Case 2."""
    app, _ = _build_app()
    body = _valid_room_body()
    body["tables"] = []
    async with _client(app) as c:
        r = await c.post("/rooms", json=body)
    assert r.status_code == 422
    assert "tables" in r.text


@pytest.mark.asyncio
async def test_patch_room_with_text_instruction_preserves_other_fields() -> None:
    """Case 6."""
    app, _ = _build_app()
    async with _client(app) as c:
        create = await c.post("/rooms", json=_valid_room_body(text_instruction="orig"))
        assert create.status_code == 201
        patched = await c.patch(
            "/rooms/r1", json={"text_instruction": "updated"}
        )
    assert patched.status_code == 200
    body = patched.json()
    assert body["text_instruction"] == "updated"
    # Other fields preserved.
    assert body["title"] == "test room"
    assert body["tables"] == ["main.x.t"]
    assert body["warehouse_id"] == "wh-1"


@pytest.mark.asyncio
async def test_delete_then_get_returns_404() -> None:
    """Case 7."""
    app, _ = _build_app()
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body())
        deleted = await c.delete("/rooms/r1")
        assert deleted.status_code == 204
        fetched = await c.get("/rooms/r1")
    assert fetched.status_code == 404


@pytest.mark.asyncio
async def test_get_missing_room_returns_404() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.get("/rooms/ghost")
    assert r.status_code == 404
    assert r.json()["error"] == "room_not_found"


@pytest.mark.asyncio
async def test_post_rooms_generates_room_id_when_omitted() -> None:
    app, _ = _build_app()
    body = _valid_room_body()
    del body["room_id"]
    async with _client(app) as c:
        r = await c.post("/rooms", json=body)
    assert r.status_code == 201
    assert r.json()["room_id"]  # auto-generated non-empty id


# ═══════════════════════════════════════════════════════════════════════════
# Conversations — cases 3, 4, 5, 9
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_messages_with_valid_question_returns_200_with_turn() -> None:
    """Case 3."""
    app, _ = _build_app()
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body())
        r = await c.post(
            "/rooms/r1/conversations/conv1/messages",
            json={"question": "hello?"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] == "SELECT 1"
    assert body["viz"] is not None
    assert body["error"] is None


@pytest.mark.asyncio
async def test_stream_returns_event_stream_content_type() -> None:
    """Case 4."""
    app, _ = _build_app()
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body())
        async with c.stream(
            "GET",
            "/rooms/r1/conversations/conv1/messages/stream",
            params={"question": "hi?"},
        ) as r:
            # Consume the stream so the response completes.
            async for _ in r.aiter_lines():
                pass
            content_type = r.headers["content-type"]
    assert "text/event-stream" in content_type


@pytest.mark.asyncio
async def test_stream_yields_done_event_last() -> None:
    """Case 5."""
    app, _ = _build_app()
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body())
        events: list[dict] = []
        async with c.stream(
            "GET",
            "/rooms/r1/conversations/conv1/messages/stream",
            params={"question": "hi?"},
        ) as r:
            async for line in r.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                events.append(json.loads(line[len("data:") :].strip()))
    assert events, "expected at least one SSE event"
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_post_conversations_nonexistent_room_returns_404() -> None:
    """Case 9."""
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post("/rooms/ghost/conversations")
    assert r.status_code == 404
    assert r.json()["error"] == "room_not_found"


@pytest.mark.asyncio
async def test_post_conversations_returns_a_conversation_id() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body())
        r = await c.post("/rooms/r1/conversations")
    assert r.status_code == 201
    assert r.json()["conversation_id"]


@pytest.mark.asyncio
async def test_get_messages_returns_persisted_turns_in_order() -> None:
    app, _ = _build_app(
        llm=_LLM(
            {
                "intent": [
                    _intent_json("sql_query", tables=["main.x.t"], confidence=0.9),
                    _intent_json("sql_query", tables=["main.x.t"], confidence=0.9),
                ],
                "sql": ["SELECT 1", "SELECT 2"],
                "viz_summary": ["s1", "s2"],
            }
        )
    )
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body())
        await c.post(
            "/rooms/r1/conversations/conv1/messages", json={"question": "q1"}
        )
        await c.post(
            "/rooms/r1/conversations/conv1/messages", json={"question": "q2"}
        )
        r = await c.get("/rooms/r1/conversations/conv1/messages")
    body = r.json()
    assert [t["question"] for t in body["turns"]] == ["q1", "q2"]


@pytest.mark.asyncio
async def test_post_messages_with_missing_question_returns_422() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body())
        r = await c.post(
            "/rooms/r1/conversations/conv1/messages", json={}
        )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Auth — case 8
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_missing_authorization_returns_401_when_auth_enabled() -> None:
    """Case 8."""
    app, _ = _build_app(auth_disabled=False)
    async with _client(app) as c:
        r = await c.get("/rooms/anything")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_authorized_request_passes_when_auth_enabled() -> None:
    app, _ = _build_app(auth_disabled=False)
    async with _client(app) as c:
        r = await c.get(
            "/rooms/ghost",
            headers={"Authorization": "Bearer tok-xyz"},
        )
    # Room doesn't exist → 404, but auth passed (no 401).
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_malformed_authorization_returns_401_when_auth_enabled() -> None:
    app, _ = _build_app(auth_disabled=False)
    async with _client(app) as c:
        r = await c.get(
            "/rooms/anything",
            headers={"Authorization": "Token xyz"},  # not "Bearer ..."
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_x_forwarded_access_token_accepted_when_no_bearer() -> None:
    """Databricks Apps deployment: Authorization absent, X-Forwarded-Access-Token
    present → request authenticates."""
    app, _ = _build_app(auth_disabled=False)
    async with _client(app) as c:
        r = await c.get(
            "/rooms/ghost",
            headers={"X-Forwarded-Access-Token": "fwd-tok"},
        )
    # Auth passes (no 401); the room itself doesn't exist → 404.
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_authorization_bearer_takes_precedence_over_forwarded() -> None:
    """When both headers are present, Authorization: Bearer wins. The forwarded
    token is the Databricks-Apps fallback, not an override path."""
    app, container = _build_app(auth_disabled=False)
    async with _client(app) as c:
        await c.post(
            "/rooms",
            json=_valid_room_body(),
            headers={"Authorization": "Bearer setup-tok"},
        )
        r = await c.post(
            "/rooms/r1/conversations/c1/messages",
            json={"question": "q"},
            headers={
                "Authorization": "Bearer bearer-wins",
                "X-Forwarded-Access-Token": "forwarded-loses",
            },
        )
    assert r.status_code == 200
    # query.execute received the Authorization Bearer token, not the
    # X-Forwarded-Access-Token — proves precedence is enforced.
    assert container["query"].executed_with_token == ["bearer-wins"]


@pytest.mark.asyncio
async def test_x_forwarded_token_is_forwarded_to_query_execute() -> None:
    """X-Forwarded fallback flows through to QueryProvider.execute (EXT-6 plumbing)."""
    app, container = _build_app(auth_disabled=False)
    async with _client(app) as c:
        await c.post(
            "/rooms",
            json=_valid_room_body(),
            headers={"X-Forwarded-Access-Token": "setup-fwd"},
        )
        r = await c.post(
            "/rooms/r1/conversations/c1/messages",
            json={"question": "q"},
            headers={"X-Forwarded-Access-Token": "user-fwd"},
        )
    assert r.status_code == 200
    assert container["query"].executed_with_token == ["user-fwd"]


# ═══════════════════════════════════════════════════════════════════════════
# Benchmarks route is stubbed in Step 10 (case 10 implemented in Step 11)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_benchmarks_run_returns_report_with_one_result_per_benchmark() -> None:
    """Case 10. Step 11 makes this real."""
    # Build a room body with one benchmark.
    room_body = _valid_room_body()
    room_body["benchmarks"] = [
        {
            "question": "how many?",
            "expected_sql": "SELECT 1",
            "id": "b1",
            "notes": "",
        }
    ]
    app, _ = _build_app(
        llm=_LLM(
            {
                "intent": [_intent_json("sql_query", tables=["main.x.t"], confidence=0.9)],
                "sql": ["SELECT 1"],
                "viz_summary": ["s"],
            }
        )
    )
    async with _client(app) as c:
        await c.post("/rooms", json=room_body)
        r = await c.post("/rooms/r1/benchmarks/run")
    assert r.status_code == 200
    report = r.json()
    assert report["total"] == 1
    assert len(report["results"]) == 1
    assert report["results"][0]["benchmark_id"] == "b1"


@pytest.mark.asyncio
async def test_feedback_record_marks_turn() -> None:
    app, container = _build_app()
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body())
        msg = await c.post(
            "/rooms/r1/conversations/c1/messages", json={"question": "q"}
        )
        turn_id = msg.json()["turn_id"]
        fb = await c.post(
            f"/rooms/r1/conversations/c1/messages/{turn_id}/feedback",
            json={"signal": "up", "comment": "ok"},
        )
    assert fb.status_code == 200
    stored = await container["store"].get(f"conv:c1:turn:{turn_id}")
    assert stored["feedback_signal"] == "up"


@pytest.mark.asyncio
async def test_feedback_propose_returns_examples_list() -> None:
    # Empty proposal path: no thumbs-up turns yet.
    app, _ = _build_app()
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body())
        r = await c.post("/rooms/r1/feedback/propose")
    assert r.status_code == 200
    assert r.json() == {"proposed_examples": []}


# ═══════════════════════════════════════════════════════════════════════════
# Table metadata inspector — GET /rooms/{id}/tables[/{name}]
# ═══════════════════════════════════════════════════════════════════════════


class _TablesCatalog(CatalogProvider):
    """Catalog with two tables, each carrying one starter column.

    Used by the inspector tests so we have a real schema for the metadata
    providers to enrich.
    """

    async def get_table_meta(self, full_name):
        if full_name == "main.x.t":
            return TableMeta(
                full_name="main.x.t",
                columns=[
                    ColumnMeta(name="id", data_type="BIGINT"),
                    ColumnMeta(name="status", data_type="STRING"),
                ],
            )
        if full_name == "main.x.u":
            return TableMeta(
                full_name="main.x.u",
                columns=[ColumnMeta(name="ref", data_type="BIGINT")],
            )
        from tiri.providers.base import TableNotFoundError as _TNF
        raise _TNF(f"Table not found: {full_name}")

    async def list_tables(self, c, s):
        return []

    async def list_schemas(self, c):
        return []

    async def search_tables(self, q, limit=10):
        return []


class _DomainMetadata(MetadataProvider):
    """Sets descriptions + a synonym. Tags every touched table+column with its name."""

    @property
    def name(self):
        return "domain_yaml"

    async def enrich(self, tables, room_config):
        for name, table in tables.items():
            table.description = f"domain description for {name}"
            table.synonyms.append("from_domain")
            if "domain_yaml" not in table.metadata_sources:
                table.metadata_sources.append("domain_yaml")
            for col in table.columns:
                col.description = f"domain says {col.name}"
                col.metadata_source = "domain_yaml"


class _CatalogAnnotations(MetadataProvider):
    """Sets a contradictory description on main.x.t — produces a conflict record."""

    @property
    def name(self):
        return "uc_annotations"

    async def enrich(self, tables, room_config):
        target = tables.get("main.x.t")
        if target is None:
            return
        from tiri.data_models import MetadataConflict
        if target.description:
            target.conflicts.append(
                MetadataConflict(
                    table="main.x.t",
                    column=None,
                    field="description",
                    values={
                        "domain_yaml": target.description,
                        "uc_annotations": "uc says hi",
                    },
                    resolved_to="uc_annotations",
                )
            )
        target.description = "uc says hi"
        if "uc_annotations" not in target.metadata_sources:
            target.metadata_sources.append("uc_annotations")
        # Column-level conflict on `status` — exercises the per-column slice.
        for col in target.columns:
            if col.name == "status" and col.description:
                target.conflicts.append(
                    MetadataConflict(
                        table="main.x.t",
                        column="status",
                        field="description",
                        values={
                            "domain_yaml": col.description,
                            "uc_annotations": "uc col says hi",
                        },
                        resolved_to="uc_annotations",
                    )
                )
                col.description = "uc col says hi"
                col.metadata_source = "uc_annotations"


def _container_with_metadata(*providers) -> dict[str, Any]:
    base = _container()
    base["catalog"] = _TablesCatalog()
    base["metadata_providers"] = list(providers)
    return base


def _build_app_with_metadata(*providers) -> tuple[FastAPI, dict[str, Any]]:
    cfg = _config(auth_disabled=True)
    container = _container_with_metadata(*providers)
    app = create_app(cfg=cfg, container=container)
    return app, container


@pytest.mark.asyncio
async def test_get_tables_returns_merged_metadata_for_every_room_table() -> None:
    app, _ = _build_app_with_metadata(_DomainMetadata())
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body(tables=["main.x.t", "main.x.u"]))
        r = await c.get("/rooms/r1/tables")
    assert r.status_code == 200
    body = r.json()
    assert body["room_id"] == "r1"
    names = [t["name"] for t in body["tables"]]
    assert names == ["main.x.t", "main.x.u"]
    for table in body["tables"]:
        assert table["description"] == f"domain description for {table['name']}"
        assert "domain_yaml" in table["metadata_sources"]
        assert table["conflicts"] == []
        assert table["columns"]
        for col in table["columns"]:
            assert col["description"] == f"domain says {col['name']}"
            assert col["metadata_sources"] == ["domain_yaml"]


@pytest.mark.asyncio
async def test_get_single_table_returns_merged_metadata() -> None:
    app, _ = _build_app_with_metadata(_DomainMetadata())
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body(tables=["main.x.t", "main.x.u"]))
        r = await c.get("/rooms/r1/tables/main.x.t")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "main.x.t"
    assert body["description"] == "domain description for main.x.t"
    assert {c["name"] for c in body["columns"]} == {"id", "status"}


@pytest.mark.asyncio
async def test_get_single_table_returns_404_when_not_in_room() -> None:
    app, _ = _build_app_with_metadata()
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body(tables=["main.x.t"]))
        r = await c.get("/rooms/r1/tables/main.x.other")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "table_not_in_room"


@pytest.mark.asyncio
async def test_get_tables_returns_404_for_missing_room() -> None:
    app, _ = _build_app_with_metadata()
    async with _client(app) as c:
        r = await c.get("/rooms/missing/tables")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_tables_lists_every_provider_that_touched_a_table() -> None:
    app, _ = _build_app_with_metadata(
        _DomainMetadata(), _CatalogAnnotations()
    )
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body(tables=["main.x.t", "main.x.u"]))
        r = await c.get("/rooms/r1/tables")
    body = r.json()
    by_name = {t["name"]: t for t in body["tables"]}
    # main.x.t was touched by both providers (in order)
    assert by_name["main.x.t"]["metadata_sources"] == ["domain_yaml", "uc_annotations"]
    # main.x.u was touched only by the first provider
    assert by_name["main.x.u"]["metadata_sources"] == ["domain_yaml"]


@pytest.mark.asyncio
async def test_get_tables_includes_schemas_key_with_merged_schema_metadata() -> None:
    """API response MUST include schemas alongside tables, populated via
    MetadataProvider.enrich_schemas across the configured stack."""
    from tiri.data_models import SchemaMeta as _SM

    class _SchemaDomainProvider(MetadataProvider):
        @property
        def name(self):
            return "domain_yaml"

        async def enrich(self, tables, room_config):
            pass

        async def enrich_schemas(self, schemas, room_config):
            for full_name, s in schemas.items():
                s.description = f"description of {full_name}"
                s.domain = "supply_chain"
                s.notes = "dates 1992-1998"
                if self.name not in s.metadata_sources:
                    s.metadata_sources.append(self.name)
                _ = _SM  # touch import for the lint pass

    app, _ = _build_app_with_metadata(_SchemaDomainProvider())
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body(tables=["main.x.t", "main.x.u"]))
        r = await c.get("/rooms/r1/tables")
    body = r.json()
    assert "schemas" in body
    schemas = body["schemas"]
    # main.x is the only catalog.schema prefix referenced.
    assert [s["name"] for s in schemas] == ["main.x"]
    main_x = schemas[0]
    assert main_x["description"] == "description of main.x"
    assert main_x["domain"] == "supply_chain"
    assert main_x["notes"] == "dates 1992-1998"
    assert main_x["metadata_sources"] == ["domain_yaml"]


@pytest.mark.asyncio
async def test_get_tables_schemas_empty_when_no_provider_enriches_schemas() -> None:
    """Backwards compat: providers without enrich_schemas don't pollute schemas."""
    app, _ = _build_app_with_metadata(_DomainMetadata())
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body(tables=["main.x.t"]))
        r = await c.get("/rooms/r1/tables")
    body = r.json()
    assert body["schemas"] == [
        {
            "name": "main.x",
            "description": "",
            "domain": "",
            "freshness": "",
            "owner": "",
            "synonyms": [],
            "notes": "",
            "metadata_sources": [],
        }
    ]


@pytest.mark.asyncio
async def test_get_tables_records_conflicts_at_table_and_column_level() -> None:
    app, _ = _build_app_with_metadata(
        _DomainMetadata(), _CatalogAnnotations()
    )
    async with _client(app) as c:
        await c.post("/rooms", json=_valid_room_body(tables=["main.x.t"]))
        r = await c.get("/rooms/r1/tables/main.x.t")
    body = r.json()
    # Table-scoped conflict on `description`
    assert len(body["conflicts"]) == 1
    table_conflict = body["conflicts"][0]
    assert table_conflict["field"] == "description"
    assert table_conflict["resolved_to"] == "uc_annotations"
    assert set(table_conflict["values"]) == {"domain_yaml", "uc_annotations"}
    # Column-scoped conflict slice — `status` column has a conflict; `id` does not
    cols_by_name = {c["name"]: c for c in body["columns"]}
    assert cols_by_name["id"]["conflicts"] == []
    assert len(cols_by_name["status"]["conflicts"]) == 1
    status_conflict = cols_by_name["status"]["conflicts"][0]
    assert status_conflict["field"] == "description"
    assert status_conflict["resolved_to"] == "uc_annotations"
    # metadata_sources picks up both providers via the conflict record
    assert set(cols_by_name["status"]["metadata_sources"]) == {
        "domain_yaml",
        "uc_annotations",
    }
    # `id` column was only touched by domain_yaml — single source, no conflict
    assert cols_by_name["id"]["metadata_sources"] == ["domain_yaml"]
