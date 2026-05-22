---
tags: [layer/orchestration]
status: stable
depends_on: [agents, providers, data_models]
---

# Room engine

## In this system

**Linked from:** [[README]], [[api]], [[feedback]]
**Links to:** [[agents]], [[providers]], [[data_models]], [[knowledge_store]]
**Layer:** orchestration

---

## What this is

The top-level pipeline orchestrator. `RoomEngine` is the single entry point for all conversation requests. It loads room config, builds context, calls agents in sequence, persists results, and returns a `ConversationTurn`.

The [[api]] layer calls `RoomEngine`. Nothing else does. All agent coordination, config loading, history management, and result persistence happen here.

---

## RoomEngine

### Construction

```python
class RoomEngine:
    def __init__(
        self,
        llm: LLMProvider,
        catalog: CatalogProvider,
        metadata_providers: list[MetadataProvider],  # ordered stack; RoomConfigMetadataProvider added automatically
        query: QueryProvider,
        vector: VectorProvider,
        store: StoreProvider,
        *,
        mcp_providers: dict[str, MCPProvider] | None = None,
        # EXT-5: registry of MCP providers keyed by URL. Authorization is
        # per-room via RoomConfig.mcp_servers — being in this registry is
        # necessary but not sufficient. Empty/None = MCP wholly disabled.
        history_window: int = 10,
        intent_threshold: float = 0.7,
        sql_max_retries: int = 3,
        query_row_limit: int = 10_000,
    ): ...
```

`RoomEngine` does not hold a `RoomConfig` at construction time. Config is loaded fresh at the start of every request. This satisfies the [[README]] key rule: *RoomConfig is the source of truth; do not cache it beyond a single request.*

### `chat()` — blocking pipeline

```python
async def chat(
    self,
    room_id: str,
    conversation_id: str,
    question: str,
    user_token: str | None = None,   # EXT-6: pass-through user credential; None = use service account
) -> ConversationTurn:
```

**Full pipeline (when all extensions are active):**

```
1. Load RoomConfig
   store.get("room:{room_id}:config") → deserialize → RoomConfig
   Raise RoomNotFoundError if missing.

2. Load conversation history
   store.get("conv:{conversation_id}:index") → list of turn_ids (empty list if new conversation)
   store.get() each turn → deserialize → list[ConversationTurn]
   Trim to last HISTORY_WINDOW turns.

3. Build ContextPackage
   ContextBuilder.build(question, config, history)
   → ContextPackage (see [[knowledge_store]])

3a. MCP context resolution (EXT-5, when mcp_servers configured)
   MCPResolver.resolve(question, config.mcp_servers) → list[str]
   context.mcp_context = resolved_terms

4. Run IntentAgent
   IntentAgent.run(question, context) → IntentResult

5a. If intent == "out_of_scope":
    turn = ConversationTurn(room_id=room_id, error="This question is outside the scope of this room.")
    → persist turn → return turn

5b. If intent == "clarify_needed" or confidence < threshold:
    ClarifyResult = ClarifyAgent.run(question, context, intent)
    turn = ConversationTurn(room_id=room_id, clarification_question=ClarifyResult.question)
    → persist turn → return turn

5c. If intent == "sql_query":
    a. PlanningAgent.plan(question, context) → ReasoningPlan (EXT-1)
       Single-step plans are returned for simple questions — no overhead.

    b. For each step in plan.steps (in dependency order):
         SQLResult = SQLAgent.run(step.description, context, intent, user_token=user_token)
         If SQLResult.is_valid == False:
           turn = ConversationTurn(room_id=room_id, error=SQLResult.error)
           → persist turn → return turn
         step.sql = SQLResult.sql
         step.result = query.execute(SQLResult.sql, user_token=user_token)

    c. SynthesisAgent.synthesize(question, plan, results, context) → SynthesizedAnswer (EXT-7)

    d. [Optional] HypothesisAgent.run(...) → HypothesisResult (EXT-11)
       Only when: hypothesis_mode_enabled=True AND len(plan.steps) > 1
       AND _is_causal_question(question)

    e. VizAgent.run(question, step_1_result, context) → VizResult

    f. Assemble turn:
       turn = ConversationTurn(
           room_id=room_id,
           sql=step_1.sql,
           query_result=step_1.result,
           viz=VizResult,
           synthesized_answer=SynthesizedAnswer (always for multi-step; medium/low only for single-step),
           hypothesis_result=HypothesisResult (if generated),
       )
       → persist turn → return turn

6. Persist turn (see invariants below)
```

### `stream_chat()` — streaming pipeline

```python
async def stream_chat(
    self,
    room_id: str,
    conversation_id: str,
    question: str,
    user_token: str | None = None,   # EXT-6: pass-through user credential
) -> AsyncIterator[dict]:
```

Yields SSE event dicts at each pipeline stage:

```python
{"type": "status",  "text": "Loading room config..."}
{"type": "status",  "text": "Building context..."}
{"type": "mcp_context", "entries": [...]}          # EXT-5, only when context.mcp_context populated
{"type": "status",  "text": "Classifying question..."}
{"type": "status",  "text": "Planning..."}         # EXT-1
{"type": "plan",    "steps": [...], "synthesis_instruction": "..."}
                                                    # EXT-1, only for multi-step plans
{"type": "sql",     "sql": "SELECT ..."}           # primary step (step_1) SQL
{"type": "result",  "columns": [...], "rows": [...], "truncated": bool}
                                                    # primary step result
{"type": "steps",   "results": [{"step_id":"step_1","sql":"...","columns":[...],"row_count":N}, ...]}
                                                    # EXT-1, only for multi-step plans — summary of every step
{"type": "viz",     "spec": {...}, "summary": "..."}
{"type": "synthesis", "answer": "...", "data_supports": [...], "data_does_not_support": [...],
                      "would_need": [...], "confidence": "high|medium|low",
                      "confidence_rationale": "..."}
                                                    # EXT-7, only when SynthesizedAnswer is attached to the turn
                                                    # (always for multi-step; medium/low only for single-step)
{"type": "hypotheses", "disclaimer": "...", "confidence": "low",
                       "hypotheses": [{"statement":"...","supporting_patterns":[...],
                                       "contradicting_patterns":[...],"testability":"...",
                                       "suggested_test":"...","domain_knowledge_used":[...]}, ...]}
                                                    # EXT-11, only when all three gates pass
{"type": "done",    "turn_id": "..."}

# Error paths:
{"type": "clarify", "question": "Can you clarify..."}
{"type": "error",   "message": "..."}
```

**Event sequence rules:**
- `plan` precedes `sql` when emitted (multi-step plans announce themselves before executing).
- `steps` follows the primary `result` (multi-step summary appears after step_1's result is in flight).
- `synthesis` precedes `done` when emitted; `hypotheses` precedes `done` when emitted.
- Single-step + high-confidence questions emit no `plan`, `steps`, `synthesis`, or `hypotheses` events — the stream is functionally identical to the pre-EXT-1 sequence (the user-emphasized regression invariant).

The streaming path runs the same agents as `chat()`. The difference is that it yields intermediate events rather than blocking until completion. SQL generation streams token-by-token using `llm.stream()`.

### Invariants

These MUST hold for every call to `chat()` or `stream_chat()`:

1. `query.validate()` MUST be called before `query.execute()` — always, without exception. This invariant is satisfied by `SQLAgent`'s self-correction loop, which validates every SQL attempt before returning `SQLResult(is_valid=True)`. `RoomEngine` MUST NOT call `validate()` a second time — the guarantee is that no SQL reaches `query.execute()` without having passed `validate()` inside the agent. If `SQLResult.is_valid == False`, `execute()` is never called.
2. A `ConversationTurn` MUST be persisted before returning, even on error paths
3. A turn MUST have exactly one of: `sql`, `clarification_question`, or `error` set — never more than one, never none
4. `RoomConfig` MUST be loaded fresh — not from an instance variable

---

## RoomManager

Handles room lifecycle (create, update, delete). Separate from `RoomEngine` which handles conversations.

```python
class RoomManager:
    def __init__(self, store: StoreProvider, vector: VectorProvider, llm: LLMProvider): ...

    async def create(self, config: RoomConfig) -> str:
        """
        1. Validate RoomConfig
        2. store.put("room:{config.room_id}:config", asdict(config))
        3. ExampleIndexer.index(config)  ← embed and index all examples
        4. Return room_id
        """

    async def update(self, room_id: str, partial: dict) -> RoomConfig:
        """
        1. Load current config
        2. Merge partial dict into config (only supplied keys replaced)
        3. store.put(...)
        4. If examples changed: re-index with ExampleIndexer
        5. Return updated RoomConfig
        """

    async def delete(self, room_id: str) -> None:
        """
        1. store.delete("room:{room_id}:config")
        2. Delete all vector entries for this room_id
        3. Delete all conversation turns for this room (list_keys + delete each)
        """

    async def get(self, room_id: str) -> RoomConfig:
        """Load and return RoomConfig. Raise RoomNotFoundError if missing."""
```

---

## Error types

```python
class RoomEngineError(Exception): ...
class RoomNotFoundError(RoomEngineError): ...
class PipelineError(RoomEngineError): ...
class _SQLStepFailure(PipelineError): ...   # internal — mid-plan SQL failure, caught at pipeline boundary
class SynthesisError(PipelineError): ...    # EXT-7: causal language detected in synthesis output
class PlanningError(PipelineError): ...     # EXT-1: unparseable planning response
class HypothesisError(PipelineError): ...   # EXT-11: causal language detected in hypothesis output
```

Provider errors bubble up as `PipelineError` with the original exception attached as `__cause__`. `SynthesisError` and `HypothesisError` are caught by `RoomEngine` and converted to error turns — they never surface to the API caller as unhandled exceptions.

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | `chat()` with a valid question | MUST return a `ConversationTurn` with `sql` and `viz` set |
| 2 | `chat()` with an out-of-scope question | MUST return a `ConversationTurn` with `error` set, not raise |
| 3 | `chat()` with an ambiguous question | MUST return `ConversationTurn` with `clarification_question` set |
| 4 | `chat()` called twice in same room | MUST include first turn in context on second call |
| 5 | `chat()` on a nonexistent room_id | MUST raise `RoomNotFoundError` |
| 6 | `chat()` when SQL agent fails all retries | MUST persist the error turn and return it |
| 7 | `stream_chat()` happy path | MUST yield `status`, `sql`, `result`, `viz`, `done` events in order |
| 8 | `stream_chat()` error path | MUST yield an `error` event, not raise |
| 9 | Any `chat()` call | MUST persist a `ConversationTurn` to store |
| 10 | Any `chat()` call | MUST call `query.validate()` before `query.execute()` |
| 11 | `RoomManager.update()` with changed examples | MUST call `ExampleIndexer.index()` |
| 12 | `RoomManager.update()` with no examples change | MUST NOT call `ExampleIndexer.index()` |
| 13 | `RoomManager.delete()` | MUST remove config, vector entries, AND conversation history |
| 14 | `chat()` with one-step plan | MUST produce a turn whose shape matches the pre-EXT-1 single-query path — `sql`/`query_result`/`viz` from the single step; `synthesized_answer=None` when confidence is `high` |
| 15 | `chat()` with multi-step plan | MUST execute steps in declared order and attach `synthesized_answer` regardless of confidence |
| 16 | `chat()` mid-plan SQL failure | MUST produce an error turn referencing the failed `step_id`, not partially ship preceding steps |
| 17 | `chat()` with `RoomConfig.mcp_servers=[]` | MUST NOT call any registered `MCPProvider` (zero-overhead regression test) |
| 18 | `chat()` with `RoomConfig.mcp_servers=[url]` and registered provider | MUST inject MCP result into the agent prompts via `context.mcp_context` |
| 19 | `chat()` with `hypothesis_mode_enabled=False` | MUST NOT call `HypothesisAgent` regardless of question phrasing or plan shape |
| 20 | `chat()` with `hypothesis_mode_enabled=True` + single-step plan | MUST NOT call `HypothesisAgent` (multi-step evidence required) |
| 21 | `chat()` with `hypothesis_mode_enabled=True` + non-causal question | MUST NOT call `HypothesisAgent` (causal-question gate) |
| 22 | `chat()` with all three EXT-11 gates passing | MUST attach `HypothesisResult` to the turn; agent failure (e.g. causal-language violation) MUST log + return a turn with `hypothesis_result=None` rather than crashing the turn |
