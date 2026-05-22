"""MetadataFetcher — runs the metadata provider stack.

For each table in a RoomConfig:
  1. Call CatalogProvider.get_table_meta to populate physical schema.
  2. Apply each MetadataProvider in stack order (mutates in place).
  3. Always apply RoomConfigMetadataProvider last.

For each unique `catalog.schema` prefix referenced by the resolved tables,
the same stack runs against schema-level metadata via
`MetadataProvider.enrich_schemas` (default no-op). Schema metadata is
populated lazily — callers can request it via `fetch_schemas()` after
`fetch()`, or call `fetch_all()` to get both in one pass.

Caches the resolved tables and schemas per request so subsequent calls in
the same request make zero additional provider calls.
"""

from __future__ import annotations

from tiri.data_models import RoomConfig, SchemaMeta, TableMeta
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
        # Schema cache — populated by fetch_schemas() (or implicitly via
        # fetch_all()). Empty until either is called.
        self._schema_cache: dict[str, SchemaMeta] = {}
        self._schema_cache_key: tuple[str, frozenset[str]] | None = None

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

    async def fetch_schemas(
        self,
        config: RoomConfig,
        tables_override: list[str] | None = None,
    ) -> dict[str, SchemaMeta]:
        """Return SchemaMeta for every `catalog.schema` referenced by tables.

        Cheap to call after `fetch()` — the schema set is derived from
        the table FQNs (no extra catalog round-trips). For each unique
        prefix, a SchemaMeta is constructed and run through the same
        stack via `MetadataProvider.enrich_schemas` (default no-op).
        Cached per-request like `fetch()`.
        """
        target_tables = (
            list(tables_override) if tables_override is not None else list(config.tables)
        )
        schema_names = _unique_schema_prefixes(target_tables)
        cache_key = (config.room_id, frozenset(schema_names))
        if self._schema_cache_key == cache_key:
            return self._schema_cache

        schemas: dict[str, SchemaMeta] = {
            name: SchemaMeta(full_name=name) for name in schema_names
        }
        for provider in self._metadata_providers:
            await provider.enrich_schemas(schemas, config)

        self._schema_cache = schemas
        self._schema_cache_key = cache_key
        return schemas

    async def fetch_all(
        self,
        config: RoomConfig,
        query: QueryProvider | None = None,
        tables_override: list[str] | None = None,
    ) -> tuple[dict[str, TableMeta], dict[str, SchemaMeta]]:
        """Convenience: returns (tables, schemas) from a single call.

        ContextBuilder uses this so the schema set is derived from the
        same selected-tables list that was used for the table fetch.
        """
        tables = await self.fetch(config, query=query, tables_override=tables_override)
        schemas = await self.fetch_schemas(config, tables_override=tables_override)
        return tables, schemas


def _unique_schema_prefixes(table_fqns: list[str]) -> list[str]:
    """Extract unique `catalog.schema` prefixes from a list of FQNs.

    "samples.tpch.lineitem" → "samples.tpch". Tables without at least
    three dot-separated parts are skipped (likely a wildcard literal
    that EXT-2 didn't expand, or a malformed entry).
    """
    seen: list[str] = []
    for fqn in table_fqns:
        parts = fqn.split(".")
        if len(parts) < 3:
            continue
        prefix = f"{parts[0]}.{parts[1]}"
        if prefix not in seen:
            seen.append(prefix)
    return seen
