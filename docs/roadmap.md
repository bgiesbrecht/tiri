---
tags: [roadmap]
status: living
depends_on: []
---

# Roadmap — validated, not yet designed

## In this system

**Linked from:** [[README]]
**Links to:** [[extensions]], [[vision]], [[data_models]], [[metadata]], [[providers]]
**Layer:** reference

---

## What this is

Capabilities that have been validated by real customer scenarios but are not yet
designed to the implementation level required for [[extensions]]. Each entry
records the motivating use case, the gap it closes, and the design direction
agreed on. This prevents the reasoning from being lost when the time comes to
implement.

Nothing here is in scope for the current build. When a capability is ready to
design, it moves to [[extensions]] with a full interface spec and test cases.

---

## R1 — Oracle (and other external catalog) metadata

**Motivated by:** A customer running Databricks with Lakehouse Federation or
JDBC external tables over Oracle. Unity Catalog knows the physical schema of
the federated tables but not the Oracle-side semantics — column descriptions,
table purposes, foreign key relationships, view logic, and business vocabulary
that the Oracle DBA has maintained for years in the Oracle data dictionary.
Genie cannot reach this metadata. Tiri's metadata stack can.

**The gap:** The existing `MetadataProvider` interface and stack design already
support this — there is no architectural gap, only a missing implementation.
`OracleMetadataProvider` would read from Oracle's data dictionary via JDBC:

- `ALL_TAB_COMMENTS` → `TableMeta.description`
- `ALL_COL_COMMENTS` → `ColumnMeta.description`
- `ALL_CONS_COLUMNS` + `ALL_CONSTRAINTS` → `ColumnMeta.is_primary_key`,
  `is_foreign_key`, `foreign_key_table`, `foreign_key_column`
- `ALL_VIEWS.text` → parsed to populate `TableMeta.grain` and
  `TableMeta.default_filter` (the view SQL reveals what the view filters and
  how it joins)

**Design direction:**
- New `MetadataProvider` implementation: `OracleMetadataProvider`
- Configured in `tiri.toml` as `type = "oracle"` with `jdbc_url`, `jdbc_user`,
  `jdbc_pass`, `schemas: list[str]`
- Slots into the metadata stack between UC annotations and room-level YAML
  overrides — Oracle knows more than UC about Oracle objects, but the room
  author's YAML should still win
- Add `OracleMetadataProviderError` to the error hierarchy in [[providers]]
- The same pattern applies to other external catalogs: Hive Metastore, AWS
  Glue, SQL Server, PostgreSQL `information_schema`. Consider a generic
  `JdbcMetadataProvider` that parameterizes the query templates rather than
  separate classes per database.

**Blocking dependency:** None — the `MetadataProvider` interface is in place.
Can be implemented independently of any extension.

---

## R2 — Query performance tracking

**Motivated by:** The same Oracle customer wanting to understand which query
patterns are expensive on their warehouse so the planning agent can make
informed decisions — prefer approximate queries, warn about joins that fan out,
suggest pre-aggregated alternatives.

**The gap:** `ConversationTurn` stores `duration_ms` but nothing more. The
Databricks SQL Statement Execution API returns richer execution statistics
that are currently discarded.

**Design direction:**

Add `QueryPerformance` to [[data_models]]:

```python
@dataclass
class QueryPerformance:
    execution_ms: int
    bytes_scanned: int | None      # from warehouse execution stats
    rows_returned: int
    warehouse_queue_ms: int | None # time spent waiting for warehouse slot
    from_cache: bool               # whether result was served from cache
```

Add `query_performance: QueryPerformance | None = None` to `QueryResult`.
`DatabricksQueryProvider` populates it from the Statement Execution API
response. All other `QueryProvider` implementations return `None` (acceptable
— this is an optional enrichment, not a contract requirement).

**Blocking dependency:** None. Purely additive to `QueryResult` and
`ConversationTurn`. Can be implemented at Step 5 (Databricks providers)
or any time after.

---

## R3 — Supervised reasoning

**Motivated by:** Domain experts who understand the business data (Oracle DBAs,
finance analysts, compliance officers) want to inspect and guide the system's
reasoning plan before it executes — not just after. Particularly valuable when
the data has nuances the LLM may not correctly infer: complex view logic,
fiscal calendar definitions, exception populations that should be excluded.

Genie does not expose a reasoning plan for inspection — the pipeline is managed by the platform. R3 adds a human-in-the-loop checkpoint between planning and execution in Tiri.

**The gap:** EXT-1 adds multi-query reasoning (`PlanningAgent` →
`ReasoningPlan` → `[SQLAgent × N]` → `SynthesisAgent`). R3 adds a
human-in-the-loop checkpoint between planning and execution. The supervisor
sees the proposed plan, can approve it as-is, amend individual steps, add
steps the planner missed, or reject and provide guidance for replanning.

**Design direction:**

New type in [[data_models]]:

```python
@dataclass
class ReasoningCheckpoint:
    checkpoint_id: str
    conversation_id: str
    room_id: str
    question: str
    proposed_plan: ReasoningPlan
    suggested_amendments: list[str]
    # Populated from history: "last time a churn question was asked,
    # the supervisor added a cohort breakdown step"
    status: str  # "pending" | "approved" | "amended" | "rejected"
    supervisor_notes: str = ""
    amended_plan: ReasoningPlan | None = None
    created_at: str = ""
    resolved_at: str = ""
```

New `RoomConfig` field: `supervision_enabled: bool = False`

Pipeline change (EXT-1 must be in place):
```
PlanningAgent → [checkpoint if supervision_enabled] → SQLAgent × N
```

When supervision is enabled, `stream_chat()` yields a `checkpoint` SSE
event instead of proceeding to SQL execution. The conversation is paused.
A new API endpoint resolves it:

```
POST /rooms/{id}/conversations/{cid}/checkpoints/{checkpoint_id}/resolve
body: {"action": "approve" | "amend" | "reject",
       "amended_plan": ReasoningPlan | null,
       "notes": "..."}
```

**Suggested amendments** (the history-awareness part): when the checkpoint
is created, the system scans prior `ReasoningCheckpoint` records for the same
room where `status == "amended"` and the question vector is similar. The
amendments made by supervisors in those cases are surfaced as suggestions —
"in 3 prior similar questions, supervisors added a step to break down by
customer segment." This is the lightweight learning loop that makes supervision
progressively less work as the system builds a history.

**Blocking dependency:** EXT-1 (needs `ReasoningPlan`), SSE streaming
infrastructure (Step 9/10), and R2 is a natural companion (performance data
informs which plans to flag for supervision).

---

## R4 — Performance-guided planning

**Motivated by:** Same customer as R2/R3. As query history accumulates,
the `PlanningAgent` should be able to factor in "this join pattern has
historically taken 45+ seconds on this warehouse" when deciding step order,
whether to suggest approximate alternatives, or whether to warn the user
about expected latency.

**Design direction:**

The `PlanningAgent` (EXT-1) receives a `performance_context: str` field in
its prompt — a brief summary of relevant historical performance patterns for
this room, derived from prior `ConversationTurn` records with `QueryPerformance`
data attached.

The `Proposer` (in [[feedback]]) is extended to consider performance signals
alongside feedback signals when nominating examples: queries that executed
quickly, returned actionable results, and were not amended by supervisors are
stronger candidates than queries that only received a thumbs-up.

No new types needed beyond R2's `QueryPerformance`.

**Blocking dependency:** R2 (needs performance data), EXT-1 (needs
`PlanningAgent`).

---

## R5 — Migration tooling: Genie Space → Tiri room

**Motivated by:** Any customer with existing Genie Spaces who wants to adopt
Tiri without re-authoring their room configurations from scratch.

**The gap:** The translation from Genie wire format to `RoomConfig` is
mechanical and well-understood (documented in [[concept_map]]). It should
be a first-class CLI command rather than a one-off script.

**Design direction:**

New CLI command:

```bash
python -m tiri.cli import-genie --space-id <id> --output <path/to/config.json>
```

Reads the Genie Space via `GET /api/2.0/genie/spaces/{id}?include_serialized_space=true`,
translates to `RoomConfig` JSON:

| Genie field | RoomConfig field | Translation |
|---|---|---|
| `instructions.text_instructions[0].content` | `text_instruction` | Unwrap list |
| `instructions.example_question_sqls` | `examples` | Unwrap list fields |
| `instructions.join_specs` | `joins` | Flatten `left.identifier`→`left_table`, strip `FROM_RELATIONSHIP_TYPE_` prefix |
| `instructions.sql_snippets.filters` | `sql_filters` | Add `kind="filter"`, unwrap list fields |
| `instructions.sql_snippets.expressions` | `sql_expressions` | Add `kind="expression"`, unwrap list fields |
| `config.sample_questions` | `sample_questions` | Unwrap list |
| `data_sources[].table_ref` | `tables` | Extract FQN |

Writes the output JSON to `--output` path. The user reviews it, adds
`warehouse_id`, optionally enriches with metrics and column overrides, then
runs `python -m tiri.cli load-room`.

**Blocking dependency:** None beyond the CLI infrastructure (Step 3 config.py
is already done). Could be implemented immediately.

---

---

## R6 — Per-user inference audit trail

**Motivated by:** EXT-6 ensures SQL executes with the user's own credentials,
so Unity Catalog enforces data access correctly. But LLM calls — intent
classification, SQL generation, synthesis — currently always use the service
credential. Organizations with strict audit requirements may need to know not
just what data was accessed per user, but what LLM calls were made on their
behalf.

**The gap:** `auth.py` extracts the user's Bearer token and passes it to
`QueryProvider`. It does not pass it to `LLMProvider`. The LLM calls carry
no per-user identity signal.

**Pattern observed in the ecosystem:** The `x-forwarded-access-token` header
injected by Databricks Apps — the same header now supported by Tiri's `auth.py`
— can be used to obtain a `WorkspaceClient` authenticated as the calling user,
which can then be used to instantiate a `ChatDatabricks` model endpoint call
carrying that user's identity. This pattern exists in at least one community
implementation and is consistent with how other Databricks services handle
per-request identity in Apps deployments.

**Design direction:**

Add `user_token: str | None = None` to `LLMProvider.complete()` and
`LLMProvider.stream()` — same pattern as `QueryProvider.execute()`. When
provided, `DatabricksLLMProvider` uses it to authenticate the Model Serving
call rather than the service credential. The token is forwarded from
`RoomEngine.chat()` alongside the existing `user_token` for SQL execution.

`RouterLLMProvider` passes `user_token` through to the backend's
`complete()`/`stream()` calls. Single-backend implementations that don't
support per-user inference (OpenAI, Anthropic, Ollama) accept and ignore
the parameter — same pattern as the `task=` parameter.

This is additive. No existing behavior changes when `user_token=None`.

**What it enables:**
- Databricks Model Serving audit logs show the calling user's identity,
  not the service principal
- Consistent identity across data access and inference — the same user
  credential used for SQL is also used for LLM calls
- Required for deployments where compliance teams audit all LLM activity
  per user

**Blocking dependency:** None architecturally. Requires Databricks Model
Serving to support per-request user token auth on the endpoint (available
in Databricks Apps deployments via the forwarded token). Not all Model
Serving configurations support this — verify before implementing.

---

## Design principles for this list

When a roadmap item is ready to graduate to [[extensions]]:

1. It must have a complete interface spec — every new type, every method
   signature, every new field on existing types
2. It must have test cases written before the implementation (the MUST/SHOULD
   table format from other extension docs)
3. Its blocking dependencies must be resolved or explicitly scheduled
4. It must have a clear answer to: "what does a room author configure to enable
   this, and what do they see differently in responses?"

Items that cannot answer question 4 are not ready to graduate.
