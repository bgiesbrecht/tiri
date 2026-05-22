"""DatabricksVectorProvider — Vector Search Direct Access index.

See docs/databricks_providers.md for the specification.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from tiri.data_models import VectorMatch
from tiri.providers.base import VectorProvider, VectorProviderError


_DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class DatabricksVectorProvider(VectorProvider):
    """Upsert / query / delete entries in a Databricks Direct Access index."""

    def __init__(
        self,
        host: str,
        token: str,
        index: str,
        endpoint: str = "",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not host:
            raise VectorProviderError("DatabricksVectorProvider requires host")
        if not token:
            raise VectorProviderError(
                "DatabricksVectorProvider requires token"
            )
        if not index:
            raise VectorProviderError(
                "DatabricksVectorProvider requires index"
            )
        self._host = host.rstrip("/")
        self._token = token
        self._index = index
        self._endpoint = endpoint
        self._client = client or httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def upsert(
        self, id: str, vector: list[float], payload: dict
    ) -> None:
        row = {"id": id, "vector": vector, **payload}
        body = {"inputs_json": json.dumps([row])}
        await self._request(
            "PUT",
            f"/api/2.0/vector-search/indexes/{self._index}/upsert-data",
            body,
        )

    async def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filter: dict | None = None,
    ) -> list[VectorMatch]:
        body: dict[str, Any] = {
            "query_vector": vector,
            "num_results": top_k,
            "filters_json": json.dumps(filter or {}),
        }
        data = await self._request(
            "POST",
            f"/api/2.0/vector-search/indexes/{self._index}/query",
            body,
        )
        return _parse_query_response(data)

    async def delete(self, id: str) -> None:
        body = {"primary_keys": [id]}
        await self._request(
            "DELETE",
            f"/api/2.0/vector-search/indexes/{self._index}/delete-data",
            body,
        )

    async def list_ids(self, filter: dict | None = None) -> list[str]:
        # Direct Access indexes have no native enumeration endpoint. Approximate
        # via a query with a zero vector and a large num_results, scoped by the
        # caller's filter. This is bounded by the API's max page size; for very
        # large indexes a future refactor should page or use the underlying
        # Delta source. Acceptable for the example-store use case (rooms with
        # < 10k examples).
        body: dict[str, Any] = {
            "query_vector": [0.0],
            "num_results": 10_000,
            "filters_json": json.dumps(filter or {}),
        }
        data = await self._request(
            "POST",
            f"/api/2.0/vector-search/indexes/{self._index}/query",
            body,
        )
        manifest = data.get("manifest") or {}
        result = data.get("result") or {}
        columns = [c["name"] for c in manifest.get("columns", [])]
        rows = result.get("data_array") or []
        try:
            id_index = columns.index("id")
        except ValueError:
            return []
        return [str(row[id_index]) for row in rows]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, body: dict
    ) -> dict[str, Any]:
        url = f"{self._host}{path}"
        try:
            response = await self._client.request(method, url, json=body)
        except httpx.HTTPError as e:
            raise VectorProviderError(f"HTTP error at {path}: {e}") from e
        if response.status_code >= 400:
            raise VectorProviderError(
                f"HTTP {response.status_code} at {path}: {response.text!r}"
            )
        try:
            return response.json()
        except json.JSONDecodeError as e:
            raise VectorProviderError(
                f"Non-JSON response from {path}: {response.text!r}"
            ) from e


def _parse_query_response(data: dict) -> list[VectorMatch]:
    """Map a Vector Search query response into VectorMatch objects.

    The response shape is `result.data_array` (list of rows) + `manifest.columns`
    (column names). Each row has the primary key, score, and payload columns.
    """
    manifest = data.get("manifest") or {}
    result = data.get("result") or {}
    columns = [c["name"] for c in manifest.get("columns", [])]
    rows = result.get("data_array") or []

    matches: list[VectorMatch] = []
    for row in rows:
        record = dict(zip(columns, row))
        match_id = str(record.pop("id", ""))
        score = float(record.pop("score", 0.0))
        # Drop the vector if present — only payload fields are interesting.
        record.pop("vector", None)
        matches.append(
            VectorMatch(id=match_id, score=score, payload=dict(record))
        )
    # Sort defensively — the API returns descending by score, but we enforce.
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches
