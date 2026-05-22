"""MetadataFetcher — runs the metadata provider stack.

For each table in a RoomConfig:
  1. Call CatalogProvider.get_table_meta to populate physical schema.
  2. Apply each MetadataProvider in stack order (mutates in place).
  3. Always apply RoomConfigMetadataProvider last.

Caches the resolved tables dict per request so a second fetch() in the same
request makes zero additional CatalogProvider calls.
"""

from __future__ import annotations

from tiri.data_models import RoomConfig, TableMeta
from tiri.knowledge.room_config_metadata import RoomConfigMetadataProvider
from tiri.providers.base import (
    CatalogProvider,
    MetadataProvider,
    QueryProvider,
    TableNotFoundError,
)


class MetadataFetcher:
    def __init__(
        self,
        catalog: CatalogProvider,
        metadata_providers: list[MetadataProvider],
    ) -> None:
        self._catalog = catalog
        # Defensive copy — caller's list is not retained.
        self._metadata_providers = list(metadata_providers)
        self._room_config_provider = RoomConfigMetadataProvider()
        # Per-request cache. Reset by calling fetch() with a fresh instance.
        # The container builds a new MetadataFetcher each request.
        self._cache: dict[str, TableMeta] = {}
        self._cache_room_id: str | None = None

    async def fetch(
        self,
        config: RoomConfig,
        query: QueryProvider | None = None,  # noqa: ARG002
        tables_override: list[str] | None = None,
    ) -> dict[str, TableMeta]:
        """Return fully-resolved TableMeta for the requested tables.

        `tables_override` (EXT-2) — when supplied, fetch metadata for these
        FQNs instead of `config.tables`. ContextBuilder uses this after
        TableSelector expands wildcards and picks the top-k tables for the
        question. The room config's metadata-stack and overrides still apply.

        `query` is accepted for forward compatibility — providers that need
        it (e.g. UCAnnotationsMetadataProvider for sample values) receive it
        via their constructor in the current design. Kept on this signature
        because docs/knowledge_store.md documents it.
        """
        target_tables = (
            list(tables_override) if tables_override is not None else list(config.tables)
        )

        if (
            self._cache_room_id == config.room_id
            and set(self._cache) == set(target_tables)
        ):
            return self._cache

        # 1. Physical schema for each table.
        tables: dict[str, TableMeta] = {}
        for full_name in target_tables:
            try:
                tables[full_name] = await self._catalog.get_table_meta(
                    full_name
                )
            except TableNotFoundError:
                # Re-raise with the full_name in the message to satisfy
                # test case 2 (missing table named in the error).
                raise TableNotFoundError(
                    f"Table not found: {full_name}"
                )

        # 2. Apply each metadata provider in declared order.
        for provider in self._metadata_providers:
            await provider.enrich(tables, config)

        # 3. RoomConfigMetadataProvider is always last.
        await self._room_config_provider.enrich(tables, config)

        self._cache = tables
        self._cache_room_id = config.room_id
        return tables
