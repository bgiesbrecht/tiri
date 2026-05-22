"""DatabricksCatalogProvider — Unity Catalog physical schema via the SDK.

See docs/databricks_providers.md for the specification.
"""

from __future__ import annotations

import asyncio
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.errors.platform import DatabricksError

from tiri.data_models import ColumnMeta, TableMeta
from tiri.providers.base import (
    CatalogProvider,
    CatalogProviderError,
    TableNotFoundError,
)


class DatabricksCatalogProvider(CatalogProvider):
    """Reads physical schema from Unity Catalog via databricks-sdk.

    Returns physical fields only (name, data_type, row_count). Descriptive
    fields (description, synonyms, sample_values, etc.) are deliberately left
    empty — those are populated by the MetadataProvider stack.
    """

    def __init__(
        self,
        host: str,
        token: str,
        *,
        client: WorkspaceClient | None = None,
    ) -> None:
        if not host:
            raise CatalogProviderError(
                "DatabricksCatalogProvider requires host"
            )
        if not token:
            raise CatalogProviderError(
                "DatabricksCatalogProvider requires token"
            )
        self._host = host
        self._token = token
        self._client = client or WorkspaceClient(host=host, token=token)

    async def get_table_meta(self, full_name: str) -> TableMeta:
        info = await _to_thread(self._client.tables.get, full_name)
        columns = [
            ColumnMeta(name=c.name, data_type=str(c.type_text or c.type_name or ""))
            for c in (info.columns or [])
        ]
        row_count = _row_count_from_properties(info.properties or {})
        return TableMeta(
            full_name=full_name,
            columns=columns,
            row_count=row_count,
        )

    async def list_tables(self, catalog: str, schema: str) -> list[str]:
        try:
            tables = await _to_thread(
                lambda: list(
                    self._client.tables.list(
                        catalog_name=catalog, schema_name=schema
                    )
                )
            )
        except (NotFound, PermissionDenied) as e:
            raise CatalogProviderError(
                f"Cannot list tables in {catalog}.{schema}: {e}"
            ) from e
        except DatabricksError as e:
            raise CatalogProviderError(str(e)) from e
        return [t.full_name for t in tables if t.full_name]

    async def list_schemas(self, catalog: str) -> list[str]:
        try:
            schemas = await _to_thread(
                lambda: list(self._client.schemas.list(catalog_name=catalog))
            )
        except (NotFound, PermissionDenied) as e:
            raise CatalogProviderError(
                f"Cannot list schemas in catalog {catalog}: {e}"
            ) from e
        except DatabricksError as e:
            raise CatalogProviderError(str(e)) from e
        return [s.name for s in schemas if getattr(s, "name", None)]

    async def search_tables(
        self, query: str, limit: int = 10
    ) -> list[TableMeta]:
        try:
            tables = await _to_thread(lambda: list(self._client.tables.list()))
        except DatabricksError as e:
            raise CatalogProviderError(str(e)) from e
        q = query.lower()
        matches: list[TableMeta] = []
        for t in tables:
            if not t.full_name:
                continue
            if q in t.full_name.lower() or q in (t.comment or "").lower():
                matches.append(TableMeta(full_name=t.full_name))
                if len(matches) >= limit:
                    break
        return matches


# ── helpers ────────────────────────────────────────────────────────────────


async def _to_thread(func, *args, **kwargs):
    """Run a sync SDK call in a thread so it doesn't block the event loop."""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except NotFound as e:
        raise TableNotFoundError(str(e)) from e
    except PermissionDenied as e:
        raise TableNotFoundError(
            f"caller lacks permission: {e}"
        ) from e
    except DatabricksError as e:
        raise CatalogProviderError(str(e)) from e


def _row_count_from_properties(properties: dict[str, Any]) -> int | None:
    raw = properties.get("numRows") or properties.get("delta.lastCommitInfo.numRows")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
