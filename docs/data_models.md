---
tags: [layer/foundation]
status: stable
depends_on: []
---

# Data models

## In this system

**Linked from:** [[README]]
**Links to:** *(none — this is the foundation layer)*
**Implemented by:** all components
**Layer:** foundation

---

## What this is

The single source of truth for every shared dataclass in the system. No component defines its own data structures — they import from here. This document has no dependencies within the system, which makes it the stable base everything else builds on.

If a data structure appears in more than one component, it belongs here. If a structure is only ever used internally within one component, it may live there — but when in doubt, define it here.

---

## ColumnOverride

Room-specific metadata override for a single column. Applied by `RoomConfigMetadataProvider` — always last in the metadata stack, so these always win. Use for context that is specific to how a room interprets a column, not for global metadata that should live in YAML or the catalog.

```python
@dataclass
class ColumnOverride:
    table: str             # fully-qualified table name
    column: str            # column name
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    value_description: str = ""
    default_filter: str = ""   # SQL filter for this column, applied in this room only
```

---

## RoomConfig

The complete configuration for one Room. Persisted via `StoreProvider` under key `room:{room_id}:config`. Loaded at the start of every request by [[room_engine]].

```python
@dataclass
class RoomConfig:
    room_id: str
    title: str
    tables: list[str]          # fully-qualified: catalog.schema.table or wildcard (EXT-2)
    warehouse_id: str
    text_instruction: str = ""
    examples: list[ExampleSQL] = field(default_factory=list)
    joins: list[JoinSpec] = field(default_factory=list)
    sql_filters: list[SqlSnippet] = field(default_factory=list)
    sql_expressions: list[SqlSnippet] = field(default_factory=list)
    sql_measures: list[SqlSnippet] = field(default_factory=list)
    # Simple aggregation fragments. For named business concepts with declared
    # dimensions and filters, prefer metrics (below).
    metrics: list[Metric] = field(default_factory=list)
    # Named business concepts — richer than sql_measures. Use for any metric
    # that has a business name, can be sliced by known dimensions, or requires
    # filters to be applied consistently. Examples: Revenue, Churn Rate, NPS.
    sample_questions: list[str] = field(default_factory=list)
    benchmarks: list[Benchmark] = field(default_factory=list)
    column_overrides: list[ColumnOverride] = field(default_factory=list)
    default_filters: list[str] = field(default_factory=list)
    # Room-level SQL filters applied to every query in this room, regardless
    # of which tables are involved. Applied as WHERE clause additions before
    # the query reaches the user. Use for cross-cutting concerns that
    # table-level default_filter cannot express:
    #   "tenant_id = '${TENANT_ID}'"    ← multi-tenant scoping
    #   "environment != 'test'"         ← exclude test data globally
    #   "active = true"                 ← soft-delete convention
    # These are injected into the SQL generation prompt as mandatory constraints.
    # The SQLAgent MUST include them in every generated query.
    # ${VAR} substitution applies — same rules as room config loading.
    max_tables_per_query: int = 10
    # EXT-2: cap on dynamic table selection when tables is a wildcard pattern

    hypothesis_mode_enabled: bool = False
    # EXT-11: off by default — room author opts in explicitly.
    # Rooms serving high-stakes non-technical audiences should leave this False.
    # When True: HypothesisAgent runs after SynthesisAgent on causal/why questions.

    domain_knowledge: list[str] = field(default_factory=list)
    # EXT-11: domain axioms provided by the room author for hypothesis generation.
    # Auditable, version-controlled, shown to users alongside any hypothesis that uses them.
    # Examples:
    #   "SMB customers in this business are more price-sensitive than enterprise"
    #   "Q4 revenue spikes are seasonal — do not treat as anomalies"
    #   "Churn typically lags contract value changes by 1-2 quarters"

    mcp_servers: list[str] = field(default_factory=list)
    # EXT-5: URLs of external MCP servers this room is permitted to call
    # during the reasoning pipeline. Empty list disables MCP tool
    # consumption entirely for this room (zero regression vs. pre-EXT-5).
    # This is a security boundary — the room author explicitly opts each
    # server in. URLs listed here but absent from the engine's MCP provider
    # registry are misconfigurations (logged as WARNING by `MCPResolver`),
    # not security violations.
    # Examples:
    #   ["https://confluence.mycompany.com/mcp",
    #    "https://glean.mycompany.com/mcp"]
```

**Serialization:** `RoomConfig` serializes to/from JSON via `dataclasses.asdict()` + `json.dumps()`. The `StoreProvider` stores the JSON string. No ORM, no schema migration — just JSON.

**Deserialization:** use `RoomConfig.from_dict(d: dict)` which rehydrates nested dataclasses (`ExampleSQL`, `JoinSpec`, `SqlSnippet`, `Metric`, `Benchmark`, `ColumnOverride`). This is the inverse of `dataclasses.asdict()`. The round-trip `RoomConfig.from_dict(json.loads(json.dumps(asdict(config))))` MUST be equal to the original under `asdict`.

**Validation rules (MUST):**
- `room_id` MUST be a non-empty string, URL-safe (no spaces or slashes)
- `tables` MUST contain at least one fully-qualified table name
- `warehouse_id` MUST be non-empty
- All `ExampleSQL.id` values within a RoomConfig MUST be unique
- `default_filters` entries MUST be SQL fragments, not full SELECT statements

---

## ExampleSQL

A worked question/SQL pair used as a few-shot example by [[agents]]. Also indexed into the vector store by [[knowledge_store]].

```python
@dataclass
class ExampleSQL:
    question: str              # natural language question
    sql: str                   # the correct SQL that answers it
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
```

**Notes:**
- `id` is stable across updates — preserve existing ids when upserting so vector store entries remain valid
- `question` is what gets embedded for similarity retrieval
- `sql` is what gets injected into the prompt as the answer

---

## JoinSpec

Teaches the [[agents]] how to join two tables. Without this, the SQL agent may guess wrong column names or join direction.

```python
@dataclass
class JoinSpec:
    left_table: str            # fully-qualified: catalog.schema.table
    left_alias: str            # short alias used in join_on SQL
    right_table: str           # fully-qualified: catalog.schema.table
    right_alias: str           # short alias used in join_on SQL
    join_on: str               # the ON clause SQL using aliases
    relationship_type: str     # see enum below
    instruction: str = ""      # optional: when/why to use this join
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
```

**`relationship_type` valid values:**

| Value | Meaning |
|---|---|
| `MANY_TO_ONE` | Many rows on the left join to one row on the right. Most common — e.g. many orders to one customer. |
| `ONE_TO_MANY` | One row on the left joins to many rows on the right. e.g. one order to many line items. |
| `ONE_TO_ONE` | Each row on the left joins to exactly one row on the right. |
| `MANY_TO_MANY` | Many rows on both sides. Requires a junction table. |

**Notes:**
- `join_on` uses the aliases, not the full table names: `orders.customer_id = customers.id`
- `relationship_type` affects how the SQL agent reasons about cardinality. `MANY_TO_ONE` is the most common.
- These values are Tiri-native. They are NOT the Genie wire format (`FROM_RELATIONSHIP_TYPE_*`). The `update_genie_space.py` loader translates when writing to Genie's API — Tiri's internal representation is always the short form above.
- `id` is stable across updates — preserve when upserting to avoid breaking references.

---

## SqlSnippet

A reusable SQL fragment. Used for filters (WHERE clause fragments), expressions (derived column expressions), and measures (aggregation expressions).

```python
@dataclass
class SqlSnippet:
    display_name: str          # human-readable name shown to user
    sql: str                   # the SQL fragment
    kind: str                  # "filter" | "expression" | "measure"
    instruction: str = ""      # when/how to apply this snippet
    synonyms: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
```

**Kind semantics:**
- `filter` — a WHERE clause fragment: `orders.amount > 1000`
- `expression` — a derived column: `DATE_TRUNC('month', orders.order_date)`
- `measure` — an aggregation: `SUM(orders.amount)`. Prefer `Metric` (below) for named business concepts — `SqlSnippet(kind="measure")` is retained for simple aggregations that don't need dimension or filter declarations.

---

## Metric

A named business concept with a SQL definition, declared dimensions, and optional filters. The preferred way to define reusable aggregations in a room — richer than `SqlSnippet(kind="measure")` because it carries the business meaning alongside the SQL.

The distinction from `SqlSnippet`:
- `SqlSnippet(kind="measure")` — a raw SQL aggregation fragment. The agent uses it as a building block but has no structured understanding of what it means, what it can be sliced by, or what filters apply.
- `Metric` — a named business concept. The agent knows what it means, what dimensions it can be grouped by, and what filters always apply when it's used. "Revenue by Region" is composable from "Revenue" (a `Metric`) and "Region" (a dimension declared on that metric).

```python
@dataclass
class Metric:
    name: str                  # canonical identifier: "revenue", "churn_rate"
    display_name: str          # human-readable: "Net Revenue", "Churn Rate"
    sql: str                   # the aggregation SQL: "SUM(l_extendedprice * (1 - l_discount))"
    grain: str                 # "line item" | "order" | "customer" — what one row represents
    description: str = ""      # what this metric means in business terms
    synonyms: list[str] = field(default_factory=list)
    # e.g. ["sales", "net sales", "revenue after discount"]
    dimensions: list[str] = field(default_factory=list)
    # column or table names this metric can be grouped by
    # e.g. ["r_name", "c_mktsegment", "YEAR(l_shipdate)"]
    filters: list[str] = field(default_factory=list)
    # SQL fragments always applied when this metric is computed
    # e.g. ["l_linestatus = 'F'"] to count only shipped items
    unit: str = ""             # "USD" | "%" | "units" | "" — for display
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
```

**Notes:**
- `name` is the stable identifier used in prompts and by the SQL agent for resolution
- `sql` is the complete aggregation expression — not a column name, not a SELECT fragment
- `dimensions` are hints, not constraints — the agent uses them to decide what GROUP BY clauses are valid for this metric
- `filters` are applied in addition to any `RoomConfig.default_filters` when this metric is used
- For complex derived metrics (rolling averages, period-over-period calculations), use `sql` with a CTE fragment and document the pattern in `description`

---

## Benchmark

A stored question/expected-SQL pair used to evaluate room quality. Managed by [[feedback]].

```python
@dataclass
class Benchmark:
    question: str
    expected_sql: str
    expected_row_count: int | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    notes: str = ""
```

---

## ContextPackage

The assembled bundle passed to every agent before any LLM call. Built by [[knowledge_store]], consumed by [[agents]]. Never persisted — constructed fresh per request.

```python
@dataclass
class ContextPackage:
    room_id: str                           # the room this context belongs to
    table_schemas: dict[str, TableMeta]    # keyed by full table name
    joins: list[JoinSpec]
    sql_snippets: list[SqlSnippet]         # filters + expressions + measures (simple)
    metrics: list[Metric]                  # named business concepts with dimensions
    text_instruction: str
    default_filters: list[str]             # mandatory WHERE additions from RoomConfig
    retrieved_examples: list[ExampleSQL]   # top-k similar from vector store
    conversation_history: list[ConversationTurn]  # last N turns
    table_selection_method: str = "configured"   # EXT-2: see notes below
    mcp_context: list[str] = field(default_factory=list)
    # EXT-5: resolved tool results from external MCP servers — one entry per
    # successful call, formatted as "tool_name: <result text>". Empty when
    # the room declares no `mcp_servers` or every call failed. Populated by
    # `MCPResolver.resolve()` called from `RoomEngine.chat()` after
    # `ContextBuilder.build()`. Surfaced in IntentAgent / SQLAgent /
    # SynthesisAgent prompts as additional context.
    domain_knowledge: list[str] = field(default_factory=list)
    # EXT-11: mirrors `RoomConfig.domain_knowledge`. Copied by ContextBuilder.
    # Consumed by HypothesisAgent for grounding; hallucinated entries
    # (i.e. anything not in this list) are dropped from
    # `Hypothesis.domain_knowledge_used` with a WARNING.
```

`table_selection_method` is `"configured"` when every entry in `RoomConfig.tables` is an explicit FQN; `"dynamic_search"` when every entry was a wildcard expanded by `TableSelector`; `"hybrid"` when mixed. `IntentAgent` copies this value into `IntentResult.table_selection_method`.

`default_filters` carries `RoomConfig.default_filters` into agent context. SQLAgent injects them as mandatory constraints in the SQL-generation prompt; the agent must include all of them in every generated query.

---

## TableMeta / ColumnMeta

The fully-resolved metadata for a table and its columns, assembled by [[metadata]] after running the complete provider stack. Consumed by [[knowledge_store]] and [[agents]]. This is richer than what any single catalog source provides — it is the *curated* view of the table.

See [[metadata]] for how these fields are populated from multiple sources.

```python
@dataclass
class MetadataConflict:
    """Recorded when two providers supply different values for the same scalar field."""
    table: str             # fully-qualified table name
    column: str | None     # None for table-level conflicts
    field: str             # field name, e.g. "description"
    values: dict[str, str] # {provider_name: value}
    resolved_to: str       # name of the provider whose value was used

@dataclass
class ColumnMeta:
    # ── Physical (from CatalogProvider — always present) ──────────────────────
    name: str
    data_type: str

    # ── Descriptive (any metadata provider can contribute) ────────────────────
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    sample_values: list[str] = field(default_factory=list)
    value_description: str = ""    # e.g. "R=returned, A=accepted, N=neither"

    # ── Semantic ──────────────────────────────────────────────────────────────
    semantic_type: str = ""
    # Valid values: "date" | "currency" | "identifier" | "category" | "measure" | ""
    currency_code: str = ""        # if semantic_type == "currency"
    date_format: str = ""          # if semantic_type == "date" and non-standard

    # ── Structural hints ──────────────────────────────────────────────────────
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_key_table: str = ""    # fully-qualified target table
    foreign_key_column: str = ""

    # ── Behavioral hints for SQL generation ───────────────────────────────────
    is_high_cardinality: bool = False
    # True → suppress sample_values injection; warn SQLAgent to use GROUP BY with care
    exclude_from_select_star: bool = False
    # True → don't include in exploratory SELECT * suggestions

    # ── Provenance ────────────────────────────────────────────────────────────
    metadata_source: str = "catalog"
    # Last provider that touched this column: "catalog" | "uc_annotations" |
    # "yaml" | "delta_table" | "dbt" | "room_config" | ...

@dataclass
class TableMeta:
    # ── Physical ──────────────────────────────────────────────────────────────
    full_name: str             # catalog.schema.table
    columns: list[ColumnMeta] = field(default_factory=list)
    row_count: int | None = None

    # ── Descriptive ───────────────────────────────────────────────────────────
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    grain: str = ""
    # "one row per order", "one row per line item per supplier" — critical for
    # correct aggregation; injected into SQL generation prompt

    # ── Semantic ──────────────────────────────────────────────────────────────
    domain: str = ""
    # "sales" | "supply_chain" | "hr" — used by IntentAgent for routing hints
    freshness: str = ""
    # "real-time" | "daily" | "monthly" — helps agents caveat answers

    # ── Behavioral ────────────────────────────────────────────────────────────
    default_date_column: str = ""
    # Column to use when user says "this year" or "last month" without specifying
    default_filter: str = ""
    # SQL filter applied automatically unless user says otherwise
    # e.g. "customers.status = 'active'"
    recommended_joins: list[str] = field(default_factory=list)
    # Other tables frequently joined with this one (fully-qualified names)

    # ── Provenance and quality ─────────────────────────────────────────────────
    metadata_sources: list[str] = field(default_factory=list)
    # Names of all providers that contributed to this table's metadata
    conflicts: list[MetadataConflict] = field(default_factory=list)
    # Recorded when providers disagreed on a scalar field value
```

**Notes:**
- `TableMeta` objects are never persisted — reconstructed fresh per request by `MetadataFetcher`
- `conflicts` is not injected into agent prompts — it is exposed via management API for admin review
- The `metadata_source` field on `ColumnMeta` records the *last* provider to touch it; `TableMeta.metadata_sources` records *all* providers that contributed anything

---

## ConversationTurn

One complete exchange in a conversation. Persisted per turn by [[room_engine]] via `StoreProvider`. Also the response type of the API — see [[api]].

```python
@dataclass
class ConversationTurn:
    turn_id: str
    conversation_id: str
    room_id: str               # required for Proposer room scoping
    question: str
    sql: str | None
    query_result: QueryResult | None
    viz: VizResult | None
    clarification_question: str | None
    error: str | None
    duration_ms: int
    feedback_signal: str | None          # "up" | "down" | None
    synthesized_answer: "SynthesizedAnswer | None" = None  # EXT-1
    hypothesis_result: "HypothesisResult | None" = None    # EXT-11
```

**Mutual exclusion rule:** exactly one of `sql`, `clarification_question`, or `error` MUST be set. `synthesized_answer` and `hypothesis_result` are independent — they may accompany `sql` when EXT-1 and EXT-11 are active.

---

## QueryResult

Returned by `QueryProvider.execute()`. Consumed by [[agents]] (VizAgent) and [[api]].

```python
@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    row_count: int
    truncated: bool            # True if results were capped at QUERY_ROW_LIMIT
    duration_ms: int
```

---

## VizResult

Produced by VizAgent in [[agents]]. Consumed by [[room_engine]] and [[api]].

```python
@dataclass
class VizResult:
    chart_type: str            # "bar" | "line" | "scatter" | "table" | "counter"
    vega_lite_spec: dict       # complete Vega-Lite JSON spec
    summary: str               # one-sentence prose summary of the result
```

---

## LLMMessage / LLMResponse

Used by all agents and the `LLMProvider` interface. Defined here so both [[providers]] and [[agents]] share the same types.

```python
@dataclass
class LLMMessage:
    role: str        # "system" | "user" | "assistant"
    content: str

@dataclass
class LLMResponse:
    content: str
    usage: dict      # {"prompt_tokens": int, "completion_tokens": int}
    raw: Any         # provider-specific raw response object
```

---

## VectorMatch

Returned by `VectorProvider.query()`. Defined here so both [[providers]] and [[knowledge_store]] share the type.

```python
@dataclass
class VectorMatch:
    id: str
    score: float     # higher = more similar; range is implementation-defined
    payload: dict    # {"question": str, "sql": str, "room_id": str}
```

---

## Agent result types

Produced by agents in [[agents]], consumed by [[room_engine]]. Defined here so both components share the same types without circular imports.

```python
@dataclass
class IntentResult:
    intent: str                          # "sql_query" | "clarify_needed" | "out_of_scope"
    relevant_tables: list[str]           # subset of ContextPackage.table_schemas keys
    relevant_snippets: list[SqlSnippet]  # subset of ContextPackage.sql_snippets
    confidence: float                    # 0.0–1.0
    reasoning: str                       # one sentence, for logging
    table_selection_method: str = "configured"  # EXT-2: "configured" | "dynamic_search" | "hybrid"

@dataclass
class SQLResult:
    is_valid: bool
    attempts: int
    sql: str = ""              # empty string on error paths
    explanation: str = ""      # empty string on error paths
    error: str | None = None   # populated when is_valid=False

@dataclass
class ClarifyResult:
    question: str        # the follow-up question to present to the user
```

---

## Benchmark types

Produced and consumed by [[feedback]]. Defined here so [[api]] can reference them without importing from feedback.

```python
@dataclass
class BenchmarkResult:
    benchmark_id: str
    question: str
    expected_sql: str
    generated_sql: str | None
    sql_match: bool            # normalized exact match
    result_match: bool | None  # True if row counts match (when expected_row_count set)
    passed: bool               # True if sql_match or result_match
    error: str | None          # populated if pipeline failed entirely

@dataclass
class BenchmarkReport:
    room_id: str
    run_at: str                # ISO 8601 UTC timestamp
    total: int
    passed: int
    failed: int
    score: float               # passed / total
    results: list[BenchmarkResult]
```

---

## EXT-5 types (MCP tool consumption)

Added by [[extensions]] EXT-5. Consumed by [[knowledge_store]] `MCPResolver`
via the `MCPProvider` ABC in [[providers]].

```python
@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict        # JSON Schema for this tool's arguments

@dataclass
class MCPToolResult:
    tool_name: str
    content: str
    is_error: bool            # True when the tool ran but returned an error.
                              # Distinct from transport failures — those raise
                              # MCPProviderError (see [[providers]]).
```

`MCPToolResult.content` is the concatenation of every `{"type": "text", "text": ...}`
content block in the MCP server's response. Non-text content blocks (image,
binary) are ignored at this layer — MCP's text content is the only useful
shape for prompt injection.

---

## EXT-1 types (multi-query reasoning)

Added by [[extensions]] EXT-1. Defined here so [[room_engine]] and [[agents]] share the types when EXT-1 is implemented. Not used in the MVP pipeline.

```python
@dataclass
class ReasoningStep:
    step_id: str
    description: str             # "Calculate churn rate by month"
    sql: str | None              # populated after SQLAgent runs this step
    result: QueryResult | None   # populated after query executes
    depends_on: list[str]        # step_ids that must complete first

@dataclass
class ReasoningPlan:
    question: str
    steps: list[ReasoningStep]
    synthesis_instruction: str   # how to combine results into a final answer

@dataclass
class SynthesizedAnswer:
    answer: str
    data_supports: list[str]           # what the data directly shows
    data_does_not_support: list[str]   # what cannot be determined
    would_need: list[str]              # what additional data would be needed
    confidence: str                    # "high" | "medium" | "low"
    confidence_rationale: str          # one sentence
```

---

## EXT-11 types (hypothesis mode)

Added by [[extensions]] EXT-11. Defined here so [[room_engine]] and [[agents]] share the types when EXT-11 is implemented. Not used until EXT-1 is complete and hypothesis mode is enabled on a room.

```python
@dataclass
class Hypothesis:
    statement: str                      # "X may be contributing to Y" — NEVER "X caused Y"
    supporting_patterns: list[str]      # data patterns consistent with this hypothesis
    contradicting_patterns: list[str]   # data patterns that cut against it
    testability: str                    # "testable_in_room" | "requires_external_data" | "not_testable"
    suggested_test: str | None          # if testable_in_room: the analysis that would confirm/refute
    domain_knowledge_used: list[str]    # which RoomConfig.domain_knowledge entries were applied

@dataclass
class HypothesisResult:
    hypotheses: list[Hypothesis]
    confidence: str = "low"             # INVARIANT: always "low" — not configurable
    disclaimer: str = (
        "These are hypotheses derived from data patterns, not conclusions. "
        "Each should be independently verified before acting on it."
    )
    # disclaimer is mandatory and non-empty — enforced by __post_init__
```

**Invariants enforced by `__post_init__`:**
- `confidence` MUST be `"low"` — raise `ValueError` if any other value is set
- `disclaimer` MUST be non-empty — raise `ValueError` if empty or whitespace
- Every `Hypothesis` MUST have at least one `contradicting_pattern` — raise `ValueError` if empty

---

## Store key layout

All keys used with `StoreProvider`. Defined here as the canonical reference — every component that reads or writes the store MUST use these key patterns exactly.

```
room:{room_id}:config                    ← RoomConfig JSON
room:{room_id}:conversations             ← sorted list of conversation_ids (index)
conv:{conv_id}:turn:{turn_id}            ← ConversationTurn JSON
conv:{conv_id}:index                     ← sorted list of turn_ids for this conversation
feedback:{conv_id}:{turn_id}             ← {"signal": "up"|"down", "comment": "..."}
```

**Room→conversation index (`room:{room_id}:conversations`):** maintained by `RoomEngine` when a new conversation is created. Required by `RoomManager.delete()` to enumerate all conversations for a room, and by `Proposer` to scope feedback scans to a room without a full store scan.

**Conversation→turn index (`conv:{conv_id}:index`):** a JSON list of turn_ids in creation order, maintained by `RoomEngine`. Used to load history in correct order without sorting by key.

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | RoomConfig with missing `tables` | MUST raise `ValueError` on construction |
| 2 | RoomConfig serialized to JSON and back | MUST be identical to original |
| 3 | Two ExampleSQL with same `id` in one RoomConfig | MUST raise `ValueError` |
| 4 | SqlSnippet with `kind` not in valid set | MUST raise `ValueError` |
| 5 | ConversationTurn with both `sql` and `clarification_question` set | MUST raise `ValueError` — these are mutually exclusive |
| 6 | All dataclasses | MUST be importable with no side effects |
| 7 | All dataclasses | MUST serialize to JSON with no custom encoder |
| 8 | RoomConfig with `default_filters` | MUST include them in JSON round-trip |
