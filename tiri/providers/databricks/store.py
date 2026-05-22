"""DatabricksStoreProvider — Delta KV table accessed via QueryProvider.

See docs/databricks_providers.md for the specification.
"""

from __future__ import annotations

import json

from tiri.providers.base import QueryProvider, StoreProvider, StoreProviderError


class DatabricksStoreProvider(StoreProvider):
    """Key-value store backed by a Delta table.

    The schema (key STRING, value STRING, updated_at TIMESTAMP) and the table
    name come from configuration; this class assumes the table exists. The
    container creates the table out-of-band at first deploy.
    """

    def __init__(self, table: str, query: QueryProvider) -> None:
        if not table:
            raise StoreProviderError("DatabricksStoreProvider requires table")
        if query is None:
            raise StoreProviderError(
                "DatabricksStoreProvider requires a QueryProvider"
            )
        self._table = table
        self._query = query

    async def get(self, key: str) -> dict | None:
        sql = (
            f"SELECT value FROM {self._table} "
            f"WHERE key = {_quote(key)} LIMIT 1"
        )
        result = await self._query.execute(sql, limit=1)
        if result.row_count == 0:
            return None
        raw = result.rows[0].get("value")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise StoreProviderError(
                f"Corrupt value at key {key!r}: not JSON ({e})"
            ) from e

    async def put(self, key: str, value: dict) -> None:
        payload = json.dumps(value)
        sql = (
            f"MERGE INTO {self._table} AS target "
            f"USING (SELECT {_quote(key)} AS key, {_quote(payload)} AS value, "
            f"current_timestamp() AS updated_at) AS source "
            f"ON target.key = source.key "
            f"WHEN MATCHED THEN UPDATE SET * "
            f"WHEN NOT MATCHED THEN INSERT *"
        )
        await self._query.execute(sql, limit=1)

    async def list_keys(self, prefix: str) -> list[str]:
        sql = (
            f"SELECT key FROM {self._table} "
            f"WHERE key LIKE {_quote(prefix + '%')} ORDER BY key"
        )
        result = await self._query.execute(sql, limit=1_000_000)
        return [row["key"] for row in result.rows]

    async def delete(self, key: str) -> None:
        sql = f"DELETE FROM {self._table} WHERE key = {_quote(key)}"
        await self._query.execute(sql, limit=1)


def _quote(s: str) -> str:
    """SQL-quote a string literal by escaping single quotes."""
    escaped = s.replace("'", "''")
    return f"'{escaped}'"
