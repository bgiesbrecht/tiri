"""Tests for tiri.providers.local.*.

Covers the 13 test cases in docs/local_providers.md. Cases that require an
external service (Ollama) use httpx.MockTransport; the OpenAI/Anthropic
network paths use SDK-level test doubles.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from tiri.data_models import (
    ColumnMeta,
    LLMMessage,
    LLMResponse,
    RoomConfig,
    TableMeta,
)
from tiri.providers.base import (
    LLMProviderError,
    MetadataProviderError,
    TableNotFoundError,
)
from tiri.providers.local.catalog_static import StaticCatalogProvider
from tiri.providers.local.llm_anthropic import AnthropicLLMProvider
from tiri.providers.local.llm_ollama import OllamaLLMProvider
from tiri.providers.local.llm_openai import OpenAILLMProvider
from tiri.providers.local.metadata_dbt import DbtMetadataProvider
from tiri.providers.local.metadata_static import StaticMetadataProvider
from tiri.providers.local.metadata_yaml import YAMLMetadataProvider
from tiri.providers.local.query_duckdb import DuckDBQueryProvider
from tiri.providers.local.store_sqlite import SQLiteStoreProvider
from tiri.providers.local.vector_chroma import ChromaVectorProvider


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _room_config() -> RoomConfig:
    return RoomConfig(
        room_id="r", title="r", tables=["tpch.sf1.lineitem"], warehouse_id="wh"
    )


# ═══════════════════════════════════════════════════════════════════════════
# StaticCatalogProvider — cases 1, 2
# ═══════════════════════════════════════════════════════════════════════════


def test_static_catalog_returns_physical_schema_with_empty_descriptive_fields() -> None:
    p = StaticCatalogProvider(schema_file=str(FIXTURES / "schemas.json"))

    import asyncio

    meta = asyncio.run(p.get_table_meta("tpch.sf1.lineitem"))
    assert meta.full_name == "tpch.sf1.lineitem"
    assert [(c.name, c.data_type) for c in meta.columns][:2] == [
        ("l_orderkey", "BIGINT"),
        ("l_extendedprice", "DECIMAL(15,2)"),
    ]
    # Descriptive fields MUST remain empty.
    assert meta.description == ""
    for c in meta.columns:
        assert c.description == ""
        assert c.synonyms == []


def test_static_catalog_unknown_table_raises_table_not_found() -> None:
    p = StaticCatalogProvider(schema_file=str(FIXTURES / "schemas.json"))

    import asyncio

    with pytest.raises(TableNotFoundError, match="not found"):
        asyncio.run(p.get_table_meta("tpch.sf1.does_not_exist"))


def test_static_catalog_list_tables_filters_by_catalog_and_schema() -> None:
    p = StaticCatalogProvider(schema_file=str(FIXTURES / "schemas.json"))

    import asyncio

    names = asyncio.run(p.list_tables("tpch", "sf1"))
    assert names == ["tpch.sf1.lineitem", "tpch.sf1.orders"]


# ═══════════════════════════════════════════════════════════════════════════
# DuckDBQueryProvider — cases 3, 4
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_duckdb_validate_valid_sql_returns_true_none() -> None:
    p = DuckDBQueryProvider()
    ok, err = await p.validate("SELECT 1 AS n")
    assert ok is True
    assert err is None


@pytest.mark.asyncio
async def test_duckdb_validate_syntax_error_returns_false_and_message() -> None:
    p = DuckDBQueryProvider()
    ok, err = await p.validate("SELECT FROM where")
    assert ok is False
    assert err and len(err) > 0


@pytest.mark.asyncio
async def test_duckdb_execute_select_1_returns_one_row() -> None:
    p = DuckDBQueryProvider()
    result = await p.execute("SELECT 1 AS n")
    assert result.row_count == 1
    assert result.rows == [{"n": 1}]
    assert result.columns == ["n"]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_duckdb_execute_truncates_at_limit() -> None:
    p = DuckDBQueryProvider()
    # range(100) returns 100 rows; cap at 5.
    result = await p.execute("SELECT range AS n FROM range(100)", limit=5)
    assert result.row_count == 5
    assert result.truncated is True


# ═══════════════════════════════════════════════════════════════════════════
# ChromaVectorProvider — case 5
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_chroma_upsert_query_delete_round_trip() -> None:
    # Unique collection_name per test — Chroma's in-process registry locks
    # collection dimensions to the first vector inserted, so separate tests
    # with different vector dimensions would otherwise collide.
    p = ChromaVectorProvider(path=":memory:", collection_name="round_trip")
    await p.upsert(
        "ex1", [1.0, 0.0, 0.0], {"question": "q1", "sql": "s1", "room_id": "r"}
    )
    await p.upsert(
        "ex2", [0.0, 1.0, 0.0], {"question": "q2", "sql": "s2", "room_id": "r"}
    )
    matches = await p.query([1.0, 0.0, 0.0], top_k=2)
    assert len(matches) == 2
    assert matches[0].id == "ex1"  # closest to the query vector
    assert matches[0].score >= matches[1].score

    await p.delete("ex1")
    matches = await p.query([1.0, 0.0, 0.0], top_k=2)
    assert all(m.id != "ex1" for m in matches)


@pytest.mark.asyncio
async def test_chroma_query_with_room_id_filter_scopes_to_room() -> None:
    p = ChromaVectorProvider(path=":memory:", collection_name="filter_test")
    await p.upsert("a", [1.0, 0.0], {"room_id": "r1", "question": "qa"})
    await p.upsert("b", [1.0, 0.0], {"room_id": "r2", "question": "qb"})
    matches = await p.query([1.0, 0.0], top_k=10, filter={"room_id": "r1"})
    assert {m.id for m in matches} == {"a"}


# ═══════════════════════════════════════════════════════════════════════════
# SQLiteStoreProvider — case 6 (full StoreProvider contract)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sqlite_get_missing_key_returns_none() -> None:
    s = SQLiteStoreProvider(path=":memory:")
    assert await s.get("nope") is None


@pytest.mark.asyncio
async def test_sqlite_put_then_get_round_trips_dict() -> None:
    s = SQLiteStoreProvider(path=":memory:")
    payload = {"answer": 42, "tags": ["a"], "nested": {"k": "v"}}
    await s.put("k1", payload)
    assert await s.get("k1") == payload


@pytest.mark.asyncio
async def test_sqlite_put_replaces_existing_value() -> None:
    s = SQLiteStoreProvider(path=":memory:")
    await s.put("k", {"v": 1})
    await s.put("k", {"v": 2})
    assert await s.get("k") == {"v": 2}


@pytest.mark.asyncio
async def test_sqlite_list_keys_returns_lexicographic_order() -> None:
    s = SQLiteStoreProvider(path=":memory:")
    await s.put("conv:b:1", {"x": 1})
    await s.put("conv:a:1", {"x": 2})
    await s.put("room:r1", {"x": 3})
    keys = await s.list_keys("conv:")
    assert keys == ["conv:a:1", "conv:b:1"]


@pytest.mark.asyncio
async def test_sqlite_delete_removes_key() -> None:
    s = SQLiteStoreProvider(path=":memory:")
    await s.put("k", {"v": 1})
    await s.delete("k")
    assert await s.get("k") is None


@pytest.mark.asyncio
async def test_sqlite_delete_missing_key_is_no_op() -> None:
    s = SQLiteStoreProvider(path=":memory:")
    await s.delete("nope")  # MUST NOT raise


# ═══════════════════════════════════════════════════════════════════════════
# AnthropicLLMProvider — case 7
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_anthropic_embed_raises_llmprovidererror() -> None:
    p = AnthropicLLMProvider(api_key="sk-x")
    with pytest.raises(LLMProviderError, match="embed"):
        await p.embed(["hi"])


@pytest.mark.asyncio
async def test_anthropic_complete_calls_sdk_with_split_system_messages(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = self  # nest namespace

        async def create(self, **kwargs):  # called as client.messages.create(**)
            captured["kwargs"] = kwargs

            class R:
                class _Usage:
                    input_tokens = 3
                    output_tokens = 1

                class _Block:
                    text = "ok"

                content = [_Block()]
                usage = _Usage()

            return R()

    fake = _FakeClient()
    p = AnthropicLLMProvider(api_key="sk-x", client=fake)  # type: ignore[arg-type]
    response = await p.complete(
        [
            LLMMessage(role="system", content="sys"),
            LLMMessage(role="user", content="hi"),
        ]
    )
    assert response.content == "ok"
    assert captured["kwargs"]["system"] == "sys"
    assert captured["kwargs"]["messages"] == [{"role": "user", "content": "hi"}]


# ═══════════════════════════════════════════════════════════════════════════
# OllamaLLMProvider — case 8 (HTTP-mocked)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ollama_complete_returns_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "llama3.3"
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": "ollama-ok"}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = OllamaLLMProvider(client=client)
    response = await p.complete([LLMMessage(role="user", content="hi")])
    assert response.content == "ollama-ok"


@pytest.mark.asyncio
async def test_ollama_embed_returns_vectors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = OllamaLLMProvider(client=client)
    vecs = await p.embed(["a", "b"])
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]


# ═══════════════════════════════════════════════════════════════════════════
# StaticMetadataProvider — cases 9, 10
# ═══════════════════════════════════════════════════════════════════════════


def _tables_with_lineitem() -> dict[str, TableMeta]:
    return {
        "tpch.sf1.lineitem": TableMeta(
            full_name="tpch.sf1.lineitem",
            columns=[
                ColumnMeta(name="l_returnflag", data_type="STRING"),
                ColumnMeta(name="l_extendedprice", data_type="DECIMAL(15,2)"),
            ],
        )
    }


@pytest.mark.asyncio
async def test_static_metadata_sets_description_and_extends_synonyms() -> None:
    """Case 9."""
    p = StaticMetadataProvider(
        name="test_meta",
        data={
            "tpch.sf1.lineitem": {
                "description": "Line items",
                "synonyms": ["lines", "order_lines"],
                "columns": {
                    "l_returnflag": {
                        "description": "Return status",
                        "synonyms": ["return", "rflag"],
                    }
                },
            }
        },
    )
    tables = _tables_with_lineitem()
    tables["tpch.sf1.lineitem"].synonyms = ["existing"]

    await p.enrich(tables, _room_config())

    table = tables["tpch.sf1.lineitem"]
    assert table.description == "Line items"
    # List fields accumulate, not replace.
    assert table.synonyms == ["existing", "lines", "order_lines"]
    # metadata_sources populated.
    assert "test_meta" in table.metadata_sources

    col = next(c for c in table.columns if c.name == "l_returnflag")
    assert col.description == "Return status"
    assert col.synonyms == ["return", "rflag"]
    assert col.metadata_source == "test_meta"


@pytest.mark.asyncio
async def test_static_metadata_skips_table_with_no_data_silently() -> None:
    """Case 10."""
    p = StaticMetadataProvider(name="test_meta", data={})
    tables = _tables_with_lineitem()
    await p.enrich(tables, _room_config())  # MUST NOT raise
    # Untouched.
    assert tables["tpch.sf1.lineitem"].description == ""
    assert tables["tpch.sf1.lineitem"].metadata_sources == []


@pytest.mark.asyncio
async def test_static_metadata_records_conflict_on_scalar_override() -> None:
    p = StaticMetadataProvider(
        name="test_meta",
        data={"tpch.sf1.lineitem": {"description": "new desc"}},
    )
    tables = _tables_with_lineitem()
    tables["tpch.sf1.lineitem"].description = "old desc"
    await p.enrich(tables, _room_config())
    table = tables["tpch.sf1.lineitem"]
    assert table.description == "new desc"
    assert len(table.conflicts) == 1
    assert table.conflicts[0].field == "description"
    assert table.conflicts[0].resolved_to == "test_meta"


@pytest.mark.asyncio
async def test_static_metadata_does_not_modify_physical_fields() -> None:
    """Contract: MUST NOT modify full_name, column.name, or column.data_type."""
    p = StaticMetadataProvider(
        name="test_meta",
        data={
            "tpch.sf1.lineitem": {
                "description": "X",
                "columns": {"l_returnflag": {"description": "Y"}},
            }
        },
    )
    tables = _tables_with_lineitem()
    await p.enrich(tables, _room_config())
    table = tables["tpch.sf1.lineitem"]
    assert table.full_name == "tpch.sf1.lineitem"
    assert [(c.name, c.data_type) for c in table.columns] == [
        ("l_returnflag", "STRING"),
        ("l_extendedprice", "DECIMAL(15,2)"),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# YAMLMetadataProvider — case 11
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_yaml_metadata_populates_declared_fields() -> None:
    p = YAMLMetadataProvider(name="yaml1", path=str(FIXTURES / "metadata.yaml"))
    tables = _tables_with_lineitem()
    await p.enrich(tables, _room_config())

    table = tables["tpch.sf1.lineitem"]
    assert table.description == "Line items on customer orders"
    assert table.grain == "one row per line item"
    assert table.domain == "sales"
    assert "line items" in table.synonyms
    assert "order lines" in table.synonyms

    col_by_name = {c.name: c for c in table.columns}
    rf = col_by_name["l_returnflag"]
    assert rf.description == "Return status"
    assert rf.value_description.startswith("R=returned")
    assert rf.semantic_type == "category"
    assert "return" in rf.synonyms

    ep = col_by_name["l_extendedprice"]
    assert ep.semantic_type == "currency"
    assert ep.currency_code == "USD"
    assert ep.is_high_cardinality is True


@pytest.mark.asyncio
async def test_yaml_metadata_missing_file_raises_provider_error() -> None:
    with pytest.raises(MetadataProviderError, match="not found"):
        YAMLMetadataProvider(name="x", path="/nonexistent/metadata.yaml")


# ═══════════════════════════════════════════════════════════════════════════
# dbt stub
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dbt_metadata_stub_raises_not_implemented() -> None:
    p = DbtMetadataProvider(name="dbt", manifest_path="x.json")
    with pytest.raises(MetadataProviderError, match="not yet implemented"):
        await p.enrich({}, _room_config())


# ═══════════════════════════════════════════════════════════════════════════
# Case 12: all local providers constructable with no network calls
# ═══════════════════════════════════════════════════════════════════════════


def test_all_local_providers_constructable_with_no_network(monkeypatch) -> None:
    import socket as _socket

    def no_network(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Network access during construction")

    monkeypatch.setattr(_socket.socket, "connect", no_network)
    monkeypatch.setattr(_socket.socket, "connect_ex", no_network)

    StaticCatalogProvider(schema_file=str(FIXTURES / "schemas.json"))
    DuckDBQueryProvider()
    ChromaVectorProvider(path=":memory:")
    SQLiteStoreProvider(path=":memory:")
    StaticMetadataProvider(name="x", data={})
    YAMLMetadataProvider(name="y", path=str(FIXTURES / "metadata.yaml"))
    # The LLM providers create SDK clients but the SDKs are lazy about network.
    OpenAILLMProvider(api_key="sk-x")
    AnthropicLLMProvider(api_key="sk-x")
    OllamaLLMProvider()


# ═══════════════════════════════════════════════════════════════════════════
# OpenAI: smoke test the SDK path with a stub
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openai_complete_calls_chat_completions(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeChat:
        async def create(self, **kwargs):
            captured["kwargs"] = kwargs

            class R:
                class _Choice:
                    class _Msg:
                        content = "openai-ok"

                    message = _Msg()

                class _Usage:
                    prompt_tokens = 3
                    completion_tokens = 1

                choices = [_Choice()]
                usage = _Usage()

            return R()

    class _FakeCompletions:
        def __init__(self):
            self.create = _FakeChat().create

    class _FakeClient:
        class chat:
            completions = _FakeCompletions()

    fake = _FakeClient()
    p = OpenAILLMProvider(api_key="sk-x", client=fake)  # type: ignore[arg-type]
    response = await p.complete([LLMMessage(role="user", content="hi")])
    assert response.content == "openai-ok"
    assert captured["kwargs"]["model"] == "gpt-4o"
    assert captured["kwargs"]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_openai_complete_honors_per_call_model_parameter() -> None:
    """EXT-3: per-call `model` parameter overrides the constructor default."""
    captured: dict[str, Any] = {}

    class _FakeChat:
        async def create(self, **kwargs):
            captured["kwargs"] = kwargs

            class R:
                class _Choice:
                    class _Msg:
                        content = "x"

                    message = _Msg()

                choices = [_Choice()]
                usage = None

            return R()

    class _FakeClient:
        class chat:
            class completions:
                create = _FakeChat().create

    p = OpenAILLMProvider(
        api_key="sk-x", model="default-gpt", client=_FakeClient()
    )  # type: ignore[arg-type]
    await p.complete(
        [LLMMessage(role="user", content="hi")], model="override-gpt"
    )
    assert captured["kwargs"]["model"] == "override-gpt"
