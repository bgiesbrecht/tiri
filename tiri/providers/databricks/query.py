"""DatabricksQueryProvider — Statement Execution API.

See docs/databricks_providers.md for the specification.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from tiri.data_models import QueryResult
from tiri.providers.base import QueryProvider, QueryProviderError


_DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})


class DatabricksQueryProvider(QueryProvider):
    """Executes SQL via the Databricks Statement Execution API.

    `validate()` runs `EXPLAIN <sql>` on the same warehouse and returns
    (True, None) on success or (False, error_message) on failure. Validation
    has no side effects — it does not appear in the warehouse query history.
    """

    def __init__(
        self,
        host: str,
        token: str,
        warehouse_id: str,
        *,
        wait_timeout: str = "30s",
        poll_interval: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not host:
            raise QueryProviderError("DatabricksQueryProvider requires host")
        if not token:
            raise QueryProviderError("DatabricksQueryProvider requires token")
        if not warehouse_id:
            raise QueryProviderError(
                "DatabricksQueryProvider requires warehouse_id"
            )
        self._host = host.rstrip("/")
        self._token = token
        self._warehouse_id = warehouse_id
        self._wait_timeout = wait_timeout
        self._poll_interval = poll_interval
        self._client = client or httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {token}"},
        )

    # ── public API ─────────────────────────────────────────────────────────

    async def execute(
        self,
        sql: str,
        limit: int = 10_000,
        user_token: str | None = None,
    ) -> QueryResult:
        started = time.monotonic()
        bounded_sql = _apply_limit(sql, limit)
        statement = await self._run_statement(
            bounded_sql, user_token=user_token, wait_timeout=self._wait_timeout
        )
        state = (statement.get("status") or {}).get("state")
        if state != "SUCCEEDED":
            err = (statement.get("status") or {}).get("error") or {}
            raise QueryProviderError(
                f"Statement {state}: {err.get('message', '')}"
            )

        manifest = statement.get("manifest") or {}
        result = statement.get("result") or {}
        columns = [
            c["name"]
            for c in (manifest.get("schema") or {}).get("columns", [])
        ]
        rows_array = result.get("data_array") or []
        rows = [dict(zip(columns, row)) for row in rows_array]
        row_count = result.get("row_count") or len(rows)
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
        explain_sql = f"EXPLAIN {sql}"
        try:
            statement = await self._run_statement(
                explain_sql, user_token=user_token, wait_timeout="10s"
            )
        except QueryProviderError:
            # Infrastructure-level failure — bubble up rather than fake validity.
            raise

        state = (statement.get("status") or {}).get("state")
        if state == "SUCCEEDED":
            return (True, None)
        err = (statement.get("status") or {}).get("error") or {}
        message = err.get("message") or f"EXPLAIN returned state {state!r}"
        return (False, message)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── internals ──────────────────────────────────────────────────────────

    async def _run_statement(
        self,
        sql: str,
        user_token: str | None,
        wait_timeout: str,
    ) -> dict[str, Any]:
        """POST a statement, poll until terminal, return the final body."""
        body = {
            "statement": sql,
            "warehouse_id": self._warehouse_id,
            "wait_timeout": wait_timeout,
            "disposition": "INLINE",
        }
        headers: dict[str, str] = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"

        statement = await self._post_statement(body, headers)
        while True:
            status = (statement.get("status") or {}).get("state")
            if status in _TERMINAL_STATES:
                return statement
            statement_id = statement.get("statement_id")
            if not statement_id:
                raise QueryProviderError(
                    f"Statement is non-terminal but lacks statement_id: "
                    f"{statement!r}"
                )
            await asyncio.sleep(self._poll_interval)
            statement = await self._get_statement(statement_id, headers)

    async def _post_statement(
        self, body: dict, extra_headers: dict[str, str]
    ) -> dict[str, Any]:
        url = f"{self._host}/api/2.0/sql/statements"
        return await self._request("POST", url, body=body, headers=extra_headers)

    async def _get_statement(
        self, statement_id: str, extra_headers: dict[str, str]
    ) -> dict[str, Any]:
        url = f"{self._host}/api/2.0/sql/statements/{statement_id}"
        return await self._request("GET", url, body=None, headers=extra_headers)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        body: dict | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, url, json=body, headers=headers or None
            )
        except httpx.HTTPError as e:
            raise QueryProviderError(f"HTTP error: {e}") from e
        if response.status_code >= 400:
            raise QueryProviderError(
                f"HTTP {response.status_code} at {url}: {response.text!r}"
            )
        try:
            return response.json()
        except json.JSONDecodeError as e:
            raise QueryProviderError(
                f"Non-JSON response from {url}: {response.text!r}"
            ) from e


def _apply_limit(sql: str, limit: int) -> str:
    """Wrap the SQL in a subquery with LIMIT.

    NOTE: this looks more complicated than it needs to be. Do not
    "simplify" it to `f"{sql} LIMIT {limit}"`. Naive append-LIMIT breaks
    when:

      1. The user SQL already has its own LIMIT/OFFSET — appending appends
         a second LIMIT, which is a syntax error in some dialects.
      2. The user SQL ends in `;` — `; LIMIT 10` is invalid.
      3. The user SQL is a `WITH` CTE — `WITH cte AS (...) SELECT * FROM cte LIMIT 10`
         is fine, but `WITH cte AS (...) SELECT * FROM cte; LIMIT 10` is not.

    Wrapping in a subquery is safe regardless of the inner SQL's shape.

    `validate()` does NOT use this wrapper — it runs `EXPLAIN <raw sql>` so
    syntax errors report against the user's actual statement, not the wrapped
    form.
    """
    stripped = sql.rstrip().rstrip(";").rstrip()
    return f"SELECT * FROM ({stripped}) AS _tiri_limited LIMIT {int(limit)}"
