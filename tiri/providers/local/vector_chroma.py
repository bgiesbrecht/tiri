"""ChromaVectorProvider — local Chroma vector store."""

from __future__ import annotations

import asyncio

import chromadb

from tiri.data_models import VectorMatch
from tiri.providers.base import VectorProvider, VectorProviderError


class ChromaVectorProvider(VectorProvider):
    """In-process Chroma. `:memory:` for tests; a directory for persistence.

    Filter dict is passed through to Chroma's `where` parameter. `{"room_id":
    "x"}` matches the canonical filter shape required by the provider
    contract.
    """

    def __init__(
        self,
        path: str = ":memory:",
        collection_name: str = "tiri_examples",
    ) -> None:
        if path == ":memory:":
            self._client = chromadb.EphemeralClient()
        else:
            self._client = chromadb.PersistentClient(path=path)
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            # Pre-supplied vectors — disable Chroma's auto-embedding.
            embedding_function=None,
        )

    async def upsert(
        self,
        id: str,
        vector: list[float],
        payload: dict,
    ) -> None:
        await asyncio.to_thread(
            self._collection.upsert,
            ids=[id],
            embeddings=[vector],
            metadatas=[payload],
        )

    async def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filter: dict | None = None,
    ) -> list[VectorMatch]:
        try:
            result = await asyncio.to_thread(
                self._collection.query,
                query_embeddings=[vector],
                n_results=top_k,
                where=filter or None,
            )
        except Exception as e:  # chromadb raises broad errors; normalize
            raise VectorProviderError(f"Chroma query failed: {e}") from e

        ids_batch = result.get("ids") or [[]]
        distances_batch = result.get("distances") or [[]]
        metas_batch = result.get("metadatas") or [[]]
        ids = ids_batch[0]
        distances = distances_batch[0] if distances_batch else []
        metas = metas_batch[0] if metas_batch else []

        matches: list[VectorMatch] = []
        for i, match_id in enumerate(ids):
            distance = float(distances[i]) if i < len(distances) else 1.0
            # Chroma returns cosine *distance* (lower = more similar). Convert
            # to similarity in [0, 1]: higher = more similar (provider contract).
            score = max(0.0, 1.0 - distance)
            payload = dict(metas[i]) if i < len(metas) and metas[i] else {}
            matches.append(VectorMatch(id=str(match_id), score=score, payload=payload))
        # Provider contract requires descending score order.
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches

    async def delete(self, id: str) -> None:
        try:
            await asyncio.to_thread(self._collection.delete, ids=[id])
        except Exception as e:
            raise VectorProviderError(f"Chroma delete failed: {e}") from e

    async def list_ids(self, filter: dict | None = None) -> list[str]:
        try:
            result = await asyncio.to_thread(
                self._collection.get,
                where=filter or None,
                include=[],  # ids come back regardless; skip embeddings/metadatas
            )
        except Exception as e:
            raise VectorProviderError(f"Chroma list_ids failed: {e}") from e
        return [str(i) for i in (result.get("ids") or [])]
