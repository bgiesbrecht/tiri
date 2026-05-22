"""ExampleIndexer — vector store sync + similarity retrieval for ExampleSQLs.

`index()` keeps the vector store aligned with `RoomConfig.examples`:
  - Embed every example question (one `llm.embed()` call).
  - Upsert each into the vector store under its example id.
  - Delete any vector entries for this room whose ids are no longer in
    `config.examples` (handles deletions across re-indexing).

`retrieve()` returns the top-k examples most similar to a question, scoped
to the room via the vector store's `{"room_id": ...}` filter.
"""

from __future__ import annotations

import logging

from tiri.data_models import ExampleSQL, RoomConfig
from tiri.providers.base import LLMProvider, VectorProvider


_log = logging.getLogger("tiri.knowledge.example_indexer")


class ExampleIndexer:
    def __init__(self, llm: LLMProvider, vector: VectorProvider) -> None:
        self._llm = llm
        self._vector = vector

    async def index(self, config: RoomConfig) -> None:
        """Sync the vector store with `config.examples`.

        Upserts every current example and deletes any that have been removed.
        """
        current_ids = [ex.id for ex in config.examples]
        if config.examples:
            embeddings = await self._llm.embed(
                [ex.question for ex in config.examples]
            )
            for example, vector in zip(config.examples, embeddings):
                await self._vector.upsert(
                    id=example.id,
                    vector=vector,
                    payload={
                        "question": example.question,
                        "sql": example.sql,
                        "room_id": config.room_id,
                    },
                )

        # Diff against the store to find ids that were removed from the
        # config but are still in the vector store.
        stored_ids = await self._vector.list_ids(
            filter={"room_id": config.room_id}
        )
        stale_ids = set(stored_ids) - set(current_ids)
        for stale_id in stale_ids:
            await self._vector.delete(stale_id)

    async def retrieve(
        self,
        question: str,
        room_id: str,
        top_k: int = 5,
    ) -> list[ExampleSQL]:
        """Return the top-k examples most similar to `question`, scoped to room."""
        vectors = await self._llm.embed([question])
        if not vectors:
            return []
        matches = await self._vector.query(
            vector=vectors[0],
            top_k=top_k,
            filter={"room_id": room_id},
        )
        results: list[ExampleSQL] = []
        for match in matches:
            payload = match.payload or {}
            q = payload.get("question")
            sql = payload.get("sql")
            if not q or not sql:
                _log.warning(
                    "VectorMatch %s missing question/sql in payload; skipping",
                    match.id,
                )
                continue
            results.append(ExampleSQL(question=q, sql=sql, id=match.id))
        return results
