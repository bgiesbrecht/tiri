"""Tests for tiri.knowledge.* — covers all 12 cases in docs/knowledge_store.md
plus RoomConfigMetadataProvider behavior.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tiri.data_models import (
    ColumnMeta,
    ColumnOverride,
    ConversationTurn,
    ExampleSQL,
    JoinSpec,
    LLMMessage,
    LLMResponse,
    MetadataConflict,
    Metric,
    RoomConfig,
    SqlSnippet,
    TableMeta,
    VectorMatch,
)
from tiri.knowledge.context_builder import ContextBuilder
from tiri.knowledge.example_indexer import ExampleIndexer
from tiri.knowledge.metadata_fetcher import MetadataFetcher
from tiri.knowledge.room_config_metadata import RoomConfigMetadataProvider
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    MetadataProvider,
    QueryProvider,
    TableNotFoundError,
    VectorProvider,
)


# ── Test doubles ────────────────────────────────────────────────────────────


class _FakeCatalog(CatalogProvider):
    """Records every get_table_meta call. Supports configurable missing tables."""

    def __init__(self, tables: dict[str, list[tuple[str, str]]], missing: set[str] | None = None) -> None:
        self._tables = tables
        self._missing = missing or set()
        self.get_calls: list[str] = []

    async def get_table_meta(self, full_name: str) -> TableMeta:
        self.get_calls.append(full_name)
        if full_name in self._missing:
            raise TableNotFoundError(full_name)
        columns = [
            ColumnMeta(name=n, data_type=t) for n, t in self._tables.get(full_name, [])
        ]
        return TableMeta(full_name=full_name, columns=columns)

    async def list_tables(self, catalog: str, schema: str) -> list[str]:
        return []

    async def list_schemas(self, catalog: str) -> list[str]:
        return []

    async def search_tables(self, query: str, limit: int = 10) -> list[TableMeta]:
        return []


class _MetadataDouble(MetadataProvider):
    """Applies a pre-configured `description` and `synonyms` per table."""

    def __init__(self, name: str, table_data: dict[str, dict]) -> None:
        self._name = name
        self._table_data = table_data
        self.order_marker: list[str] = []  # appended by test fixtures to verify ordering

    @property
    def name(self) -> str:
        return self._name

    async def enrich(self, tables: dict[str, TableMeta], room_config: RoomConfig) -> None:
        self.order_marker.append(self._name)
        for full_name, entry in self._table_data.items():
            tm = tables.get(full_name)
            if tm is None:
                continue
            new_desc = entry.get("description")
            if new_desc:
                if tm.description and tm.description != new_desc:
                    tm.conflicts.append(
                        MetadataConflict(
                            table=full_name,
                            column=None,
                            field="description",
                            values={"existing": tm.description, self._name: new_desc},
                            resolved_to=self._name,
                        )
                    )
                tm.description = new_desc
            for s in entry.get("synonyms", []):
                if s not in tm.synonyms:
                    tm.synonyms.append(s)
            if self._name not in tm.metadata_sources:
                tm.metadata_sources.append(self._name)


class _FakeLLM(LLMProvider):
    """Counts embed/complete/stream calls. embed returns simple deterministic vectors."""

    def __init__(self) -> None:
        self.embed_calls: list[list[str]] = []
        self.complete_calls = 0
        self.stream_calls = 0

    async def complete(self, messages, temperature=0.0, max_tokens=2048, task="sql", model=None) -> LLMResponse:
        self.complete_calls += 1
        return LLMResponse(content="", usage={}, raw=None)

    async def stream(self, messages, temperature=0.0, task="sql", model=None) -> AsyncIterator[str]:
        self.stream_calls += 1
        yield ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        # Each text → a 3-dim vector with the index in position 0.
        return [[float(i), 0.0, 0.0] for i, _ in enumerate(texts)]


class _FakeVector(VectorProvider):
    """In-memory dict keyed by id. Honors {"room_id": X} filter."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def upsert(self, id, vector, payload) -> None:
        self._data[id] = {"vector": vector, "payload": dict(payload)}

    async def query(self, vector, top_k=5, filter=None) -> list[VectorMatch]:
        room_id = (filter or {}).get("room_id")
        items = []
        for k, v in self._data.items():
            if room_id is not None and v["payload"].get("room_id") != room_id:
                continue
            # Simple negative-Euclidean score so larger vector[0] → higher score.
            score = -sum(
                (a - b) ** 2 for a, b in zip(vector, v["vector"])
            )
            items.append(
                VectorMatch(id=k, score=score, payload=dict(v["payload"]))
            )
        items.sort(key=lambda m: m.score, reverse=True)
        return items[:top_k]

    async def delete(self, id) -> None:
        self._data.pop(id, None)

    async def list_ids(self, filter=None) -> list[str]:
        room_id = (filter or {}).get("room_id")
        if room_id is None:
            return list(self._data.keys())
        return [
            k for k, v in self._data.items()
            if v["payload"].get("room_id") == room_id
        ]


def _room_config(
    *,
    room_id: str = "r1",
    tables: list[str] | None = None,
    examples: list[ExampleSQL] | None = None,
    column_overrides: list[ColumnOverride] | None = None,
    sql_filters: list[SqlSnippet] | None = None,
    sql_expressions: list[SqlSnippet] | None = None,
    sql_measures: list[SqlSnippet] | None = None,
    metrics: list[Metric] | None = None,
    text_instruction: str = "",
    joins: list[JoinSpec] | None = None,
) -> RoomConfig:
    return RoomConfig(
        room_id=room_id,
        title="r",
        tables=tables or ["main.x.t"],
        warehouse_id="wh",
        text_instruction=text_instruction,
        examples=examples or [],
        joins=joins or [],
        sql_filters=sql_filters or [],
        sql_expressions=sql_expressions or [],
        sql_measures=sql_measures or [],
        metrics=metrics or [],
        column_overrides=column_overrides or [],
    )


# ═══════════════════════════════════════════════════════════════════════════
# MetadataFetcher — cases 1-6
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_returns_one_tablemeta_per_table() -> None:
    """Case 1."""
    catalog = _FakeCatalog(
        tables={
            "main.x.t1": [("id", "BIGINT")],
            "main.x.t2": [("name", "STRING")],
        }
    )
    fetcher = MetadataFetcher(catalog, metadata_providers=[])
    cfg = _room_config(tables=["main.x.t1", "main.x.t2"])
    tables = await fetcher.fetch(cfg)
    assert set(tables) == {"main.x.t1", "main.x.t2"}
    assert isinstance(tables["main.x.t1"], TableMeta)


@pytest.mark.asyncio
async def test_fetch_missing_table_raises_with_table_name() -> None:
    """Case 2."""
    catalog = _FakeCatalog(tables={}, missing={"main.x.missing"})
    fetcher = MetadataFetcher(catalog, metadata_providers=[])
    cfg = _room_config(tables=["main.x.missing"])
    with pytest.raises(TableNotFoundError, match="main.x.missing"):
        await fetcher.fetch(cfg)


@pytest.mark.asyncio
async def test_fetch_uc_then_yaml_overrides_description_and_accumulates_synonyms() -> None:
    """Case 3."""
    catalog = _FakeCatalog(tables={"main.x.t": []})
    uc = _MetadataDouble(
        "uc", {"main.x.t": {"description": "from UC", "synonyms": ["a", "b"]}}
    )
    yaml = _MetadataDouble(
        "yaml", {"main.x.t": {"description": "from YAML", "synonyms": ["b", "c"]}}
    )
    fetcher = MetadataFetcher(catalog, metadata_providers=[uc, yaml])
    cfg = _room_config(tables=["main.x.t"])
    tables = await fetcher.fetch(cfg)
    tm = tables["main.x.t"]
    # Last writer wins for scalar.
    assert tm.description == "from YAML"
    # Synonyms accumulate (deduped).
    assert tm.synonyms == ["a", "b", "c"]
    # Conflict recorded.
    assert any(c.field == "description" for c in tm.conflicts)
    # Both provider names in sources.
    assert "uc" in tm.metadata_sources
    assert "yaml" in tm.metadata_sources


@pytest.mark.asyncio
async def test_fetch_always_applies_room_config_metadata_last() -> None:
    """Case 4."""
    catalog = _FakeCatalog(tables={"main.x.t": [("id", "BIGINT")]})
    uc = _MetadataDouble("uc", {"main.x.t": {"description": "UC desc"}})
    cfg = _room_config(
        tables=["main.x.t"],
        column_overrides=[
            ColumnOverride(table="main.x.t", column="id", description="ROOM desc")
        ],
    )
    fetcher = MetadataFetcher(catalog, metadata_providers=[uc])
    tables = await fetcher.fetch(cfg)
    col = next(c for c in tables["main.x.t"].columns if c.name == "id")
    # RoomConfigMetadataProvider ran last and applied the override.
    assert col.description == "ROOM desc"
    assert col.metadata_source == "room_config"
    assert "room_config" in tables["main.x.t"].metadata_sources


@pytest.mark.asyncio
async def test_fetch_twice_in_same_request_caches_catalog_calls() -> None:
    """Case 5."""
    catalog = _FakeCatalog(tables={"main.x.t": [("id", "BIGINT")]})
    fetcher = MetadataFetcher(catalog, metadata_providers=[])
    cfg = _room_config(tables=["main.x.t"])
    await fetcher.fetch(cfg)
    await fetcher.fetch(cfg)
    # Only one call to the catalog for the single table.
    assert catalog.get_calls == ["main.x.t"]


@pytest.mark.asyncio
async def test_fetch_with_empty_stack_returns_physical_schema_only() -> None:
    """Case 6."""
    catalog = _FakeCatalog(tables={"main.x.t": [("id", "BIGINT")]})
    fetcher = MetadataFetcher(catalog, metadata_providers=[])
    cfg = _room_config(tables=["main.x.t"])
    tables = await fetcher.fetch(cfg)
    tm = tables["main.x.t"]
    assert tm.description == ""
    assert tm.synonyms == []
    assert tm.grain == ""
    assert tm.columns[0].description == ""


@pytest.mark.asyncio
async def test_fetch_runs_providers_in_declared_order() -> None:
    """Sanity: stack order matches the configured list."""
    catalog = _FakeCatalog(tables={"main.x.t": []})
    a = _MetadataDouble("a", {})
    b = _MetadataDouble("b", {})
    c = _MetadataDouble("c", {})
    shared: list[str] = []
    a.order_marker = b.order_marker = c.order_marker = shared
    fetcher = MetadataFetcher(catalog, metadata_providers=[a, b, c])
    await fetcher.fetch(_room_config(tables=["main.x.t"]))
    assert shared == ["a", "b", "c"]


# ═══════════════════════════════════════════════════════════════════════════
# Schema-level metadata via MetadataFetcher.fetch_schemas / fetch_all
# ═══════════════════════════════════════════════════════════════════════════


class _SchemaMetadataDouble(MetadataProvider):
    """Applies pre-configured schema-level data via enrich_schemas."""

    def __init__(self, name: str, schema_data: dict[str, dict]) -> None:
        self._name = name
        self._schema_data = schema_data

    @property
    def name(self) -> str:
        return self._name

    async def enrich(self, tables, room_config) -> None:
        pass  # table layer untouched

    async def enrich_schemas(self, schemas, room_config) -> None:
        for full_name, entry in self._schema_data.items():
            s = schemas.get(full_name)
            if s is None:
                continue
            if entry.get("description"):
                s.description = entry["description"]
            if entry.get("domain"):
                s.domain = entry["domain"]
            if entry.get("notes"):
                s.notes = entry["notes"]
            if self._name not in s.metadata_sources:
                s.metadata_sources.append(self._name)


@pytest.mark.asyncio
async def test_fetch_schemas_extracts_unique_catalog_schema_prefixes() -> None:
    catalog = _FakeCatalog(
        tables={
            "main.public.users": [],
            "main.public.orders": [],
            "main.private.audit": [],
            "warehouse.metrics.events": [],
        }
    )
    fetcher = MetadataFetcher(catalog, metadata_providers=[])
    cfg = _room_config(
        tables=[
            "main.public.users",
            "main.public.orders",
            "main.private.audit",
            "warehouse.metrics.events",
        ]
    )
    schemas = await fetcher.fetch_schemas(cfg)
    # Three distinct catalog.schema prefixes — public appears twice in tables
    # but only once in the schemas dict.
    assert set(schemas) == {"main.public", "main.private", "warehouse.metrics"}


@pytest.mark.asyncio
async def test_fetch_schemas_applies_provider_stack_in_order() -> None:
    catalog = _FakeCatalog(tables={"main.public.t": []})
    p1 = _SchemaMetadataDouble("uc", {"main.public": {"description": "from uc"}})
    p2 = _SchemaMetadataDouble("yaml", {"main.public": {"description": "from yaml"}})
    fetcher = MetadataFetcher(catalog, metadata_providers=[p1, p2])
    cfg = _room_config(tables=["main.public.t"])
    schemas = await fetcher.fetch_schemas(cfg)
    s = schemas["main.public"]
    # Last writer wins.
    assert s.description == "from yaml"
    # Both providers tagged in order.
    assert s.metadata_sources == ["uc", "yaml"]


@pytest.mark.asyncio
async def test_fetch_all_returns_both_tables_and_schemas_consistently() -> None:
    catalog = _FakeCatalog(tables={"main.public.t": [("id", "BIGINT")]})
    p = _SchemaMetadataDouble(
        "dom", {"main.public": {"description": "main.public schema"}}
    )
    fetcher = MetadataFetcher(catalog, metadata_providers=[p])
    cfg = _room_config(tables=["main.public.t"])
    tables, schemas = await fetcher.fetch_all(cfg)
    assert set(tables) == {"main.public.t"}
    assert set(schemas) == {"main.public"}
    assert schemas["main.public"].description == "main.public schema"


# ═══════════════════════════════════════════════════════════════════════════
# format_schema_context — agents/base.py helper for SQLAgent prompt
# ═══════════════════════════════════════════════════════════════════════════


def test_format_schema_context_returns_none_when_empty() -> None:
    from tiri.engine.agents.base import format_schema_context
    assert format_schema_context({}) == "(none)"


def test_format_schema_context_returns_none_when_only_full_names_no_data() -> None:
    """SchemaMeta entries with no descriptive fields shouldn't print anything."""
    from tiri.engine.agents.base import format_schema_context
    from tiri.data_models import SchemaMeta
    schemas = {"main.public": SchemaMeta(full_name="main.public")}
    assert format_schema_context(schemas) == "(none)"


def test_format_schema_context_renders_description_and_tags() -> None:
    from tiri.engine.agents.base import format_schema_context
    from tiri.data_models import SchemaMeta
    schemas = {
        "main.public": SchemaMeta(
            full_name="main.public",
            description="public data",
            domain="sales",
            freshness="daily",
            owner="team-x",
            notes="dates always non-null",
        )
    }
    out = format_schema_context(schemas)
    assert "main.public" in out
    assert "sales" in out
    assert "daily" in out
    assert "owner: team-x" in out
    assert "public data" in out
    assert "Notes: dates always non-null" in out


# ═══════════════════════════════════════════════════════════════════════════
# ExampleIndexer — cases 7-9
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_indexer_index_then_retrieve_returns_most_similar_in_top_result() -> None:
    """Case 7."""
    llm = _FakeLLM()
    vector = _FakeVector()
    indexer = ExampleIndexer(llm, vector)
    cfg = _room_config(
        examples=[
            ExampleSQL(question="What is total revenue?", sql="SELECT 1", id="A"),
            ExampleSQL(question="What is the count?", sql="SELECT 2", id="B"),
        ]
    )
    await indexer.index(cfg)
    # Retrieve with the same question text — embed assigns index-based
    # vectors so "What is total revenue?" indexed first (i=0) wins against
    # the question vector also at index 0.
    matches = await indexer.retrieve("What is total revenue?", room_id="r1", top_k=2)
    assert matches[0].id == "A"


@pytest.mark.asyncio
async def test_indexer_removing_an_example_deletes_from_vector_store() -> None:
    """Case 8."""
    llm = _FakeLLM()
    vector = _FakeVector()
    indexer = ExampleIndexer(llm, vector)

    initial = _room_config(
        examples=[
            ExampleSQL(question="Q1", sql="S1", id="A"),
            ExampleSQL(question="Q2", sql="S2", id="B"),
        ]
    )
    await indexer.index(initial)
    assert set(await vector.list_ids({"room_id": "r1"})) == {"A", "B"}

    after_removal = _room_config(
        examples=[ExampleSQL(question="Q1", sql="S1", id="A")]
    )
    await indexer.index(after_removal)
    assert set(await vector.list_ids({"room_id": "r1"})) == {"A"}


@pytest.mark.asyncio
async def test_indexer_retrieve_with_room_id_does_not_return_other_rooms() -> None:
    """Case 9."""
    llm = _FakeLLM()
    vector = _FakeVector()
    indexer = ExampleIndexer(llm, vector)

    await indexer.index(
        _room_config(
            room_id="room_x",
            examples=[ExampleSQL(question="Q", sql="S", id="x1")],
        )
    )
    await indexer.index(
        _room_config(
            room_id="room_y",
            examples=[ExampleSQL(question="Q", sql="S", id="y1")],
        )
    )

    matches = await indexer.retrieve("Q", room_id="room_x", top_k=10)
    assert {m.id for m in matches} == {"x1"}


@pytest.mark.asyncio
async def test_indexer_index_with_empty_examples_is_noop() -> None:
    llm = _FakeLLM()
    vector = _FakeVector()
    indexer = ExampleIndexer(llm, vector)
    await indexer.index(_room_config(examples=[]))
    assert llm.embed_calls == []  # no embed call when there's nothing to embed
    assert await vector.list_ids({"room_id": "r1"}) == []


# ═══════════════════════════════════════════════════════════════════════════
# ContextBuilder — cases 10-12
# ═══════════════════════════════════════════════════════════════════════════


def _builder(catalog=None, providers=None, llm=None, vector=None) -> tuple[ContextBuilder, _FakeLLM, _FakeVector]:
    catalog = catalog or _FakeCatalog(tables={"main.x.t": [("id", "BIGINT")]})
    providers = providers if providers is not None else []
    llm = llm or _FakeLLM()
    vector = vector or _FakeVector()
    # No query provider needed for these tests — pass None-equivalent stub.

    class _NopQuery(QueryProvider):
        async def execute(self, sql, limit=10_000, user_token=None):
            raise AssertionError("query.execute should not be called from ContextBuilder")

        async def validate(self, sql, user_token=None):
            return (True, None)

    return ContextBuilder(
        catalog=catalog,
        metadata_providers=providers,
        query=_NopQuery(),
        llm=llm,
        vector=vector,
    ), llm, vector


@pytest.mark.asyncio
async def test_build_makes_exactly_one_embed_call() -> None:
    """Case 10."""
    builder, llm, _vec = _builder()
    cfg = _room_config(tables=["main.x.t"])
    await builder.build("hello?", cfg, history=[])
    assert len(llm.embed_calls) == 1
    # The single embed call carried just the question.
    assert llm.embed_calls[0] == ["hello?"]


@pytest.mark.asyncio
async def test_build_trims_history_to_last_window() -> None:
    """Case 11."""
    builder, _llm, _vec = _builder()
    cfg = _room_config(tables=["main.x.t"])
    history = [
        ConversationTurn(
            room_id="r1",
            conversation_id="c",
            turn_id=f"t{i}",
            question=f"q{i}",
            sql="SELECT 1",
        )
        for i in range(20)
    ]
    ctx = await builder.build("now?", cfg, history=history, history_window=10)
    assert len(ctx.conversation_history) == 10
    # Last ten preserved (t10..t19).
    assert [t.turn_id for t in ctx.conversation_history] == [
        f"t{i}" for i in range(10, 20)
    ]


@pytest.mark.asyncio
async def test_build_never_calls_llm_complete_or_stream() -> None:
    """Case 12."""
    builder, llm, _vec = _builder()
    cfg = _room_config(tables=["main.x.t"])
    await builder.build("hello?", cfg, history=[])
    assert llm.complete_calls == 0
    assert llm.stream_calls == 0


@pytest.mark.asyncio
async def test_build_assembles_all_context_fields_from_config() -> None:
    """ContextPackage should expose snippets, joins, metrics, instruction from config."""
    builder, _llm, _vec = _builder()
    snippet = SqlSnippet(display_name="x", sql="x=1", kind="filter")
    join = JoinSpec(
        left_table="main.x.t",
        left_alias="t",
        right_table="main.x.u",
        right_alias="u",
        join_on="t.id = u.id",
        relationship_type="MANY_TO_ONE",
    )
    metric = Metric(
        name="revenue",
        display_name="Net Revenue",
        sql="SUM(x)",
        grain="row",
    )
    cfg = _room_config(
        tables=["main.x.t"],
        sql_filters=[snippet],
        joins=[join],
        metrics=[metric],
        text_instruction="follow vocab rules",
    )
    ctx = await builder.build("q", cfg, history=[])
    assert ctx.room_id == "r1"
    assert ctx.sql_snippets == [snippet]
    assert ctx.joins == [join]
    assert ctx.metrics == [metric]
    assert ctx.text_instruction == "follow vocab rules"


# ═══════════════════════════════════════════════════════════════════════════
# RoomConfigMetadataProvider — direct tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_room_config_provider_applies_column_override_description() -> None:
    table = TableMeta(
        full_name="main.x.t",
        columns=[ColumnMeta(name="status", data_type="STRING")],
    )
    tables = {"main.x.t": table}
    cfg = _room_config(
        tables=["main.x.t"],
        column_overrides=[
            ColumnOverride(
                table="main.x.t",
                column="status",
                description="active/inactive flag",
                synonyms=["state"],
            )
        ],
    )
    provider = RoomConfigMetadataProvider()
    await provider.enrich(tables, cfg)
    col = tables["main.x.t"].columns[0]
    assert col.description == "active/inactive flag"
    assert "state" in col.synonyms
    assert col.metadata_source == "room_config"


@pytest.mark.asyncio
async def test_room_config_provider_records_conflict_on_override() -> None:
    table = TableMeta(
        full_name="main.x.t",
        columns=[
            ColumnMeta(name="status", data_type="STRING", description="existing")
        ],
    )
    tables = {"main.x.t": table}
    cfg = _room_config(
        tables=["main.x.t"],
        column_overrides=[
            ColumnOverride(
                table="main.x.t", column="status", description="overridden"
            )
        ],
    )
    await RoomConfigMetadataProvider().enrich(tables, cfg)
    assert tables["main.x.t"].columns[0].description == "overridden"
    assert any(c.field == "description" for c in tables["main.x.t"].conflicts)


@pytest.mark.asyncio
async def test_room_config_provider_skips_missing_table_or_column_silently() -> None:
    table = TableMeta(
        full_name="main.x.t",
        columns=[ColumnMeta(name="id", data_type="BIGINT")],
    )
    tables = {"main.x.t": table}
    cfg = _room_config(
        tables=["main.x.t"],
        column_overrides=[
            ColumnOverride(table="main.x.ghost", column="x", description="A"),
            ColumnOverride(table="main.x.t", column="ghost", description="B"),
        ],
    )
    await RoomConfigMetadataProvider().enrich(tables, cfg)
    # No mutation — the table that exists wasn't touched.
    assert tables["main.x.t"].columns[0].description == ""
