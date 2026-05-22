"""StaticCatalogProvider — reads physical schema from a local JSON file.

See docs/local_providers.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tiri.data_models import ColumnMeta, TableMeta
from tiri.providers.base import (
    CatalogProvider,
    CatalogProviderError,
    TableNotFoundError,
)


class StaticCatalogProvider(CatalogProvider):
    """Reads `schemas.json` once at construction. No I/O per call.

    Returns physical schema only — `description`, `synonyms`, etc. stay at
    their dataclass defaults. The MetadataProvider stack populates them.
    """

    def __init__(self, schema_file: str) -> None:
        if not schema_file:
            raise CatalogProviderError(
                "StaticCatalogProvider requires schema_file"
            )
        path = Path(schema_file)
        if not path.exists():
            raise CatalogProviderError(
                f"StaticCatalogProvider schema_file not found: {schema_file}"
            )
        try:
            with path.open() as f:
                self._data: dict[str, Any] = json.load(f)
        except json.JSONDecodeError as e:
            raise CatalogProviderError(
                f"StaticCatalogProvider schema_file is not valid JSON: {e}"
            ) from e
        if not isinstance(self._data, dict):
            raise CatalogProviderError(
                f"StaticCatalogProvider schema_file root must be a JSON object; "
                f"got {type(self._data).__name__}"
            )

    async def get_table_meta(self, full_name: str) -> TableMeta:
        entry = self._data.get(full_name)
        if entry is None:
            raise TableNotFoundError(
                f"Table {full_name!r} not found in static schema file"
            )
        columns = [
            ColumnMeta(name=c["name"], data_type=c["data_type"])
            for c in entry.get("columns", [])
        ]
        return TableMeta(
            full_name=full_name,
            columns=columns,
            row_count=entry.get("row_count"),
        )

    async def list_tables(self, catalog: str, schema: str) -> list[str]:
        prefix = f"{catalog}.{schema}."
        return sorted(k for k in self._data if k.startswith(prefix))

    async def list_schemas(self, catalog: str) -> list[str]:
        prefix = f"{catalog}."
        schemas: set[str] = set()
        for full_name in self._data:
            if not full_name.startswith(prefix):
                continue
            # full_name = "catalog.schema.table" → extract middle component
            parts = full_name.split(".")
            if len(parts) >= 3:
                schemas.add(parts[1])
        return sorted(schemas)

    async def search_tables(
        self, query: str, limit: int = 10
    ) -> list[TableMeta]:
        q = query.lower()
        matches: list[TableMeta] = []
        for full_name in self._data:
            if q in full_name.lower():
                matches.append(TableMeta(full_name=full_name))
                if len(matches) >= limit:
                    break
        return matches
