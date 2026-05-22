"""ContextBuilder — assembles a ContextPackage per request.

Orchestrates MetadataFetcher and ExampleIndexer. Makes one `llm.embed()`
call (inside ExampleIndexer.retrieve) and zero `llm.complete()` calls —
this layer is data assembly only.
"""

from __future__ import annotations

from tiri.data_models import (
    ContextPackage,
    ConversationTurn,
    RoomConfig,
)
from tiri.knowledge.example_indexer import ExampleIndexer
from tiri.knowledge.metadata_fetcher import MetadataFetcher
from tiri.knowledge.table_selector import (
    TableSelector,
    has_wildcard,
    selection_method,
)
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    MetadataProvider,
    QueryProvider,
    VectorProvider,
)


class ContextBuilder:
    def __init__(
        self,
        catalog: CatalogProvider,
        metadata_providers: list[MetadataProvider],
        query: QueryProvider,
        llm: LLMProvider,
        vector: VectorProvider,
    ) -> None:
        self._catalog = catalog
        self._metadata_providers = list(metadata_providers)
        self._query = query
        self._llm = llm
        self._vector = vector

    async def build(
        self,
        question: str,
        config: RoomConfig,
        history: list[ConversationTurn],
        history_window: int = 10,
    ) -> ContextPackage:
        # 0. EXT-2: if RoomConfig.tables contains wildcards, expand and rank
        #    them now so MetadataFetcher fetches metadata for only the
        #    selected subset. With explicit FQNs this is a no-op.
        selected_tables: list[str] | None
        if has_wildcard(config.tables):
            selector = TableSelector(
                catalog=self._catalog,
                vector=self._vector,
                llm=self._llm,
            )
            selected_tables = await selector.select(
                question=question,
                room_config=config,
                max_tables=config.max_tables_per_query,
            )
        else:
            selected_tables = None  # MetadataFetcher uses config.tables

        # 1. Physical + semantic metadata for the selected tables, plus
        #    schema-level metadata for every `catalog.schema` referenced.
        fetcher = MetadataFetcher(self._catalog, self._metadata_providers)
        table_schemas, schema_meta = await fetcher.fetch_all(
            config, query=self._query, tables_override=selected_tables
        )

        # 2. Top-k similar examples (the only LLM call in this layer).
        indexer = ExampleIndexer(self._llm, self._vector)
        retrieved = await indexer.retrieve(
            question=question, room_id=config.room_id, top_k=5
        )

        # 3. Snippets come from the three snippet lists on the config.
        sql_snippets = (
            list(config.sql_filters)
            + list(config.sql_expressions)
            + list(config.sql_measures)
        )

        # 4. Last N turns of history.
        trimmed_history = (
            list(history[-history_window:])
            if history_window > 0
            else []
        )

        return ContextPackage(
            room_id=config.room_id,
            table_schemas=table_schemas,
            joins=list(config.joins),
            sql_snippets=sql_snippets,
            metrics=list(config.metrics),
            text_instruction=config.text_instruction,
            default_filters=list(config.default_filters),
            retrieved_examples=retrieved,
            conversation_history=trimmed_history,
            table_selection_method=selection_method(config.tables),
            domain_knowledge=list(config.domain_knowledge),
            schema_meta=schema_meta,
        )
