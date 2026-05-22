"""Databricks metadata providers — Unity Catalog annotations + Delta table.

See docs/databricks_providers.md and docs/metadata.md for specifications.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.errors.platform import DatabricksError

from tiri.data_models import MetadataConflict, RoomConfig, TableMeta
from tiri.providers.base import (
    MetadataProvider,
    MetadataProviderError,
    QueryProvider,
)


_log = logging.getLogger("tiri.providers.databricks.metadata")

_HIGH_CARDINALITY_NAMES = frozenset(
    {"id", "uuid", "email", "description", "notes", "comment"}
)


class UCAnnotationsMetadataProvider(MetadataProvider):
    """Reads Unity Catalog table and column comments and (optionally) collects
    `sample_values` for low-cardinality string columns.

    Typically the first entry in the metadata stack — provides baseline
    descriptions that YAML or other sources can override later.
    """

    def __init__(
        self,
        host: str = "",
        token: str = "",
        query: QueryProvider | None = None,
        *,
        sample_values_enabled: bool = True,
        sample_values_max_distinct: int = 50,
        client: WorkspaceClient | None = None,
    ) -> None:
        self._host = host
        self._token = token
        self._query = query
        self._sample_values_enabled = sample_values_enabled
        self._sample_values_max_distinct = sample_values_max_distinct
        if client is not None:
            self._client = client
        elif host and token:
            self._client = WorkspaceClient(host=host, token=token)
        else:
            self._client = None  # type: ignore[assignment]

    @property
    def name(self) -> str:
        return "uc_annotations"

    async def enrich(
        self,
        tables: dict[str, TableMeta],
        room_config: RoomConfig,
    ) -> None:
        if self._client is None:
            _log.warning(
                "UCAnnotationsMetadataProvider has no client (missing host/token); "
                "skipping enrichment"
            )
            return

        for full_name, table_meta in tables.items():
            try:
                info = await asyncio.to_thread(
                    self._client.tables.get, full_name
                )
            except (NotFound, PermissionDenied) as e:
                _log.warning(
                    "Skipping %s — UC lookup failed: %s", full_name, e
                )
                continue
            except DatabricksError as e:
                raise MetadataProviderError(
                    f"UC tables.get failed for {full_name}: {e}"
                ) from e

            self._apply_table_comment(table_meta, info)
            self._apply_column_comments(table_meta, info)

            if self._sample_values_enabled and self._query is not None:
                await self._populate_sample_values(table_meta)

            if self.name not in table_meta.metadata_sources:
                table_meta.metadata_sources.append(self.name)

    # ── application helpers ────────────────────────────────────────────────

    def _apply_table_comment(self, table_meta: TableMeta, info: Any) -> None:
        comment = (info.comment or "").strip()
        if not comment:
            return
        if table_meta.description and table_meta.description != comment:
            table_meta.conflicts.append(
                MetadataConflict(
                    table=table_meta.full_name,
                    column=None,
                    field="description",
                    values={
                        "existing": table_meta.description,
                        self.name: comment,
                    },
                    resolved_to=self.name,
                )
            )
        table_meta.description = comment

    def _apply_column_comments(
        self, table_meta: TableMeta, info: Any
    ) -> None:
        by_name = {c.name: c for c in (info.columns or [])}
        for col in table_meta.columns:
            uc_col = by_name.get(col.name)
            if uc_col is None:
                continue
            comment = (uc_col.comment or "").strip()
            if not comment:
                continue
            if col.description and col.description != comment:
                table_meta.conflicts.append(
                    MetadataConflict(
                        table=table_meta.full_name,
                        column=col.name,
                        field="description",
                        values={
                            "existing": col.description,
                            self.name: comment,
                        },
                        resolved_to=self.name,
                    )
                )
            col.description = comment
            col.metadata_source = self.name

    async def _populate_sample_values(self, table_meta: TableMeta) -> None:
        if self._query is None:
            return
        for col in table_meta.columns:
            if not _eligible_for_sample_values(col):
                continue
            try:
                result = await self._query.execute(
                    f"SELECT DISTINCT {col.name} FROM {table_meta.full_name} "
                    f"LIMIT {self._sample_values_max_distinct}",
                    limit=self._sample_values_max_distinct,
                )
            except Exception as e:
                _log.warning(
                    "Sample-value collection failed for %s.%s: %s",
                    table_meta.full_name,
                    col.name,
                    e,
                )
                continue
            new_values = [
                str(row.get(col.name))
                for row in result.rows
                if row.get(col.name) is not None
            ]
            for v in new_values:
                if v not in col.sample_values:
                    col.sample_values.append(v)


def _eligible_for_sample_values(col: Any) -> bool:
    """Eligible if string-typed, not in high-cardinality exclusion list,
    and not flagged as high-cardinality.

    NOTE: the doc also lists a `row_count < 1_000_000` threshold but we
    do NOT enforce it here. `TableInfo.properties.numRows` is frequently
    missing or stale in UC, so gating on it would silently break sample-value
    collection on large tables that happen to have unpopulated stats. The
    column-name exclusion list + `is_high_cardinality` flag give us the
    same protection without depending on flaky catalog statistics.
    Re-evaluate when UC improves stats freshness.
    """
    if col.is_high_cardinality:
        return False
    if col.name.lower() in _HIGH_CARDINALITY_NAMES:
        return False
    type_upper = (col.data_type or "").upper()
    return "STRING" in type_upper or "VARCHAR" in type_upper or "CHAR" in type_upper


# ────────────────────────────────────────────────────────────────────────────


class DeltaTableMetadataProvider(MetadataProvider):
    """Reads metadata from a (table, column, field, value) Delta table.

    Schema and merge behavior follow docs/metadata.md. Implementation is
    deferred — placeholder in place so container.py can reference it; raises
    when used until the implementation lands.
    """

    def __init__(
        self,
        name: str,
        table: str,
        query: QueryProvider | None = None,
    ) -> None:
        self._name = name
        self._table = table
        self._query = query

    @property
    def name(self) -> str:
        return self._name

    async def enrich(
        self,
        tables: dict[str, TableMeta],
        room_config: RoomConfig,
    ) -> None:
        raise MetadataProviderError(
            "DeltaTableMetadataProvider not yet implemented — "
            "scheduled for a follow-up step"
        )
