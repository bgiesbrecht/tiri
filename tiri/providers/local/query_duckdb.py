"""DuckDBQueryProvider — runs SQL against local Parquet/CSV files."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import duckdb

from tiri.data_models import QueryResult
from tiri.providers.base import QueryProvider, QueryProviderError


class DuckDBQueryProvider(QueryProvider):
    """In-memory DuckDB. Auto-registers files in `data_dir` as views.

    Files named `{schema}__{table}.parquet` (or `.csv`) become reachable as
    `<catalog>.{schema}.{table}`, where catalog defaults to the constructor
    parameter `catalog`. If no auto-registration is desired, set `data_dir=""`.
    """

    def __init__(
        self,
        data_dir: str = "",
        catalog: str = "tpch",
    ) -> None:
        self._data_dir = data_dir
        self._catalog = catalog
        self._conn = duckdb.connect(database=":memory:")
        if data_dir:
            self._register_files(data_dir)

    def _register_files(self, data_dir: str) -> None:
        root = Path(data_dir)
        if not root.exists():
            return  # silent: dev dir may not exist yet
        for path in list(root.glob("*.parquet")) + list(root.glob("*.csv")):
            stem = path.stem
            if "__" not in stem:
                continue
            # Filename convention: `{schema}__{table}.parquet` → registered
            # as `{catalog}.{schema}.{table}` (catalog defaults to "tpch" to
            # match the demo). The double-underscore separator avoids
            # ambiguity with dots that may appear inside the schema or table
            # name on disk.
            schema, _, table = stem.partition("__")
            view_name = f"{self._catalog}.{schema}.{table}"
            reader = "read_parquet" if path.suffix == ".parquet" else "read_csv_auto"
            self._conn.execute(
                f"CREATE OR REPLACE VIEW {view_name} AS "
                f"SELECT * FROM {reader}('{path.as_posix()}')"
            )

    async def execute(
        self,
        sql: str,
        limit: int = 10_000,
        user_token: str | None = None,
    ) -> QueryResult:
        return await asyncio.to_thread(self._execute_sync, sql, limit)

    def _execute_sync(self, sql: str, limit: int) -> QueryResult:
        started = time.monotonic()
        # Wrap to enforce limit safely, same rationale as DatabricksQueryProvider.
        wrapped = _apply_limit(sql, limit)
        try:
            cursor = self._conn.execute(wrapped)
        except duckdb.Error as e:
            raise QueryProviderError(f"DuckDB execute failed: {e}") from e
        columns = [d[0] for d in cursor.description or []]
        rows_raw = cursor.fetchall()
        rows = [dict(zip(columns, row)) for row in rows_raw]
        row_count = len(rows)
        truncated = row_count >= limit
        duration_ms = int((time.monotonic() - started) * 1000)
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=row_count,
            truncated=truncated,
            duration_ms=duration_ms,
        )

    async def validate(
        self,
        sql: str,
        user_token: str | None = None,
    ) -> tuple[bool, str | None]:
        return await asyncio.to_thread(self._validate_sync, sql)

    def _validate_sync(self, sql: str) -> tuple[bool, str | None]:
        try:
            # EXPLAIN parses and plans the query without executing it.
            self._conn.execute(f"EXPLAIN {sql}")
            return (True, None)
        except duckdb.Error as e:
            return (False, str(e))


def _apply_limit(sql: str, limit: int) -> str:
    stripped = sql.rstrip().rstrip(";").rstrip()
    return f"SELECT * FROM ({stripped}) AS _tiri_limited LIMIT {int(limit)}"
