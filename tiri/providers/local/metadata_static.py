"""StaticMetadataProvider — in-memory dict source for tests and local dev."""

from __future__ import annotations

from tiri.data_models import (
    ColumnMeta,
    MetadataConflict,
    RoomConfig,
    SchemaMeta,
    TableMeta,
)
from tiri.providers.base import MetadataProvider


_SCALAR_SCHEMA_FIELDS = (
    "description",
    "domain",
    "freshness",
    "owner",
    "notes",
)
_LIST_SCHEMA_FIELDS = ("synonyms",)

_SCALAR_TABLE_FIELDS = (
    "description",
    "grain",
    "domain",
    "freshness",
    "default_date_column",
    "default_filter",
)
_LIST_TABLE_FIELDS = ("synonyms", "recommended_joins")

_SCALAR_COLUMN_FIELDS = (
    "description",
    "value_description",
    "semantic_type",
    "currency_code",
    "date_format",
    "foreign_key_table",
    "foreign_key_column",
)
_LIST_COLUMN_FIELDS = ("synonyms", "sample_values")
_BOOL_COLUMN_FIELDS = (
    "is_primary_key",
    "is_foreign_key",
    "is_high_cardinality",
    "exclude_from_select_star",
)


class StaticMetadataProvider(MetadataProvider):
    """Applies metadata from a nested Python dict.

    Data shape (per table):
      {
        "description": "...", "grain": "...", "synonyms": [...],
        "columns": {
          "col_name": {"description": "...", "synonyms": [...], ...}
        }
      }
    """

    def __init__(
        self,
        name: str,
        data: dict,
        schemas: dict | None = None,
    ) -> None:
        self._name = name
        self._data: dict[str, dict] = dict(data or {})
        self._schemas: dict[str, dict] = dict(schemas or {})

    @property
    def name(self) -> str:
        return self._name

    async def enrich(
        self,
        tables: dict[str, TableMeta],
        room_config: RoomConfig,
    ) -> None:
        for full_name, table_meta in tables.items():
            entry = self._data.get(full_name)
            if entry is None:
                continue  # silently skip — that's the contract
            _apply_table_entry(table_meta, entry, source=self._name)
            if self._name not in table_meta.metadata_sources:
                table_meta.metadata_sources.append(self._name)

    async def enrich_schemas(
        self,
        schemas: dict[str, SchemaMeta],
        room_config: RoomConfig,
    ) -> None:
        for full_name, schema_meta in schemas.items():
            entry = self._schemas.get(full_name)
            if entry is None:
                continue  # silently skip — same contract as enrich()
            _apply_schema_entry(schema_meta, entry, source=self._name)
            if self._name not in schema_meta.metadata_sources:
                schema_meta.metadata_sources.append(self._name)


def _apply_schema_entry(
    schema_meta: SchemaMeta, entry: dict, *, source: str
) -> None:
    for field_name in _SCALAR_SCHEMA_FIELDS:
        new = entry.get(field_name)
        if new is None or new == "":
            continue
        # Schemas don't currently carry a conflicts list (kept simple
        # since multi-source schema overrides are rare); last writer
        # wins, prior value is silently replaced. If a use case for
        # schema conflict tracking emerges, add a conflicts field on
        # SchemaMeta and mirror the table pattern here.
        setattr(schema_meta, field_name, new)

    for field_name in _LIST_SCHEMA_FIELDS:
        new = entry.get(field_name)
        if not new:
            continue
        existing = getattr(schema_meta, field_name)
        for item in new:
            if item not in existing:
                existing.append(item)


def _apply_table_entry(
    table_meta: TableMeta, entry: dict, *, source: str
) -> None:
    for field_name in _SCALAR_TABLE_FIELDS:
        new = entry.get(field_name)
        if new is None or new == "":
            continue
        old = getattr(table_meta, field_name)
        if old and old != new:
            table_meta.conflicts.append(
                MetadataConflict(
                    table=table_meta.full_name,
                    column=None,
                    field=field_name,
                    values={"existing": old, source: new},
                    resolved_to=source,
                )
            )
        setattr(table_meta, field_name, new)

    for field_name in _LIST_TABLE_FIELDS:
        new = entry.get(field_name)
        if not new:
            continue
        existing = getattr(table_meta, field_name)
        for item in new:
            if item not in existing:
                existing.append(item)

    columns_section = entry.get("columns") or {}
    col_by_name = {c.name: c for c in table_meta.columns}
    for col_name, col_entry in columns_section.items():
        col = col_by_name.get(col_name)
        if col is None:
            continue  # no physical column of that name — ignore
        _apply_column_entry(col, col_entry, table_meta=table_meta, source=source)


def _apply_column_entry(
    col: ColumnMeta,
    col_entry: dict,
    *,
    table_meta: TableMeta,
    source: str,
) -> None:
    for field_name in _SCALAR_COLUMN_FIELDS:
        new = col_entry.get(field_name)
        if new is None or new == "":
            continue
        old = getattr(col, field_name)
        if old and old != new:
            table_meta.conflicts.append(
                MetadataConflict(
                    table=table_meta.full_name,
                    column=col.name,
                    field=field_name,
                    values={"existing": old, source: new},
                    resolved_to=source,
                )
            )
        setattr(col, field_name, new)

    for field_name in _LIST_COLUMN_FIELDS:
        new = col_entry.get(field_name)
        if not new:
            continue
        existing = getattr(col, field_name)
        for item in new:
            if item not in existing:
                existing.append(item)

    for field_name in _BOOL_COLUMN_FIELDS:
        if field_name in col_entry:
            setattr(col, field_name, bool(col_entry[field_name]))

    col.metadata_source = source
