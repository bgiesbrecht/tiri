"""Tests for tiri.knowledge.table_selector — EXT-2 dynamic table selection.

Covers all 5 cases from docs/extensions.md EXT-2 plus the
selection_method() classifier.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from tiri.data_models import (
    JoinSpec,
    LLMResponse,
    RoomConfig,
    TableMeta,
    VectorMatch,
)
from tiri.knowledge.table_selector import (
    TableSelector,
    has_wildcard,
    selection_method,
)
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    VectorProvider,
)


# ── Test doubles ────────────────────────────────────────────────────────────


class _Catalog(CatalogProvider):
    def __init__(
        self,
        schemas: dict[str, list[str]] | None = None,
        tables: dict[tuple[str, str], list[str]] | None = None,
    ) -> None:
        self._schemas = schemas or {}
        self._tables = tables or {}

    async def get_table_meta(self, full_name):
        return TableMeta(full_name=full_name)

    async def list_tables(self, catalog, schema):
        return list(self._tables.get((catalog, schema), []))

    async def list_schemas(self, catalog):
        return list(self._schemas.get(catalog, []))

    async def search_tables(self, query, limit=10):
        return []


class _DeterministicLLM(LLMProvider):
    """Embeds each text as a 1-D vector keyed by `scores[text]`.

    Cosine similarity of two 1-D vectors with the same sign is 1.0, so by
    giving the question and the desired top-ranked tables positive scores
    and others zero/negative, we control ranking deterministically.
    """

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    async def complete(self, messages, temperature=0.0, max_tokens=2048, task="sql", model=None):
        return LLMResponse(content="", usage={}, raw=None)

    async def stream(self, messages, temperature=0.0, task="sql", model=None) -> AsyncIterator[str]:
        yield ""

    async def embed(self, texts):
        # 1-D vector. Cosine similarity reduces to sign(a)*sign(b), which
        # we want to dominate ordering — so use the actual magnitude.
        # We instead emit 2-D vectors `[score, 1.0]` so cosine isn't
        # purely sign-based and ordering tracks magnitude.
        out = []
        for t in texts:
            s = self._scores.get(t, 0.0)
            out.append([s, 1.0])
        return out


class _NoopVector(VectorProvider):
    async def upsert(self, id, vector, payload):
        return None

    async def query(self, vector, top_k=5, filter=None):
        return []

    async def delete(self, id):
        return None

    async def list_ids(self, filter=None):
        return []


def _room(tables: list[str], joins: list[JoinSpec] | None = None, max_tables_per_query: int = 10) -> RoomConfig:
    return RoomConfig(
        room_id="r1",
        title="r",
        tables=tables,
        warehouse_id="wh",
        joins=joins or [],
        max_tables_per_query=max_tables_per_query,
    )


# ── selection_method classifier ─────────────────────────────────────────────


def test_selection_method_all_explicit_returns_configured() -> None:
    assert selection_method(["a.b.c", "a.b.d"]) == "configured"


def test_selection_method_all_wildcards_returns_dynamic_search() -> None:
    assert selection_method(["a.b.*", "a.*.*"]) == "dynamic_search"


def test_selection_method_mixed_returns_hybrid() -> None:
    assert selection_method(["a.b.c", "a.b.*"]) == "hybrid"


def test_has_wildcard_detects_any_star() -> None:
    assert has_wildcard(["a.b.*"]) is True
    assert has_wildcard(["a.b.c"]) is False
    assert has_wildcard(["a.b.c", "a.b.*"]) is True


# ═══════════════════════════════════════════════════════════════════════════
# Doc test cases — EXT-2 cases 1–5
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_explicit_table_list_uses_configured_no_dynamic_selection() -> None:
    """Case 1."""
    selector = TableSelector(
        catalog=_Catalog(),
        vector=_NoopVector(),
        llm=_DeterministicLLM(scores={}),  # no embeds expected
    )
    cfg = _room(tables=["tpch.sf1.customer", "tpch.sf1.orders"])
    selected = await selector.select(question="q", room_config=cfg)
    assert selected == ["tpch.sf1.customer", "tpch.sf1.orders"]


@pytest.mark.asyncio
async def test_schema_wildcard_returns_top_k_by_similarity() -> None:
    """Case 2."""
    catalog = _Catalog(
        tables={
            ("tpch", "sf1"): [
                "tpch.sf1.customer",
                "tpch.sf1.orders",
                "tpch.sf1.lineitem",
                "tpch.sf1.partsupp",
                "tpch.sf1.region",
            ]
        }
    )
    # Higher scores → ranked higher; question's score doesn't matter for
    # ordering of the candidates relative to each other.
    llm = _DeterministicLLM(
        scores={
            "Who are top customers?": 1.0,
            "tpch.sf1.customer": 0.9,
            "tpch.sf1.orders": 0.7,
            "tpch.sf1.lineitem": 0.5,
            "tpch.sf1.partsupp": 0.0,
            "tpch.sf1.region": -0.5,
        }
    )
    selector = TableSelector(catalog=catalog, vector=_NoopVector(), llm=llm)
    cfg = _room(tables=["tpch.sf1.*"], max_tables_per_query=3)
    selected = await selector.select(
        question="Who are top customers?", room_config=cfg
    )
    assert len(selected) <= 3
    assert selected[0] == "tpch.sf1.customer"
    assert selected[1] == "tpch.sf1.orders"


@pytest.mark.asyncio
async def test_dynamic_selection_includes_join_spec_tables() -> None:
    """Case 3."""
    catalog = _Catalog(
        tables={
            ("tpch", "sf1"): [
                "tpch.sf1.customer",
                "tpch.sf1.orders",
                "tpch.sf1.lineitem",
                "tpch.sf1.region",
            ]
        }
    )
    # Customers wins by similarity; region is bottom-ranked but appears in a
    # join, so the selector must include it.
    llm = _DeterministicLLM(
        scores={
            "Q": 1.0,
            "tpch.sf1.customer": 0.9,
            "tpch.sf1.orders": 0.8,
            "tpch.sf1.lineitem": 0.7,
            "tpch.sf1.region": -1.0,
        }
    )
    joins = [
        JoinSpec(
            left_table="tpch.sf1.customer",
            left_alias="c",
            right_table="tpch.sf1.region",
            right_alias="r",
            join_on="c.region_id = r.id",
            relationship_type="MANY_TO_ONE",
        )
    ]
    selector = TableSelector(catalog=catalog, vector=_NoopVector(), llm=llm)
    cfg = _room(tables=["tpch.sf1.*"], joins=joins, max_tables_per_query=2)
    selected = await selector.select(question="Q", room_config=cfg)

    assert "tpch.sf1.customer" in selected
    assert "tpch.sf1.region" in selected  # join-required, included


@pytest.mark.asyncio
async def test_join_table_below_top_k_is_still_included() -> None:
    """Case 4 — phrased as the strong form: a join-only table is included
    even when the question's wording wouldn't have ranked it in the top-k."""
    catalog = _Catalog(
        tables={
            ("tpch", "sf1"): [
                "tpch.sf1.customer",
                "tpch.sf1.orders",
                "tpch.sf1.unrelated_one",
                "tpch.sf1.unrelated_two",
                "tpch.sf1.nation",
            ]
        }
    )
    llm = _DeterministicLLM(
        scores={
            "q": 1.0,
            "tpch.sf1.customer": 0.9,
            "tpch.sf1.orders": 0.8,
            "tpch.sf1.unrelated_one": 0.7,
            "tpch.sf1.unrelated_two": 0.6,
            "tpch.sf1.nation": -1.0,  # below all others, would be excluded
        }
    )
    joins = [
        JoinSpec(
            left_table="tpch.sf1.customer",
            left_alias="c",
            right_table="tpch.sf1.nation",
            right_alias="n",
            join_on="c.nation_id = n.id",
            relationship_type="MANY_TO_ONE",
        )
    ]
    selector = TableSelector(catalog=catalog, vector=_NoopVector(), llm=llm)
    # max_tables_per_query=2 → top 2 by sim are customer + orders; nation
    # must still be added because of the join.
    cfg = _room(tables=["tpch.sf1.*"], joins=joins, max_tables_per_query=2)
    selected = await selector.select(question="q", room_config=cfg)
    assert "tpch.sf1.nation" in selected


@pytest.mark.asyncio
async def test_full_catalog_wildcard_with_200_tables_under_two_seconds() -> None:
    """Case 5 — perf budget."""
    # Build a catalog with 10 schemas × 20 tables = 200 tables.
    schemas = {"tpch": [f"sch{i}" for i in range(10)]}
    tables = {
        ("tpch", f"sch{i}"): [f"tpch.sch{i}.t{j}" for j in range(20)]
        for i in range(10)
    }
    catalog = _Catalog(schemas=schemas, tables=tables)
    # All tables score uniformly; the embed call needs to handle 201 vectors.
    scores = {"q": 1.0}
    for i in range(10):
        for j in range(20):
            scores[f"tpch.sch{i}.t{j}"] = float(j)  # within-schema ordering
    llm = _DeterministicLLM(scores=scores)
    selector = TableSelector(catalog=catalog, vector=_NoopVector(), llm=llm)
    cfg = _room(tables=["tpch.*.*"], max_tables_per_query=10)

    start = time.monotonic()
    selected = await selector.select(question="q", room_config=cfg)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"selection took {elapsed:.2f}s (budget 2.0s)"
    assert len(selected) == 10


# ═══════════════════════════════════════════════════════════════════════════
# Wildcard parsing
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_catalog_wildcard_expands_via_list_schemas() -> None:
    catalog = _Catalog(
        schemas={"tpch": ["sf1", "sf10"]},
        tables={
            ("tpch", "sf1"): ["tpch.sf1.a", "tpch.sf1.b"],
            ("tpch", "sf10"): ["tpch.sf10.x"],
        },
    )
    llm = _DeterministicLLM(
        scores={
            "q": 1.0,
            "tpch.sf1.a": 0.9,
            "tpch.sf1.b": 0.8,
            "tpch.sf10.x": 0.7,
        }
    )
    selector = TableSelector(catalog=catalog, vector=_NoopVector(), llm=llm)
    cfg = _room(tables=["tpch.*.*"], max_tables_per_query=10)
    selected = await selector.select(question="q", room_config=cfg)
    assert set(selected) == {"tpch.sf1.a", "tpch.sf1.b", "tpch.sf10.x"}


@pytest.mark.asyncio
async def test_mixed_explicit_and_wildcard_supported() -> None:
    catalog = _Catalog(
        tables={("tpch", "sf1"): ["tpch.sf1.x", "tpch.sf1.y"]}
    )
    llm = _DeterministicLLM(
        scores={
            "q": 1.0,
            "tpch.sf1.x": 0.9,
            "tpch.sf1.y": 0.8,
            "tpch.sf1.explicit_one": 0.7,  # passed through from FQN entry
        }
    )
    selector = TableSelector(catalog=catalog, vector=_NoopVector(), llm=llm)
    cfg = _room(
        tables=["tpch.sf1.explicit_one", "tpch.sf1.*"],
        max_tables_per_query=10,
    )
    selected = await selector.select(question="q", room_config=cfg)
    # All three FQNs are candidates because the explicit one is in the same
    # schema; ranking only matters for top-k caps.
    assert set(selected) == {
        "tpch.sf1.x",
        "tpch.sf1.y",
        "tpch.sf1.explicit_one",
    }


@pytest.mark.asyncio
async def test_unsupported_wildcard_pattern_skipped_with_warning(caplog) -> None:
    """A wildcard whose shape isn't `catalog.schema.*` or `catalog.*.*` is
    skipped with a logged warning rather than crashing the pipeline."""
    catalog = _Catalog()
    llm = _DeterministicLLM(scores={})
    selector = TableSelector(catalog=catalog, vector=_NoopVector(), llm=llm)
    import logging

    with caplog.at_level(logging.WARNING, logger="tiri.knowledge.table_selector"):
        selected = await selector.select(
            question="q",
            # 2-part with wildcard — not a supported shape.
            room_config=_room(tables=["weird.*"]),
        )
    assert selected == []
    assert any("weird.*" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════════
# ContextBuilder integration — confirm table_selection_method propagates
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_context_builder_marks_method_for_explicit_room() -> None:
    """When all tables are explicit FQNs, ContextPackage.table_selection_method
    is 'configured'."""
    from tiri.knowledge.context_builder import ContextBuilder
    from tiri.providers.base import MetadataProvider, QueryProvider

    catalog = _Catalog(tables={})
    llm = _DeterministicLLM(scores={})
    vector = _NoopVector()

    class _NoopQuery(QueryProvider):
        async def execute(self, sql, limit=10_000, user_token=None):
            raise AssertionError
        async def validate(self, sql, user_token=None):
            return (True, None)

    builder = ContextBuilder(
        catalog=catalog,
        metadata_providers=[],
        query=_NoopQuery(),
        llm=llm,
        vector=vector,
    )
    # Catalog can answer get_table_meta even when tables map is empty —
    # our stub returns a default TableMeta for any name.
    cfg = _room(tables=["x.y.z"])
    ctx = await builder.build(question="q", config=cfg, history=[])
    assert ctx.table_selection_method == "configured"


@pytest.mark.asyncio
async def test_context_builder_marks_method_for_wildcard_room() -> None:
    from tiri.knowledge.context_builder import ContextBuilder
    from tiri.providers.base import QueryProvider

    catalog = _Catalog(
        tables={("a", "b"): ["a.b.t1", "a.b.t2"]}
    )
    llm = _DeterministicLLM(scores={"q": 1.0, "a.b.t1": 0.9, "a.b.t2": 0.8})
    vector = _NoopVector()

    class _NoopQuery(QueryProvider):
        async def execute(self, sql, limit=10_000, user_token=None):
            raise AssertionError
        async def validate(self, sql, user_token=None):
            return (True, None)

    builder = ContextBuilder(
        catalog=catalog,
        metadata_providers=[],
        query=_NoopQuery(),
        llm=llm,
        vector=vector,
    )
    cfg = _room(tables=["a.b.*"], max_tables_per_query=5)
    ctx = await builder.build(question="q", config=cfg, history=[])
    assert ctx.table_selection_method == "dynamic_search"
    # MetadataFetcher only fetched the expanded tables.
    assert set(ctx.table_schemas) == {"a.b.t1", "a.b.t2"}
