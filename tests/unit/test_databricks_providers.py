"""Tests for tiri.providers.databricks.*.

HTTP-based providers (LLM, Query, Vector) are tested with httpx.MockTransport.
SDK-based providers (Catalog, UCAnnotationsMetadataProvider) are tested with
a stubbed WorkspaceClient.

Covers all 10 test cases from docs/databricks_providers.md.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from tiri.data_models import (
    ColumnMeta,
    LLMMessage,
    QueryResult,
    RoomConfig,
    TableMeta,
)
from tiri.providers.base import (
    LLMProviderError,
    QueryProvider,
    QueryProviderError,
    TableNotFoundError,
    VectorProviderError,
)
from tiri.providers.databricks.catalog import DatabricksCatalogProvider
from tiri.providers.databricks.llm import DatabricksLLMProvider
from tiri.providers.databricks.metadata import (
    UCAnnotationsMetadataProvider,
)
from tiri.providers.databricks.query import DatabricksQueryProvider
from tiri.providers.databricks.store import DatabricksStoreProvider
from tiri.providers.databricks.vector import DatabricksVectorProvider


HOST = "https://example.cloud.databricks.com"
TOKEN = "tok-xyz"


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://placeholder",  # ignored — handler sees full URL
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def _room_config() -> RoomConfig:
    return RoomConfig(
        room_id="r",
        title="r",
        tables=["main.x.t"],
        warehouse_id="wh",
    )


# ═══════════════════════════════════════════════════════════════════════════
# DatabricksLLMProvider
# ═══════════════════════════════════════════════════════════════════════════


def test_llm_constructor_requires_host_and_token() -> None:
    with pytest.raises(LLMProviderError, match="host"):
        DatabricksLLMProvider(host="", token=TOKEN)
    with pytest.raises(LLMProviderError, match="token"):
        DatabricksLLMProvider(host=HOST, token="")


# ── Case 1: complete() valid endpoint MUST return non-empty content ────────


@pytest.mark.asyncio
async def test_llm_complete_returns_content() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello world"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    provider = DatabricksLLMProvider(
        host=HOST,
        token=TOKEN,
        completion_endpoint="my-model",
        client=_make_client(handler),
    )
    response = await provider.complete([LLMMessage(role="user", content="hi")])

    assert response.content == "hello world"
    assert response.usage == {"prompt_tokens": 5, "completion_tokens": 2}
    assert captured["url"] == f"{HOST}/serving-endpoints/my-model/invocations"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]


# ── Case 2: HTTP 429 MUST retry up to 3 times ──────────────────────────────


@pytest.mark.asyncio
async def test_llm_complete_retries_on_429(monkeypatch) -> None:
    # Patch asyncio.sleep so the test doesn't actually wait.
    import asyncio as _asyncio

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(429, json={"error": "rate"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "done"}}], "usage": {}},
        )

    provider = DatabricksLLMProvider(
        host=HOST, token=TOKEN, client=_make_client(handler)
    )
    response = await provider.complete([LLMMessage(role="user", content="hi")])
    assert response.content == "done"
    assert call_count["n"] == 3
    assert len(sleeps) == 2  # one before retry 2, one before retry 3


@pytest.mark.asyncio
async def test_llm_complete_raises_after_max_429_retries(monkeypatch) -> None:
    import asyncio as _asyncio

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "still rate-limited"})

    provider = DatabricksLLMProvider(
        host=HOST, token=TOKEN, client=_make_client(handler)
    )
    with pytest.raises(LLMProviderError, match="Rate-limited"):
        await provider.complete([LLMMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_llm_complete_raises_on_4xx_other_than_429() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    provider = DatabricksLLMProvider(
        host=HOST, token=TOKEN, client=_make_client(handler)
    )
    with pytest.raises(LLMProviderError, match="400"):
        await provider.complete([LLMMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_llm_embed_returns_vectors_in_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [float(i)] * 3} for i, _ in enumerate(body["input"])
                ]
            },
        )

    provider = DatabricksLLMProvider(
        host=HOST, token=TOKEN, client=_make_client(handler)
    )
    vecs = await provider.embed(["a", "b", "c"])
    assert vecs == [[0.0] * 3, [1.0] * 3, [2.0] * 3]


# EXT-3: per-call model parameter overrides the constructor endpoint.


@pytest.mark.asyncio
async def test_llm_complete_uses_model_parameter_when_provided() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    provider = DatabricksLLMProvider(
        host=HOST,
        token=TOKEN,
        completion_endpoint="constructor-default",
        client=_make_client(handler),
    )
    # Per-call override targets a different serving endpoint.
    await provider.complete(
        [LLMMessage(role="user", content="hi")], model="per-call-big"
    )
    assert "per-call-big/invocations" in captured["url"]
    assert "constructor-default" not in captured["url"]


@pytest.mark.asyncio
async def test_llm_complete_falls_back_to_constructor_endpoint() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    provider = DatabricksLLMProvider(
        host=HOST,
        token=TOKEN,
        completion_endpoint="default-endpoint",
        client=_make_client(handler),
    )
    await provider.complete([LLMMessage(role="user", content="hi")])
    assert "default-endpoint/invocations" in captured["url"]


# Case 10 — auth failure raises ProviderError subclass.
@pytest.mark.asyncio
async def test_llm_complete_raises_on_auth_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    provider = DatabricksLLMProvider(
        host=HOST, token=TOKEN, client=_make_client(handler)
    )
    with pytest.raises(LLMProviderError, match="401"):
        await provider.complete([LLMMessage(role="user", content="hi")])


# ═══════════════════════════════════════════════════════════════════════════
# DatabricksQueryProvider
# ═══════════════════════════════════════════════════════════════════════════


def _statement_success(rows: list[list], columns: list[str], row_count: int | None = None) -> dict:
    return {
        "statement_id": "stmt-1",
        "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": [{"name": c} for c in columns]}},
        "result": {"data_array": rows, "row_count": row_count if row_count is not None else len(rows)},
    }


def _statement_failed(message: str) -> dict:
    return {
        "statement_id": "stmt-1",
        "status": {"state": "FAILED", "error": {"message": message}},
    }


@pytest.mark.asyncio
async def test_query_validate_select_1_returns_true_none() -> None:
    """Case 4."""
    captured_statements: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_statements.append(body["statement"])
        return httpx.Response(200, json=_statement_success([], []))

    provider = DatabricksQueryProvider(
        host=HOST, token=TOKEN, warehouse_id="wh", client=_make_client(handler)
    )
    ok, err = await provider.validate("SELECT 1")
    assert ok is True
    assert err is None
    # The wire SQL is EXPLAIN-prefixed.
    assert captured_statements[-1].startswith("EXPLAIN ")


@pytest.mark.asyncio
async def test_query_validate_syntax_error_returns_false_and_message() -> None:
    """Case 5."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_statement_failed("syntax error near 'oops'")
        )

    provider = DatabricksQueryProvider(
        host=HOST, token=TOKEN, warehouse_id="wh", client=_make_client(handler)
    )
    ok, err = await provider.validate("SELECT oops FROM nope")
    assert ok is False
    assert err and "syntax" in err


@pytest.mark.asyncio
async def test_query_execute_respects_limit_and_truncated() -> None:
    """Case 6 — execute with limit on a table that returns >= limit rows."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # The provider wraps with `LIMIT <n>`.
        assert "LIMIT 5" in body["statement"]
        rows = [[i] for i in range(5)]
        return httpx.Response(
            200, json=_statement_success(rows, ["n"], row_count=5)
        )

    provider = DatabricksQueryProvider(
        host=HOST, token=TOKEN, warehouse_id="wh", client=_make_client(handler)
    )
    result = await provider.execute("SELECT n FROM big_table", limit=5)
    assert result.row_count == 5
    assert result.truncated is True
    assert result.columns == ["n"]
    assert result.rows == [{"n": i} for i in range(5)]


@pytest.mark.asyncio
async def test_query_execute_under_limit_not_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        rows = [[1], [2]]
        return httpx.Response(
            200, json=_statement_success(rows, ["n"], row_count=2)
        )

    provider = DatabricksQueryProvider(
        host=HOST, token=TOKEN, warehouse_id="wh", client=_make_client(handler)
    )
    result = await provider.execute("SELECT n FROM t", limit=10)
    assert result.truncated is False


@pytest.mark.asyncio
async def test_query_execute_polls_on_running_state() -> None:
    """Statement comes back RUNNING; provider polls until SUCCEEDED."""

    state_sequence = iter(["RUNNING", "RUNNING", "SUCCEEDED"])

    def handler(request: httpx.Request) -> httpx.Response:
        state = next(state_sequence)
        if state == "SUCCEEDED":
            return httpx.Response(
                200, json=_statement_success([[1]], ["n"], row_count=1)
            )
        return httpx.Response(
            200,
            json={
                "statement_id": "stmt-1",
                "status": {"state": state},
            },
        )

    provider = DatabricksQueryProvider(
        host=HOST,
        token=TOKEN,
        warehouse_id="wh",
        poll_interval=0.001,
        client=_make_client(handler),
    )
    result = await provider.execute("SELECT 1", limit=10)
    assert result.row_count == 1


@pytest.mark.asyncio
async def test_query_execute_raises_on_failed_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_statement_failed("table not found"))

    provider = DatabricksQueryProvider(
        host=HOST, token=TOKEN, warehouse_id="wh", client=_make_client(handler)
    )
    with pytest.raises(QueryProviderError, match="table not found"):
        await provider.execute("SELECT * FROM nope", limit=10)


@pytest.mark.asyncio
async def test_query_execute_with_user_token_uses_that_token() -> None:
    """EXT-6: per-user credential pass-through."""
    captured_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers.get("Authorization"))
        return httpx.Response(
            200, json=_statement_success([[1]], ["n"], row_count=1)
        )

    provider = DatabricksQueryProvider(
        host=HOST, token=TOKEN, warehouse_id="wh", client=_make_client(handler)
    )
    await provider.execute("SELECT 1", user_token="user-token-xyz")
    assert captured_auth[-1] == "Bearer user-token-xyz"


# ═══════════════════════════════════════════════════════════════════════════
# DatabricksStoreProvider
# ═══════════════════════════════════════════════════════════════════════════


class _RecordingQueryProvider(QueryProvider):
    """In-memory dict + executed-SQL log; used to test the Store wrapper."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self.executed: list[str] = []

    async def execute(self, sql, limit=10_000, user_token=None) -> QueryResult:
        self.executed.append(sql)
        # Naive routing — enough for store tests.
        lower = sql.lower()
        if lower.startswith("select value from"):
            key = _extract_quoted_after(sql, "key = ")
            raw = self._data.get(key)
            if raw is None:
                return QueryResult(
                    columns=["value"],
                    rows=[],
                    row_count=0,
                    truncated=False,
                    duration_ms=0,
                )
            return QueryResult(
                columns=["value"],
                rows=[{"value": raw}],
                row_count=1,
                truncated=False,
                duration_ms=0,
            )
        if lower.startswith("select key from"):
            prefix_literal = _extract_quoted_after(sql, "LIKE ")
            prefix = prefix_literal.rstrip("%")
            matching = sorted(k for k in self._data if k.startswith(prefix))
            return QueryResult(
                columns=["key"],
                rows=[{"key": k} for k in matching],
                row_count=len(matching),
                truncated=False,
                duration_ms=0,
            )
        if lower.startswith("merge into"):
            key = _extract_quoted_after(sql, "SELECT ")
            value = _extract_quoted_after(sql, " AS key, ")
            self._data[key] = value
        elif lower.startswith("delete from"):
            key = _extract_quoted_after(sql, "key = ")
            self._data.pop(key, None)
        return QueryResult(
            columns=[], rows=[], row_count=0, truncated=False, duration_ms=0
        )

    async def validate(self, sql, user_token=None) -> tuple[bool, str | None]:
        return (True, None)


def _extract_quoted_after(sql: str, prefix: str) -> str:
    """Crude single-quoted-literal extractor for the test double."""
    idx = sql.find(prefix)
    if idx == -1:
        return ""
    start = sql.find("'", idx)
    if start == -1:
        return ""
    end = start + 1
    while end < len(sql):
        if sql[end] == "'":
            if end + 1 < len(sql) and sql[end + 1] == "'":
                end += 2
                continue
            break
        end += 1
    return sql[start + 1 : end].replace("''", "'")


@pytest.mark.asyncio
async def test_store_put_then_get_returns_identical_dict() -> None:
    """Case 8."""
    query = _RecordingQueryProvider()
    store = DatabricksStoreProvider(table="main.tiri.kv", query=query)

    payload = {"answer": 42, "tags": ["a", "b"], "nested": {"k": "v"}}
    await store.put("foo", payload)
    got = await store.get("foo")
    assert got == payload


@pytest.mark.asyncio
async def test_store_get_missing_key_returns_none() -> None:
    """Case 9."""
    query = _RecordingQueryProvider()
    store = DatabricksStoreProvider(table="main.tiri.kv", query=query)
    assert await store.get("not-set") is None


@pytest.mark.asyncio
async def test_store_list_keys_returns_lexicographic_order_and_filters() -> None:
    query = _RecordingQueryProvider()
    store = DatabricksStoreProvider(table="main.tiri.kv", query=query)
    await store.put("conv:a:1", {"x": 1})
    await store.put("conv:a:2", {"x": 2})
    await store.put("room:r1", {"x": 3})

    conv_keys = await store.list_keys("conv:")
    assert conv_keys == ["conv:a:1", "conv:a:2"]


@pytest.mark.asyncio
async def test_store_delete_removes_key() -> None:
    query = _RecordingQueryProvider()
    store = DatabricksStoreProvider(table="main.tiri.kv", query=query)
    await store.put("k", {"v": 1})
    await store.delete("k")
    assert await store.get("k") is None


@pytest.mark.asyncio
async def test_store_quotes_single_quotes_in_keys() -> None:
    """Defence-in-depth: keys with embedded quotes round-trip cleanly."""
    query = _RecordingQueryProvider()
    store = DatabricksStoreProvider(table="main.tiri.kv", query=query)
    tricky = "room:o'malley:1"
    await store.put(tricky, {"v": "x"})
    assert await store.get(tricky) == {"v": "x"}


# ═══════════════════════════════════════════════════════════════════════════
# DatabricksVectorProvider
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_vector_upsert_then_query_returns_entry_in_top_result() -> None:
    """Case 7."""
    stored: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/upsert-data"):
            body = json.loads(request.content)
            for row in json.loads(body["inputs_json"]):
                stored[row["id"]] = row
            return httpx.Response(200, json={"status": "OK"})
        if path.endswith("/query"):
            body = json.loads(request.content)
            rows = []
            for row in stored.values():
                payload = {k: v for k, v in row.items() if k != "vector"}
                rows.append([row["id"], 0.99] + list(payload.values()))
            cols = ["id", "score"] + [
                k for k in next(iter(stored.values())).keys() if k != "vector"
            ]
            # Re-shape so id/score come first in declared order.
            col_names = ["id", "score"]
            payload_cols = [k for k in next(iter(stored.values())).keys() if k not in {"id", "vector"}]
            col_names = ["id", "score"] + payload_cols
            data_rows = []
            for row in stored.values():
                data_rows.append(
                    [row["id"], 0.99]
                    + [row[c] for c in payload_cols]
                )
            return httpx.Response(
                200,
                json={
                    "manifest": {"columns": [{"name": c} for c in col_names]},
                    "result": {"data_array": data_rows},
                },
            )
        return httpx.Response(404)

    provider = DatabricksVectorProvider(
        host=HOST,
        token=TOKEN,
        index="main.tiri.idx",
        client=_make_client(handler),
    )
    await provider.upsert(
        "ex1", [0.1, 0.2, 0.3], {"question": "q1", "sql": "select 1", "room_id": "r"}
    )
    matches = await provider.query([0.1, 0.2, 0.3], top_k=5)
    assert len(matches) == 1
    assert matches[0].id == "ex1"
    assert matches[0].payload["question"] == "q1"


@pytest.mark.asyncio
async def test_vector_query_orders_by_score_descending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "manifest": {
                    "columns": [
                        {"name": "id"},
                        {"name": "score"},
                        {"name": "question"},
                    ]
                },
                "result": {
                    "data_array": [
                        ["a", 0.5, "qa"],
                        ["b", 0.9, "qb"],
                        ["c", 0.7, "qc"],
                    ]
                },
            },
        )

    provider = DatabricksVectorProvider(
        host=HOST, token=TOKEN, index="idx", client=_make_client(handler)
    )
    matches = await provider.query([0.0], top_k=3)
    assert [m.id for m in matches] == ["b", "c", "a"]


@pytest.mark.asyncio
async def test_vector_request_failure_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    provider = DatabricksVectorProvider(
        host=HOST, token=TOKEN, index="idx", client=_make_client(handler)
    )
    with pytest.raises(VectorProviderError, match="500"):
        await provider.upsert("x", [0.0], {})


# ═══════════════════════════════════════════════════════════════════════════
# DatabricksCatalogProvider
# ═══════════════════════════════════════════════════════════════════════════


def _table_info(
    full_name: str,
    cols: list[tuple[str, str]],
    comment: str | None = None,
    column_comments: dict[str, str] | None = None,
):
    """Build a stub TableInfo-like object with .columns and .properties."""
    column_comments = column_comments or {}
    info = MagicMock()
    info.full_name = full_name
    info.comment = comment
    info.properties = {}
    info.columns = []
    for name, type_text in cols:
        c = MagicMock()
        c.name = name
        c.type_text = type_text
        c.type_name = type_text
        c.comment = column_comments.get(name)
        info.columns.append(c)
    return info


def test_catalog_constructor_requires_host_and_token() -> None:
    from tiri.providers.base import CatalogProviderError

    with pytest.raises(CatalogProviderError, match="host"):
        DatabricksCatalogProvider(host="", token=TOKEN, client=MagicMock())
    with pytest.raises(CatalogProviderError, match="token"):
        DatabricksCatalogProvider(host=HOST, token="", client=MagicMock())


@pytest.mark.asyncio
async def test_catalog_get_table_meta_returns_columns() -> None:
    """Case 3."""
    client = MagicMock()
    client.tables.get.return_value = _table_info(
        "main.x.t", [("id", "BIGINT"), ("name", "STRING")]
    )
    provider = DatabricksCatalogProvider(host=HOST, token=TOKEN, client=client)
    meta = await provider.get_table_meta("main.x.t")
    assert meta.full_name == "main.x.t"
    assert [(c.name, c.data_type) for c in meta.columns] == [
        ("id", "BIGINT"),
        ("name", "STRING"),
    ]
    # Descriptive fields stay empty — that's the MetadataProvider's job.
    assert meta.description == ""
    for c in meta.columns:
        assert c.description == ""
        assert c.synonyms == []


@pytest.mark.asyncio
async def test_catalog_get_table_meta_nonexistent_raises() -> None:
    from databricks.sdk.errors import NotFound

    client = MagicMock()
    client.tables.get.side_effect = NotFound("table 'main.x.t' not found")
    provider = DatabricksCatalogProvider(host=HOST, token=TOKEN, client=client)
    with pytest.raises(TableNotFoundError, match="not found"):
        await provider.get_table_meta("main.x.t")


# ═══════════════════════════════════════════════════════════════════════════
# UCAnnotationsMetadataProvider
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_uc_annotations_sets_table_description() -> None:
    client = MagicMock()
    client.tables.get.return_value = _table_info(
        "main.x.t", [("id", "BIGINT")], comment="orders table"
    )
    provider = UCAnnotationsMetadataProvider(
        host=HOST, token=TOKEN, client=client, sample_values_enabled=False
    )
    tables = {"main.x.t": TableMeta(full_name="main.x.t", columns=[ColumnMeta(name="id", data_type="BIGINT")])}
    await provider.enrich(tables, _room_config())
    assert tables["main.x.t"].description == "orders table"
    assert "uc_annotations" in tables["main.x.t"].metadata_sources


@pytest.mark.asyncio
async def test_uc_annotations_sets_column_descriptions() -> None:
    client = MagicMock()
    client.tables.get.return_value = _table_info(
        "main.x.t",
        [("id", "BIGINT"), ("status", "STRING")],
        column_comments={"status": "order status"},
    )
    provider = UCAnnotationsMetadataProvider(
        host=HOST, token=TOKEN, client=client, sample_values_enabled=False
    )
    tables = {
        "main.x.t": TableMeta(
            full_name="main.x.t",
            columns=[
                ColumnMeta(name="id", data_type="BIGINT"),
                ColumnMeta(name="status", data_type="STRING"),
            ],
        )
    }
    await provider.enrich(tables, _room_config())
    desc_by_name = {c.name: c.description for c in tables["main.x.t"].columns}
    assert desc_by_name == {"id": "", "status": "order status"}


@pytest.mark.asyncio
async def test_uc_annotations_records_conflict_when_overriding_description() -> None:
    client = MagicMock()
    client.tables.get.return_value = _table_info(
        "main.x.t", [("id", "BIGINT")], comment="from UC"
    )
    provider = UCAnnotationsMetadataProvider(
        host=HOST, token=TOKEN, client=client, sample_values_enabled=False
    )
    tables = {
        "main.x.t": TableMeta(
            full_name="main.x.t",
            description="from earlier provider",
            columns=[ColumnMeta(name="id", data_type="BIGINT")],
        )
    }
    await provider.enrich(tables, _room_config())
    assert tables["main.x.t"].description == "from UC"
    assert len(tables["main.x.t"].conflicts) == 1
    conflict = tables["main.x.t"].conflicts[0]
    assert conflict.field == "description"
    assert conflict.resolved_to == "uc_annotations"
    assert conflict.values == {
        "existing": "from earlier provider",
        "uc_annotations": "from UC",
    }


@pytest.mark.asyncio
async def test_uc_annotations_skips_missing_table_without_raising() -> None:
    from databricks.sdk.errors import NotFound

    client = MagicMock()
    client.tables.get.side_effect = NotFound("nope")
    provider = UCAnnotationsMetadataProvider(
        host=HOST, token=TOKEN, client=client, sample_values_enabled=False
    )
    tables = {"main.x.t": TableMeta(full_name="main.x.t")}
    await provider.enrich(tables, _room_config())  # must not raise
    assert tables["main.x.t"].description == ""


@pytest.mark.asyncio
async def test_uc_annotations_populates_sample_values_for_strings() -> None:
    client = MagicMock()
    client.tables.get.return_value = _table_info(
        "main.x.t", [("status", "STRING")]
    )

    class _StubQuery(QueryProvider):
        async def execute(self, sql, limit=10_000, user_token=None):
            return QueryResult(
                columns=["status"],
                rows=[
                    {"status": "active"},
                    {"status": "trial"},
                    {"status": "active"},  # duplicate — provider dedupes
                ],
                row_count=3,
                truncated=False,
                duration_ms=0,
            )

        async def validate(self, sql, user_token=None):
            return (True, None)

    provider = UCAnnotationsMetadataProvider(
        host=HOST,
        token=TOKEN,
        client=client,
        query=_StubQuery(),
        sample_values_enabled=True,
    )
    tables = {
        "main.x.t": TableMeta(
            full_name="main.x.t",
            columns=[ColumnMeta(name="status", data_type="STRING")],
        )
    }
    await provider.enrich(tables, _room_config())
    assert tables["main.x.t"].columns[0].sample_values == ["active", "trial"]


@pytest.mark.asyncio
async def test_uc_annotations_does_not_modify_physical_fields() -> None:
    """Contract: enrich() MUST NOT touch full_name, column.name, or column.data_type."""
    client = MagicMock()
    client.tables.get.return_value = _table_info(
        "main.x.t",
        [("id", "BIGINT"), ("status", "STRING")],
        comment="table",
        column_comments={"id": "primary key"},
    )
    provider = UCAnnotationsMetadataProvider(
        host=HOST, token=TOKEN, client=client, sample_values_enabled=False
    )
    cols_before = [ColumnMeta(name="id", data_type="BIGINT"), ColumnMeta(name="status", data_type="STRING")]
    tables = {"main.x.t": TableMeta(full_name="main.x.t", columns=cols_before)}
    await provider.enrich(tables, _room_config())
    table = tables["main.x.t"]
    assert table.full_name == "main.x.t"
    assert [(c.name, c.data_type) for c in table.columns] == [
        ("id", "BIGINT"),
        ("status", "STRING"),
    ]
