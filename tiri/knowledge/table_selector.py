"""TableSelector — EXT-2 dynamic table selection.

Resolves wildcard patterns in `RoomConfig.tables` into a concrete table list,
then narrows that list to the top-k most semantically similar to the user's
question. Join-spec tables are always included regardless of similarity.

Wildcard syntax:
- `catalog.schema.*`   → every table in that schema
- `catalog.*.*`        → every table in every schema of that catalog

Pure FQN strings (no `*`) pass through unchanged. Mixed entries are handled
per-entry: explicit FQNs are kept as-is, wildcards are expanded.

Performance: one `llm.embed()` call covers the question + all candidate
table names. Similarity is computed in Python (cosine). For 200 tables this
completes well inside the 2-second budget called for in EXT-2 test case 5.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable

from tiri.data_models import RoomConfig
from tiri.providers.base import (
    CatalogProvider,
    CatalogProviderError,
    LLMProvider,
    VectorProvider,
)


_log = logging.getLogger("tiri.knowledge.table_selector")


def has_wildcard(tables: Iterable[str]) -> bool:
    """True if any entry contains a `*`."""
    return any("*" in t for t in tables)


def selection_method(tables: Iterable[str]) -> str:
    """Classify the room's table list into `configured` / `dynamic_search` / `hybrid`."""
    entries = list(tables)
    if not entries:
        return "configured"
    wildcard_count = sum(1 for t in entries if "*" in t)
    if wildcard_count == 0:
        return "configured"
    if wildcard_count == len(entries):
        return "dynamic_search"
    return "hybrid"


class TableSelector:
    """Expand wildcards, rank by semantic similarity, always include joins."""

    def __init__(
        self,
        catalog: CatalogProvider,
        vector: VectorProvider,
        llm: LLMProvider,
    ) -> None:
        self._catalog = catalog
        # Pre-indexing path for catalogs > ~1000 tables. Live embedding works
        # well to ~500 tables; beyond that, pre-index table descriptions in
        # the vector store at room-create time and query here instead of
        # batch-embedding every candidate per request.
        self._vector = vector
        self._llm = llm

    async def select(
        self,
        question: str,
        room_config: RoomConfig,
        max_tables: int | None = None,
    ) -> list[str]:
        """Return the FQNs to load into the ContextPackage for this question.

        `max_tables` defaults to `room_config.max_tables_per_query` so the
        cap declared on the room flows through automatically. Pass an
        explicit value to override.
        """
        if max_tables is None:
            max_tables = room_config.max_tables_per_query
        candidates = await self._expand_wildcards(room_config.tables)
        if not candidates:
            return []

        join_tables = _join_tables(room_config)

        # If no wildcards were present, the candidates ARE the configured FQNs.
        # No similarity ranking — just return them in declared order.
        if not has_wildcard(room_config.tables):
            return list(room_config.tables)

        # Rank candidates by similarity to the question.
        ranked = await self._rank_by_similarity(question, candidates)

        # Take top-k, then append any join-graph members not already in the set.
        # This guarantees test case 4: a join-required table that didn't score
        # in the top-k is still included.
        selected: list[str] = []
        seen: set[str] = set()
        for name in ranked[:max_tables]:
            if name not in seen:
                selected.append(name)
                seen.add(name)
        for name in join_tables:
            if name in candidates and name not in seen:
                selected.append(name)
                seen.add(name)
        return selected

    async def _expand_wildcards(self, patterns: list[str]) -> list[str]:
        out: set[str] = set()
        for pat in patterns:
            if "*" not in pat:
                out.add(pat)
                continue
            parts = pat.split(".")
            if len(parts) != 3:
                _log.warning(
                    "Skipping unsupported wildcard pattern %r — expected 3-part FQN",
                    pat,
                )
                continue
            catalog, schema, table = parts
            try:
                if schema == "*" and table == "*":
                    schemas = await self._catalog.list_schemas(catalog)
                    for s in schemas:
                        tables = await self._catalog.list_tables(catalog, s)
                        out.update(tables)
                elif table == "*" and schema != "*":
                    tables = await self._catalog.list_tables(catalog, schema)
                    out.update(tables)
                else:
                    # `*.schema.table` or other shapes — not supported yet.
                    # TODO: extend here when customers need `a.*.c` or other
                    # mixed patterns — requires catalog.list_schemas + filtered
                    # list_tables, then filter by table-name pattern.
                    _log.warning(
                        "Skipping pattern %r — only `catalog.schema.*` and "
                        "`catalog.*.*` are supported",
                        pat,
                    )
            except CatalogProviderError as e:
                _log.warning(
                    "Wildcard expansion failed for %r: %s — skipping", pat, e
                )
        return sorted(out)

    async def _rank_by_similarity(
        self, question: str, candidates: list[str]
    ) -> list[str]:
        embeddings = await self._llm.embed([question] + candidates)
        if not embeddings:
            return list(candidates)
        question_vec = embeddings[0]
        table_vecs = embeddings[1:]
        scored: list[tuple[float, str]] = []
        for name, vec in zip(candidates, table_vecs):
            scored.append((_cosine(question_vec, vec), name))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [name for _score, name in scored]


def _join_tables(room_config: RoomConfig) -> list[str]:
    seen: list[str] = []
    for join in room_config.joins:
        for name in (join.left_table, join.right_table):
            if name and name not in seen:
                seen.append(name)
    return seen


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 for zero vectors (avoids div-by-zero)."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
