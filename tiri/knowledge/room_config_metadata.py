"""RoomConfigMetadataProvider — the always-last entry in the metadata stack.

Applies `RoomConfig.column_overrides`. Room-level overrides win because
this provider runs last — see docs/metadata.md for the merge rules.
"""

from __future__ import annotations

from tiri.data_models import MetadataConflict, RoomConfig, TableMeta
from tiri.providers.base import MetadataProvider


class RoomConfigMetadataProvider(MetadataProvider):
    """Applies `RoomConfig.column_overrides` to the resolved tables.

    Per `docs/metadata.md`, this provider is appended automatically by
    `MetadataFetcher` as the last entry in the stack; it MUST NOT be
    included in user-declared `tiri.toml` stacks.
    """

    @property
    def name(self) -> str:
        return "room_config"

    async def enrich(
        self,
        tables: dict[str, TableMeta],
        room_config: RoomConfig,
    ) -> None:
        if not room_config.column_overrides:
            return

        touched: set[str] = set()
        for override in room_config.column_overrides:
            table_meta = tables.get(override.table)
            if table_meta is None:
                continue  # no such table in this room — silently skip
            column = _find_column(table_meta, override.column)
            if column is None:
                continue  # no such column in physical schema — silently skip

            _apply_scalar(
                table_meta, column, "description",
                override.description, source=self.name,
            )
            _apply_scalar(
                table_meta, column, "value_description",
                override.value_description, source=self.name,
            )
            _extend_list(column.synonyms, override.synonyms)
            # `default_filter` on `ColumnOverride` is the room-level column
            # filter — not currently mirrored on `ColumnMeta`. The SQL agent
            # consumes it via `RoomConfig.default_filters` and column overrides
            # directly. Future revision could surface this on ColumnMeta.

            column.metadata_source = self.name
            touched.add(table_meta.full_name)

        for full_name in touched:
            sources = tables[full_name].metadata_sources
            if self.name not in sources:
                sources.append(self.name)


def _find_column(table_meta: TableMeta, column_name: str):
    for col in table_meta.columns:
        if col.name == column_name:
            return col
    return None


def _apply_scalar(
    table_meta: TableMeta,
    column,
    field_name: str,
    new_value: str,
    *,
    source: str,
) -> None:
    if not new_value:
        return
    old = getattr(column, field_name)
    if old and old != new_value:
        table_meta.conflicts.append(
            MetadataConflict(
                table=table_meta.full_name,
                column=column.name,
                field=field_name,
                values={"existing": old, source: new_value},
                resolved_to=source,
            )
        )
    setattr(column, field_name, new_value)


def _extend_list(target: list, new: list) -> None:
    for item in new:
        if item not in target:
            target.append(item)
