"""Proposer — scans thumbs-up turns and proposes new ExampleSQLs for admin review.

Hard rule: never modifies `RoomConfig`. The admin reviews the returned list
and (separately) calls `PATCH /rooms/{id}` to add approved examples.

Benchmark conversations are excluded by convention — `BenchmarkRunner` uses
conversation ids of the form `benchmark-{id}`. Those are real conversations
but they're not user feedback, so they should not feed back into example
suggestions.
"""

from __future__ import annotations

import logging
import uuid

from tiri.data_models import ExampleSQL, LLMMessage, RoomConfig
from tiri.feedback.sql_normalize import normalize_sql
from tiri.providers.base import LLMProvider, StoreProvider


_log = logging.getLogger("tiri.feedback.proposer")
_BENCHMARK_CONV_PREFIX = "benchmark-"


class Proposer:
    def __init__(self, store: StoreProvider, llm: LLMProvider) -> None:
        self._store = store
        self._llm = llm

    async def propose(
        self, room_id: str, config: RoomConfig
    ) -> list[ExampleSQL]:
        """Return ExampleSQL candidates for admin review.

        1. Walk the room→conversation index, skipping `benchmark-` ids.
        2. Walk each conversation's turn index, picking turns with
           `feedback_signal == "up"` that produced SQL.
        3. Filter out turns whose normalized SQL is already in
           `config.examples`.
        4. For each remaining turn, ask the LLM YES/NO; collect the YES
           ones as ExampleSQL.
        """
        existing_sql = {
            normalize_sql(ex.sql) for ex in config.examples if ex.sql
        }

        room_index = await self._store.get(f"room:{room_id}:conversations")
        conversation_ids: list[str] = []
        if isinstance(room_index, dict):
            conversation_ids = list(
                room_index.get("conversation_ids", [])
            )

        proposed: list[ExampleSQL] = []
        for conv_id in conversation_ids:
            if conv_id.startswith(_BENCHMARK_CONV_PREFIX):
                continue
            conv_index = await self._store.get(f"conv:{conv_id}:index")
            turn_ids: list[str] = []
            if isinstance(conv_index, dict):
                turn_ids = list(conv_index.get("turn_ids", []))
            for turn_id in turn_ids:
                turn = await self._store.get(
                    f"conv:{conv_id}:turn:{turn_id}"
                )
                if not isinstance(turn, dict):
                    continue
                if turn.get("feedback_signal") != "up":
                    continue
                sql = turn.get("sql")
                question = turn.get("question")
                # Clarification and error turns may have feedback_signal="up"
                # (e.g. a user thumbs-up a good clarifying question) but yield
                # no example SQL — proposing one with empty SQL is incoherent,
                # so skip them.
                if not sql or not question:
                    continue
                if normalize_sql(sql) in existing_sql:
                    continue
                if await self._llm_says_yes(question, sql):
                    proposed.append(
                        ExampleSQL(
                            question=question,
                            sql=sql,
                            id=uuid.uuid4().hex,
                        )
                    )
        return proposed

    async def _llm_says_yes(self, question: str, sql: str) -> bool:
        prompt = (
            "Given this question and SQL that a user rated helpful, should "
            "it be added as a worked example for the room?\n"
            f"Question: {question}\n"
            f"SQL: {sql}\n"
            "Reply YES or NO with a one-sentence reason."
        )
        response = await self._llm.complete(
            [LLMMessage(role="system", content=prompt)],
            task="clarify",  # cheap/fast model is fine; no dedicated route
        )
        return response.content.strip().upper().startswith("YES")
