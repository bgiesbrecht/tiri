"""RoomEngine + RoomManager — pipeline orchestrator and room lifecycle.

`RoomEngine.chat()` is the single entry point for conversation requests.
`RoomEngine.stream_chat()` is its SSE-yielding variant.

`RoomManager` handles room CRUD: create / get / update / delete.

Both classes are constructed per request by the API layer — they hold no
state beyond provider references.

Pipeline invariants (docs/room_engine.md):
  1. `query.validate()` is called before `query.execute()` — satisfied by
     `SQLAgent`'s self-correction loop. `RoomEngine` does not validate again.
  2. A `ConversationTurn` is persisted before returning, even on error paths.
  3. A turn has exactly one of `sql`, `clarification_question`, or `error`.
  4. `RoomConfig` is loaded fresh per request — no instance-level cache.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from tiri.data_models import (
    ClarifyResult,
    ContextPackage,
    ConversationTurn,
    ExampleSQL,
    HypothesisResult,
    IntentResult,
    QueryResult,
    ReasoningPlan,
    ReasoningStep,
    RoomConfig,
    SQLResult,
    SynthesizedAnswer,
    VizResult,
)
from tiri.engine.agents.clarify_agent import ClarifyAgent
from tiri.engine.agents.hypothesis_agent import (
    HypothesisAgent,
    HypothesisError,
)
from tiri.engine.agents.intent_agent import IntentAgent
from tiri.engine.agents.planning_agent import PlanningAgent
from tiri.engine.agents.sql_agent import SQLAgent
from tiri.engine.agents.synthesis_agent import (
    SynthesisAgent,
    SynthesisError,
)
from tiri.container import SingleModelLLMProvider
from tiri.engine.agents.viz_agent import VizAgent
from tiri.knowledge.context_builder import ContextBuilder
from tiri.knowledge.example_indexer import ExampleIndexer
from tiri.knowledge.mcp_resolver import MCPResolver
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    MCPProvider,
    MetadataProvider,
    QueryProvider,
    StoreProvider,
    VectorProvider,
)


_log = logging.getLogger("tiri.engine.room_engine")

_DEFAULT_HISTORY_WINDOW = 10
_DEFAULT_INTENT_THRESHOLD = 0.7
_DEFAULT_SQL_MAX_RETRIES = 3
_DEFAULT_QUERY_ROW_LIMIT = 10_000


# ────────────────────────────────────────────────────────────────────────────
# Error types
# ────────────────────────────────────────────────────────────────────────────


class RoomEngineError(Exception):
    """Base for room-engine errors."""


class RoomNotFoundError(RoomEngineError):
    """No RoomConfig is stored under the given room_id."""


class PipelineError(RoomEngineError):
    """Wraps a provider error that escaped the pipeline. Inner exception is on `__cause__`."""


class _SQLStepFailure(Exception):
    """Internal signal: a step in a ReasoningPlan failed SQL validation.
    Caught at the boundary of `_route_intent` / `stream_chat` and converted
    into a user-facing error turn. Not part of the public surface."""

    def __init__(self, *, step_id: str, message: str) -> None:
        super().__init__(message)
        self.step_id = step_id
        self.message = message


# ────────────────────────────────────────────────────────────────────────────
# RoomEngine
# ────────────────────────────────────────────────────────────────────────────


class RoomEngine:
    def __init__(
        self,
        llm: LLMProvider,
        catalog: CatalogProvider,
        metadata_providers: list[MetadataProvider],
        query: QueryProvider,
        vector: VectorProvider,
        store: StoreProvider,
        *,
        mcp_providers: dict[str, MCPProvider] | None = None,
        llm_backends: dict[str, LLMProvider] | None = None,
        history_window: int = _DEFAULT_HISTORY_WINDOW,
        intent_threshold: float = _DEFAULT_INTENT_THRESHOLD,
        sql_max_retries: int = _DEFAULT_SQL_MAX_RETRIES,
        query_row_limit: int = _DEFAULT_QUERY_ROW_LIMIT,
    ) -> None:
        self._llm = llm
        self._catalog = catalog
        self._metadata_providers = list(metadata_providers)
        self._query = query
        self._vector = vector
        self._store = store
        # EXT-5: registry of external MCP providers, keyed by URL. The room's
        # `mcp_servers` list authorizes which entries here may actually be
        # called per request — an entry in this registry is necessary but
        # not sufficient. Empty / None when the deployment has no MCP wiring.
        self._mcp_providers: dict[str, MCPProvider] = dict(mcp_providers or {})
        # UI: registry of individual LLM backends keyed by provider name (the
        # `[llm.providers.NAME]` blocks in tiri.toml). Used by the UI's
        # `model_override` parameter to pin a chat invocation to one
        # specific `provider::model`, bypassing the per-task router. Empty
        # dict when not provided — model_override then logs a WARNING and
        # falls through to the router.
        self._llm_backends: dict[str, LLMProvider] = dict(llm_backends or {})
        self._history_window = history_window
        self._intent_threshold = intent_threshold
        self._sql_max_retries = sql_max_retries
        self._query_row_limit = query_row_limit

    # ── public ─────────────────────────────────────────────────────────────

    async def chat(
        self,
        room_id: str,
        conversation_id: str,
        question: str,
        user_token: str | None = None,
        model_override: str | None = None,
    ) -> ConversationTurn:
        started = time.monotonic()
        config = await self._load_room_config(room_id)
        history = await self._load_history(conversation_id)
        llm = self._resolve_llm(model_override)

        builder = ContextBuilder(
            catalog=self._catalog,
            metadata_providers=self._metadata_providers,
            query=self._query,
            llm=llm,
            vector=self._vector,
        )
        context = await builder.build(
            question=question,
            config=config,
            history=history,
            history_window=self._history_window,
        )

        # EXT-5: enrich context with external tool resolutions when the room
        # opts in. Zero work when config.mcp_servers is empty — preserves
        # pre-EXT-5 latency profile for the common case.
        await self._maybe_resolve_mcp(question, config, context)

        intent_agent = IntentAgent(
            llm, confidence_threshold=self._intent_threshold
        )
        intent = await intent_agent.run(question, context)

        turn = await self._route_intent(
            intent=intent,
            question=question,
            context=context,
            config=config,
            conversation_id=conversation_id,
            user_token=user_token,
            started=started,
            llm=llm,
        )
        await self._persist_turn(room_id, conversation_id, turn)
        return turn

    async def stream_chat(
        self,
        room_id: str,
        conversation_id: str,
        question: str,
        user_token: str | None = None,
        model_override: str | None = None,
    ) -> AsyncIterator[dict]:
        """SSE-friendly pipeline. Yields status/sql/result/viz/done events.

        Mirrors `chat()` but emits intermediate events. Errors are surfaced
        as `{"type": "error", "message": ...}` rather than raised.
        """
        try:
            yield {"type": "status", "text": "Loading room config..."}
            config = await self._load_room_config(room_id)

            yield {"type": "status", "text": "Loading conversation history..."}
            history = await self._load_history(conversation_id)

            llm = self._resolve_llm(model_override)

            yield {"type": "status", "text": "Building context..."}
            builder = ContextBuilder(
                catalog=self._catalog,
                metadata_providers=self._metadata_providers,
                query=self._query,
                llm=llm,
                vector=self._vector,
            )
            context = await builder.build(
                question=question,
                config=config,
                history=history,
                history_window=self._history_window,
            )

            await self._maybe_resolve_mcp(question, config, context)
            if context.mcp_context:
                yield {"type": "mcp_context", "entries": list(context.mcp_context)}

            yield {"type": "status", "text": "Classifying question..."}
            intent_agent = IntentAgent(
                llm, confidence_threshold=self._intent_threshold
            )
            intent = await intent_agent.run(question, context)

            started = time.monotonic()
            if intent.intent == "out_of_scope":
                turn = self._error_turn(
                    room_id=room_id,
                    conversation_id=conversation_id,
                    question=question,
                    error="This question is outside the scope of this room.",
                    started=started,
                )
                yield {"type": "error", "message": turn.error}
            elif (
                intent.intent == "clarify_needed"
                or intent.confidence < self._intent_threshold
            ):
                clarify_agent = ClarifyAgent(llm)
                clarify_result = await clarify_agent.run(
                    question, context, intent
                )
                turn = self._clarify_turn(
                    room_id=room_id,
                    conversation_id=conversation_id,
                    question=question,
                    clarification=clarify_result.question,
                    started=started,
                )
                yield {"type": "clarify", "question": clarify_result.question}
            else:
                yield {"type": "status", "text": "Planning..."}
                plan = await PlanningAgent(llm).plan(question, context)
                if len(plan.steps) > 1:
                    yield {
                        "type": "plan",
                        "steps": [
                            {
                                "step_id": s.step_id,
                                "description": s.description,
                                "depends_on": list(s.depends_on),
                            }
                            for s in plan.steps
                        ],
                        "synthesis_instruction": plan.synthesis_instruction,
                    }
                try:
                    results = await self._execute_plan(
                        plan, context, intent, user_token, llm=llm,
                    )
                except _SQLStepFailure as failure:
                    turn = self._error_turn(
                        room_id=room_id,
                        conversation_id=conversation_id,
                        question=question,
                        error=failure.message,
                        started=started,
                    )
                    yield {"type": "error", "message": turn.error}
                else:
                    primary_step = plan.steps[0]
                    primary_result = results[0]
                    yield {"type": "sql", "sql": primary_step.sql or ""}
                    yield {
                        "type": "result",
                        "columns": primary_result.columns,
                        "rows": primary_result.rows,
                        "truncated": primary_result.truncated,
                    }
                    if len(plan.steps) > 1:
                        yield {
                            "type": "steps",
                            "results": [
                                {
                                    "step_id": s.step_id,
                                    "description": s.description,
                                    "sql": s.sql or "",
                                    "columns": r.columns,
                                    "row_count": r.row_count,
                                }
                                for s, r in zip(plan.steps, results)
                            ],
                        }
                    viz_agent = VizAgent(llm)
                    viz_result = await viz_agent.run(
                        question, primary_result, context
                    )
                    yield {
                        "type": "viz",
                        "spec": viz_result.vega_lite_spec,
                        "summary": viz_result.summary,
                    }
                    synthesized = await self._synthesize(
                        question=question,
                        plan=plan,
                        results=results,
                        context=context,
                        llm=llm,
                    )
                    attached_answer = self._answer_to_attach(plan, synthesized)
                    if attached_answer is not None:
                        yield {
                            "type": "synthesis",
                            "answer": attached_answer.answer,
                            "data_supports": attached_answer.data_supports,
                            "data_does_not_support": attached_answer.data_does_not_support,
                            "would_need": attached_answer.would_need,
                            "confidence": attached_answer.confidence,
                            "confidence_rationale": attached_answer.confidence_rationale,
                        }
                    hypothesis_result = await self._maybe_generate_hypotheses(
                        question=question,
                        plan=plan,
                        results=results,
                        synthesized=synthesized,
                        context=context,
                        config=config,
                        llm=llm,
                    )
                    if hypothesis_result is not None:
                        yield {
                            "type": "hypotheses",
                            "disclaimer": hypothesis_result.disclaimer,
                            "confidence": hypothesis_result.confidence,
                            "hypotheses": [
                                {
                                    "statement": h.statement,
                                    "supporting_patterns": h.supporting_patterns,
                                    "contradicting_patterns": h.contradicting_patterns,
                                    "testability": h.testability,
                                    "suggested_test": h.suggested_test,
                                    "domain_knowledge_used": h.domain_knowledge_used,
                                }
                                for h in hypothesis_result.hypotheses
                            ],
                        }
                    turn = self._sql_turn(
                        room_id=room_id,
                        conversation_id=conversation_id,
                        question=question,
                        sql=primary_step.sql or "",
                        query_result=primary_result,
                        viz=viz_result,
                        synthesized_answer=attached_answer,
                        hypothesis_result=hypothesis_result,
                        started=started,
                    )

            await self._persist_turn(room_id, conversation_id, turn)
            yield {"type": "done", "turn_id": turn.turn_id}
        except RoomNotFoundError as e:
            yield {"type": "error", "message": str(e)}
        except Exception as e:
            _log.exception("stream_chat pipeline failed")
            yield {"type": "error", "message": f"pipeline error: {e}"}

    # ── intent → turn routing ──────────────────────────────────────────────

    async def _route_intent(
        self,
        *,
        intent: IntentResult,
        question: str,
        context,
        config: RoomConfig,
        conversation_id: str,
        user_token: str | None,
        started: float,
        llm: LLMProvider,
    ) -> ConversationTurn:
        room_id = config.room_id

        if intent.intent == "out_of_scope":
            return self._error_turn(
                room_id=room_id,
                conversation_id=conversation_id,
                question=question,
                error="This question is outside the scope of this room.",
                started=started,
            )

        if (
            intent.intent == "clarify_needed"
            or intent.confidence < self._intent_threshold
        ):
            clarify_agent = ClarifyAgent(llm)
            clarify_result = await clarify_agent.run(question, context, intent)
            return self._clarify_turn(
                room_id=room_id,
                conversation_id=conversation_id,
                question=question,
                clarification=clarify_result.question,
                started=started,
            )

        # sql_query path — plan → execute steps → synthesize
        plan = await PlanningAgent(llm).plan(question, context)
        try:
            results = await self._execute_plan(
                plan, context, intent, user_token, llm=llm,
            )
        except _SQLStepFailure as failure:
            return self._error_turn(
                room_id=room_id,
                conversation_id=conversation_id,
                question=question,
                error=failure.message,
                started=started,
            )

        primary_step = plan.steps[0]
        primary_result = results[0]
        viz_agent = VizAgent(llm)
        viz_result = await viz_agent.run(
            question, primary_result, context
        )

        synthesized_answer = await self._synthesize(
            question=question,
            plan=plan,
            results=results,
            context=context,
            llm=llm,
        )
        attached_answer = self._answer_to_attach(plan, synthesized_answer)

        hypothesis_result = await self._maybe_generate_hypotheses(
            question=question,
            plan=plan,
            results=results,
            synthesized=synthesized_answer,
            context=context,
            config=config,
            llm=llm,
        )

        return self._sql_turn(
            room_id=room_id,
            conversation_id=conversation_id,
            question=question,
            sql=primary_step.sql or "",
            query_result=primary_result,
            viz=viz_result,
            synthesized_answer=attached_answer,
            hypothesis_result=hypothesis_result,
            started=started,
        )

    # ── turn constructors ─────────────────────────────────────────────────

    @staticmethod
    def _new_turn_id() -> str:
        return uuid.uuid4().hex

    @classmethod
    def _error_turn(
        cls,
        *,
        room_id: str,
        conversation_id: str,
        question: str,
        error: str,
        started: float,
    ) -> ConversationTurn:
        return ConversationTurn(
            room_id=room_id,
            conversation_id=conversation_id,
            turn_id=cls._new_turn_id(),
            question=question,
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    @classmethod
    def _clarify_turn(
        cls,
        *,
        room_id: str,
        conversation_id: str,
        question: str,
        clarification: str,
        started: float,
    ) -> ConversationTurn:
        return ConversationTurn(
            room_id=room_id,
            conversation_id=conversation_id,
            turn_id=cls._new_turn_id(),
            question=question,
            clarification_question=clarification,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    @classmethod
    def _sql_turn(
        cls,
        *,
        room_id: str,
        conversation_id: str,
        question: str,
        sql: str,
        query_result: QueryResult,
        viz: VizResult,
        synthesized_answer: SynthesizedAnswer | None,
        hypothesis_result: HypothesisResult | None = None,
        started: float,
    ) -> ConversationTurn:
        return ConversationTurn(
            room_id=room_id,
            conversation_id=conversation_id,
            turn_id=cls._new_turn_id(),
            question=question,
            sql=sql,
            query_result=query_result,
            viz=viz,
            synthesized_answer=synthesized_answer,
            hypothesis_result=hypothesis_result,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # ── model_override resolution (UI per-question backend pinning) ───────

    def _resolve_llm(self, model_override: str | None) -> LLMProvider:
        """Resolve the LLM the rest of the chat call will use.

        `model_override` is a `provider::model` string the UI passes when the
        operator wants this one chat invocation pinned to a specific backend
        and model (e.g. side-by-side comparison across backends in AskView).
        When unset, the per-task router is used — the standard production
        path. When set but malformed or referring to an unknown backend, a
        WARNING is logged and the router is used — degrades safely rather
        than failing the chat call over a UI parameter bug.

        Embedding always routes through the original router via
        `SingleModelLLMProvider.embed_provider=self._llm`. Anthropic and
        Ollama backends don't support embeddings, so a chat-only override
        would break ContextBuilder's embed call without this delegation.
        """
        if not model_override:
            return self._llm
        if "::" not in model_override:
            _log.warning(
                "Invalid model_override %r (expected 'provider::model'); "
                "using router",
                model_override,
            )
            return self._llm
        backend_name, model = model_override.split("::", 1)
        backend = self._llm_backends.get(backend_name)
        if backend is None:
            _log.warning(
                "model_override %r references unknown backend %r; using "
                "router. Registered backends: %s",
                model_override,
                backend_name,
                sorted(self._llm_backends),
            )
            return self._llm
        return SingleModelLLMProvider(
            backend=backend, model=model, embed_provider=self._llm
        )

    # ── MCP context enrichment (EXT-5) ────────────────────────────────────

    async def _maybe_resolve_mcp(
        self,
        question: str,
        config: RoomConfig,
        context: ContextPackage,
    ) -> None:
        """Populate `context.mcp_context` from authorized MCP servers.

        Short-circuits to a no-op when the room declares no MCP servers OR
        when no providers are registered in this engine instance — this is
        the path the test_room_engine and test_api suites take, and it MUST
        match pre-EXT-5 behavior exactly (no LLM call, no network, no
        latency overhead).

        Failures are absorbed by MCPResolver; this method never raises.
        """
        # Security boundary: only call MCP servers explicitly declared in
        # RoomConfig.mcp_servers. A room author opts in to external reach
        # by listing URLs here. An empty list means this room makes no
        # external calls — existing behavior is fully preserved.
        # URLs listed here but absent from self._mcp_providers are
        # misconfigurations (logged as WARNING), not security violations.
        if not config.mcp_servers or not self._mcp_providers:
            return
        resolver = MCPResolver(self._mcp_providers)
        context.mcp_context = await resolver.resolve(
            question, config.mcp_servers
        )

    # ── multi-query orchestration (EXT-1) ─────────────────────────────────

    async def _execute_plan(
        self,
        plan: ReasoningPlan,
        context: ContextPackage,
        intent: IntentResult,
        user_token: str | None,
        *,
        llm: LLMProvider | None = None,
    ) -> list[QueryResult]:
        """Run SQLAgent once per step in declared order, populating
        `step.sql` and `step.result` as it goes. For MVP execution is
        sequential — `depends_on` is metadata, not data flow. Returns the
        ordered list of QueryResult objects (one per step).

        Raises _SQLStepFailure on the first step whose SQL fails to validate
        through SQLAgent's self-correction loop. The caller turns this into
        an error turn rather than partially executing a broken plan."""
        sql_agent = SQLAgent(
            llm or self._llm,
            self._query,
            max_retries=self._sql_max_retries,
        )
        results: list[QueryResult] = []
        for step in plan.steps:
            sql_result = await sql_agent.run(
                step.description, context, intent, user_token=user_token
            )
            if not sql_result.is_valid:
                raise _SQLStepFailure(
                    step_id=step.step_id,
                    message=(
                        f"{step.step_id}: "
                        f"{sql_result.error or 'SQL generation failed'}"
                    ),
                )
            step.sql = sql_result.sql
            query_result = await self._query.execute(
                sql_result.sql,
                limit=self._query_row_limit,
                user_token=user_token,
            )
            step.result = query_result
            results.append(query_result)
        return results

    async def _synthesize(
        self,
        *,
        question: str,
        plan: ReasoningPlan,
        results: list[QueryResult],
        context: ContextPackage,
        llm: LLMProvider | None = None,
    ) -> SynthesizedAnswer:
        """SynthesisAgent.synthesize() with logging on failure. The error
        propagates — never silently ship a turn whose synthesis violated
        the causal-language ban."""
        agent = SynthesisAgent(llm or self._llm)
        try:
            return await agent.synthesize(question, plan, results, context)
        except SynthesisError:
            _log.exception("SynthesisAgent failed")
            raise

    # ── hypothesis mode (EXT-11) ──────────────────────────────────────────

    async def _maybe_generate_hypotheses(
        self,
        *,
        question: str,
        plan: ReasoningPlan,
        results: list[QueryResult],
        synthesized: SynthesizedAnswer,
        context: ContextPackage,
        config: RoomConfig,
        llm: LLMProvider | None = None,
    ) -> HypothesisResult | None:
        """Run HypothesisAgent only when all three gates pass:

          1. RoomConfig.hypothesis_mode_enabled is True
             — room author opt-in; the room's audience accepts hypotheses.
          2. len(plan.steps) > 1
             — single-query turns have no multi-step evidence to reason over.
          3. The question phrasing is causal ("why", "what caused", ...)
             — non-causal questions in a hypothesis-enabled room still get
             the witness-mode answer, no hypothesis layer.

        All three gates MUST hold. The order above is the cheap-to-expensive
        order: scalar bool, integer compare, then string scan — short-
        circuit gives zero overhead for rooms that don't opt in.

        Returns None when any gate fails OR when HypothesisAgent itself
        raises HypothesisError (causal-language violation, malformed
        response). A bad hypothesis must not crash the turn; the
        synthesized answer is still valid and useful on its own.
        """
        if not config.hypothesis_mode_enabled:
            return None
        if len(plan.steps) <= 1:
            return None
        if not _is_causal_question(question):
            return None
        agent = HypothesisAgent(llm or self._llm)
        try:
            return await agent.run(
                question=question,
                plan=plan,
                results=results,
                synthesized=synthesized,
                context=context,
            )
        except HypothesisError:
            _log.exception(
                "HypothesisAgent failed; turn proceeds without hypothesis_result"
            )
            return None

    @staticmethod
    def _answer_to_attach(
        plan: ReasoningPlan, synthesized: SynthesizedAnswer
    ) -> SynthesizedAnswer | None:
        """ConversationTurn.synthesized_answer population rules (EXT-7 + EXT-1):
          - multi-step plan: always attach (the multi-query case needs to
            surface the synthesis itself, since the user can't see the
            individual step results in the primary turn fields)
          - single-step plan: attach only when confidence is medium or low,
            matching the pre-EXT-1 single-query behavior (high-confidence
            direct queries don't need uncertainty narration — the
            sql/result/viz already tell the story).
        """
        if len(plan.steps) > 1:
            return synthesized
        if synthesized.confidence == "high":
            return None
        return synthesized

    # ── persistence ───────────────────────────────────────────────────────

    async def _load_room_config(self, room_id: str) -> RoomConfig:
        raw = await self._store.get(f"room:{room_id}:config")
        if raw is None:
            raise RoomNotFoundError(f"Room not found: {room_id}")
        return RoomConfig.from_dict(raw)

    async def _load_history(
        self, conversation_id: str
    ) -> list[ConversationTurn]:
        index = await self._store.get(f"conv:{conversation_id}:index")
        if not index:
            return []
        turn_ids = index.get("turn_ids", []) if isinstance(index, dict) else []
        history: list[ConversationTurn] = []
        for turn_id in turn_ids[-self._history_window :]:
            raw = await self._store.get(
                f"conv:{conversation_id}:turn:{turn_id}"
            )
            if raw is None:
                continue
            history.append(_turn_from_dict(raw))
        return history

    async def _persist_turn(
        self,
        room_id: str,
        conversation_id: str,
        turn: ConversationTurn,
    ) -> None:
        await self._store.put(
            f"conv:{conversation_id}:turn:{turn.turn_id}", asdict(turn)
        )
        existing = await self._store.get(f"conv:{conversation_id}:index")
        turn_ids: list[str] = []
        if isinstance(existing, dict):
            turn_ids = list(existing.get("turn_ids", []))
        turn_ids.append(turn.turn_id)
        await self._store.put(
            f"conv:{conversation_id}:index", {"turn_ids": turn_ids}
        )
        # Room → conversation index — first turn in a new conversation must
        # register the conversation_id under the room. Proposer and
        # RoomManager.delete() depend on this index.
        room_index = await self._store.get(f"room:{room_id}:conversations")
        conversation_ids: list[str] = []
        if isinstance(room_index, dict):
            conversation_ids = list(room_index.get("conversation_ids", []))
        if conversation_id not in conversation_ids:
            conversation_ids.append(conversation_id)
            await self._store.put(
                f"room:{room_id}:conversations",
                {"conversation_ids": conversation_ids},
            )


# ────────────────────────────────────────────────────────────────────────────
# RoomManager
# ────────────────────────────────────────────────────────────────────────────


class RoomManager:
    def __init__(
        self,
        store: StoreProvider,
        vector: VectorProvider,
        llm: LLMProvider,
    ) -> None:
        self._store = store
        self._vector = vector
        self._llm = llm
        self._indexer = ExampleIndexer(llm, vector)

    async def create(self, config: RoomConfig) -> str:
        await self._store.put(
            f"room:{config.room_id}:config", asdict(config)
        )
        await self._indexer.index(config)
        return config.room_id

    async def get(self, room_id: str) -> RoomConfig:
        raw = await self._store.get(f"room:{room_id}:config")
        if raw is None:
            raise RoomNotFoundError(f"Room not found: {room_id}")
        return RoomConfig.from_dict(raw)

    async def update(
        self, room_id: str, partial: dict[str, Any]
    ) -> RoomConfig:
        current_raw = await self._store.get(f"room:{room_id}:config")
        if current_raw is None:
            raise RoomNotFoundError(f"Room not found: {room_id}")
        merged: dict[str, Any] = dict(current_raw)
        merged.update(partial)
        new_config = RoomConfig.from_dict(merged)
        await self._store.put(
            f"room:{room_id}:config", asdict(new_config)
        )

        # Re-index only if the examples list changed. Compare by id set —
        # ordering doesn't matter and re-indexing identical content is
        # wasteful (and not required by docs/room_engine.md test case 12).
        old_ids = {
            e.get("id")
            for e in (current_raw.get("examples") or [])
            if isinstance(e, dict)
        }
        new_ids = {ex.id for ex in new_config.examples}
        if old_ids != new_ids or _example_content_changed(
            current_raw.get("examples") or [], new_config.examples
        ):
            await self._indexer.index(new_config)

        return new_config

    async def delete(self, room_id: str) -> None:
        # 1. Vector entries for this room.
        ids_in_room = await self._vector.list_ids({"room_id": room_id})
        for entry_id in ids_in_room:
            await self._vector.delete(entry_id)

        # 2. Conversation turns for this room — enumerate via the
        #    room→conversation index, not a full store scan.
        room_index = await self._store.get(f"room:{room_id}:conversations")
        conv_ids: list[str] = []
        if isinstance(room_index, dict):
            conv_ids = list(room_index.get("conversation_ids", []))
        for conv_id in conv_ids:
            conv_index = await self._store.get(f"conv:{conv_id}:index")
            turn_ids: list[str] = []
            if isinstance(conv_index, dict):
                turn_ids = list(conv_index.get("turn_ids", []))
            for turn_id in turn_ids:
                await self._store.delete(f"conv:{conv_id}:turn:{turn_id}")
                await self._store.delete(f"feedback:{conv_id}:{turn_id}")
            await self._store.delete(f"conv:{conv_id}:index")
        await self._store.delete(f"room:{room_id}:conversations")

        # 3. Finally remove the config itself.
        await self._store.delete(f"room:{room_id}:config")


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _example_content_changed(
    raw_examples: list, new_examples: list[ExampleSQL]
) -> bool:
    """Detect changes beyond simple id-set equality (e.g. the SQL was edited
    but the id was preserved)."""
    by_id = {ex.id: ex for ex in new_examples}
    for raw in raw_examples:
        if not isinstance(raw, dict):
            continue
        rid = raw.get("id")
        if rid not in by_id:
            return True
        if (
            raw.get("question") != by_id[rid].question
            or raw.get("sql") != by_id[rid].sql
        ):
            return True
    return False


def _turn_from_dict(raw: dict) -> ConversationTurn:
    """Reconstruct a ConversationTurn from a JSON-deserialized dict.

    Nested QueryResult/VizResult are reconstructed too. EXT-1
    (`synthesized_answer`) and EXT-11 (`hypothesis_result`) payloads are
    intentionally NOT rehydrated here — MVP history replay does not read
    them, and reconstructing the dataclasses faithfully would require
    cross-version compatibility code that isn't needed yet. Extend this
    helper when those extensions land.
    """
    return ConversationTurn(
        room_id=raw.get("room_id", ""),
        conversation_id=raw.get("conversation_id", ""),
        turn_id=raw.get("turn_id", ""),
        question=raw.get("question", ""),
        sql=raw.get("sql"),
        query_result=_query_result_from_dict(raw.get("query_result")),
        viz=_viz_from_dict(raw.get("viz")),
        clarification_question=raw.get("clarification_question"),
        error=raw.get("error"),
        duration_ms=raw.get("duration_ms", 0),
        feedback_signal=raw.get("feedback_signal"),
    )


def _query_result_from_dict(raw: Any) -> QueryResult | None:
    if not isinstance(raw, dict):
        return None
    return QueryResult(
        columns=list(raw.get("columns", [])),
        rows=list(raw.get("rows", [])),
        row_count=int(raw.get("row_count", 0)),
        truncated=bool(raw.get("truncated", False)),
        duration_ms=int(raw.get("duration_ms", 0)),
    )


def _viz_from_dict(raw: Any) -> VizResult | None:
    if not isinstance(raw, dict):
        return None
    return VizResult(
        chart_type=raw.get("chart_type", "table"),
        vega_lite_spec=dict(raw.get("vega_lite_spec") or {}),
        summary=raw.get("summary", ""),
    )


_CAUSAL_QUESTION_MARKERS = ("why", "what caused", "what led to", "what drove")


def _is_causal_question(question: str) -> bool:
    """Heuristic for "this question implies causation". Used as EXT-11
    gate #3 (along with hypothesis_mode_enabled and multi-step plan).
    Cheap substring check on a lowercased question — same marker set
    used by SynthesisAgent's causal_hint logic, so the two layers stay
    coherent (any question that pushes SynthesisAgent to low confidence
    is the same question that gates HypothesisAgent on)."""
    q = question.lower()
    return any(marker in q for marker in _CAUSAL_QUESTION_MARKERS)
