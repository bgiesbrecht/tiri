"""Tiri shared data models.

Single source of truth for every cross-component dataclass. No component
defines its own data structures — they all import from here.

See docs/data_models.md for the specification.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


# ────────────────────────────────────────────────────────────────────────────
# Room knowledge store — leaf types
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ColumnOverride:
    """Room-specific metadata override for a single column.

    Applied by RoomConfigMetadataProvider — always last in the metadata stack,
    so these always win. Use for context that is specific to how a room
    interprets a column, not for global metadata that should live in YAML
    or the catalog.
    """

    table: str
    column: str
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    value_description: str = ""
    default_filter: str = ""


@dataclass
class ExampleSQL:
    """A worked question/SQL pair used as a few-shot example."""

    question: str
    sql: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


_VALID_RELATIONSHIP_TYPES = frozenset(
    {"MANY_TO_ONE", "ONE_TO_MANY", "ONE_TO_ONE", "MANY_TO_MANY"}
)


@dataclass
class JoinSpec:
    """Teaches the SQL agent how to join two tables.

    These are Tiri-native short-form values, not Genie wire format
    (`FROM_RELATIONSHIP_TYPE_*`).
    """

    left_table: str
    left_alias: str
    right_table: str
    right_alias: str
    join_on: str
    relationship_type: str
    instruction: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


_VALID_SNIPPET_KINDS = frozenset({"filter", "expression", "measure"})


@dataclass
class SqlSnippet:
    """A reusable SQL fragment (filter, expression, or measure)."""

    display_name: str
    sql: str
    kind: str
    instruction: str = ""
    synonyms: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if self.kind not in _VALID_SNIPPET_KINDS:
            raise ValueError(
                f"SqlSnippet.kind must be one of {sorted(_VALID_SNIPPET_KINDS)}; "
                f"got {self.kind!r}"
            )


@dataclass
class Metric:
    """Named business concept — richer than SqlSnippet of kind 'measure'.

    Use for any metric that has a business name, can be sliced by known
    dimensions, or requires filters to be applied consistently.
    """

    name: str                  # canonical identifier: "revenue", "churn_rate"
    display_name: str          # human-readable: "Net Revenue"
    sql: str                   # aggregation SQL
    grain: str                 # "line item" | "order" | "customer"
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    # column/table names this metric can be grouped by
    filters: list[str] = field(default_factory=list)
    # SQL fragments always applied when this metric is computed
    unit: str = ""             # "USD" | "%" | "units" | ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class Benchmark:
    """A stored question/expected-SQL pair used to evaluate room quality."""

    question: str
    expected_sql: str
    expected_row_count: int | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    notes: str = ""


# ────────────────────────────────────────────────────────────────────────────
# Metadata
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class MetadataConflict:
    """Recorded when two providers supply different values for a scalar field."""

    table: str
    column: str | None
    field: str
    values: dict[str, str]
    resolved_to: str


@dataclass
class ColumnMeta:
    """Fully-resolved column metadata after the metadata stack runs."""

    # Physical (from CatalogProvider — always present)
    name: str
    data_type: str

    # Descriptive (any metadata provider can contribute)
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    sample_values: list[str] = field(default_factory=list)
    value_description: str = ""

    # Semantic
    semantic_type: str = ""
    currency_code: str = ""
    date_format: str = ""

    # Structural hints
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_key_table: str = ""
    foreign_key_column: str = ""

    # Behavioral hints for SQL generation
    is_high_cardinality: bool = False
    exclude_from_select_star: bool = False

    # Provenance
    metadata_source: str = "catalog"


@dataclass
class TableMeta:
    """Fully-resolved table metadata after the metadata stack runs."""

    # Physical
    full_name: str
    columns: list[ColumnMeta] = field(default_factory=list)
    row_count: int | None = None

    # Descriptive
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    grain: str = ""

    # Semantic
    domain: str = ""
    freshness: str = ""

    # Behavioral
    default_date_column: str = ""
    default_filter: str = ""
    recommended_joins: list[str] = field(default_factory=list)

    # Provenance and quality
    metadata_sources: list[str] = field(default_factory=list)
    conflicts: list[MetadataConflict] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────
# RoomConfig
# ────────────────────────────────────────────────────────────────────────────


def _is_url_safe_room_id(s: str) -> bool:
    """URL-safe room_id: non-empty, no whitespace, no slashes, no backslashes."""
    if not s:
        return False
    return not any(c.isspace() or c in "/\\" for c in s)


@dataclass
class RoomConfig:
    """Complete configuration for one Room.

    Persisted as JSON via dataclasses.asdict() + json.dumps() under store key
    `room:{room_id}:config`. Loaded fresh at the start of every request.
    """

    room_id: str
    title: str
    tables: list[str]
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
    # EXT-5: URLs of external MCP servers this room is permitted to call
    # during the reasoning pipeline. Empty list disables MCP tool consumption
    # entirely for this room (zero regression vs. pre-EXT-5 behavior).
    # The room author explicitly opts each server in — this is a security
    # boundary, not a performance toggle.
    mcp_servers: list[str] = field(default_factory=list)
    # EXT-11
    hypothesis_mode_enabled: bool = False
    domain_knowledge: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not _is_url_safe_room_id(self.room_id):
            raise ValueError(
                f"RoomConfig.room_id must be a non-empty URL-safe string "
                f"(no spaces, slashes, or backslashes); got {self.room_id!r}"
            )
        if not self.tables:
            raise ValueError(
                "RoomConfig.tables must contain at least one fully-qualified "
                "table name"
            )
        if not self.warehouse_id:
            raise ValueError("RoomConfig.warehouse_id must be non-empty")
        ids = [ex.id for ex in self.examples]
        if len(set(ids)) != len(ids):
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                "RoomConfig.examples must have unique ids; "
                f"duplicates: {duplicates}"
            )
        for f in self.default_filters:
            stripped = f.strip().upper()
            if stripped.startswith("SELECT ") or stripped.startswith("WITH "):
                raise ValueError(
                    f"default_filters entries must be SQL fragments, not full "
                    f"statements (SELECT or WITH); got: {f!r}"
                )

    @classmethod
    def from_dict(cls, d: dict) -> RoomConfig:
        """Reconstruct from a JSON-deserialized dict, rehydrating nested types.

        For `sql_filters` / `sql_expressions` / `sql_measures`: the snippet's
        `kind` is structurally determined by which list it lives in. We inject
        the kind here when missing so room configs authored in the Genie wire
        format (no `kind` field — Genie keeps each list typed by its key) load
        without modification. Configs that DO include `kind` keep it; we never
        override an explicit value.
        """
        def _snippets(items: list[dict] | None, default_kind: str) -> list[SqlSnippet]:
            out: list[SqlSnippet] = []
            for raw in items or []:
                payload = dict(raw)
                payload.setdefault("kind", default_kind)
                out.append(SqlSnippet(**payload))
            return out

        return cls(
            room_id=d["room_id"],
            title=d["title"],
            tables=list(d["tables"]),
            warehouse_id=d["warehouse_id"],
            text_instruction=d.get("text_instruction", ""),
            examples=[ExampleSQL(**e) for e in d.get("examples", [])],
            joins=[JoinSpec(**j) for j in d.get("joins", [])],
            sql_filters=_snippets(d.get("sql_filters"), "filter"),
            sql_expressions=_snippets(d.get("sql_expressions"), "expression"),
            sql_measures=_snippets(d.get("sql_measures"), "measure"),
            metrics=[Metric(**m) for m in d.get("metrics", [])],
            sample_questions=list(d.get("sample_questions", [])),
            benchmarks=[Benchmark(**b) for b in d.get("benchmarks", [])],
            column_overrides=[
                ColumnOverride(**o) for o in d.get("column_overrides", [])
            ],
            default_filters=list(d.get("default_filters", [])),
            max_tables_per_query=d.get("max_tables_per_query", 10),
            mcp_servers=list(d.get("mcp_servers", [])),
            hypothesis_mode_enabled=d.get("hypothesis_mode_enabled", False),
            domain_knowledge=list(d.get("domain_knowledge", [])),
        )


# ────────────────────────────────────────────────────────────────────────────
# Query and viz results
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class QueryResult:
    """Returned by QueryProvider.execute()."""

    columns: list[str]
    rows: list[dict]
    row_count: int
    truncated: bool
    duration_ms: int


@dataclass
class VizResult:
    """Produced by VizAgent."""

    chart_type: str
    vega_lite_spec: dict
    summary: str


# ────────────────────────────────────────────────────────────────────────────
# LLM provider supporting types
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class LLMMessage:
    role: str       # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    content: str
    usage: dict     # {"prompt_tokens": int, "completion_tokens": int}
    raw: Any        # provider-specific raw response


# ────────────────────────────────────────────────────────────────────────────
# Vector provider supporting type
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class VectorMatch:
    id: str
    score: float
    payload: dict   # {"question": str, "sql": str, "room_id": str}


# ────────────────────────────────────────────────────────────────────────────
# Agent result types
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class IntentResult:
    intent: str                          # "sql_query" | "clarify_needed" | "out_of_scope"
    relevant_tables: list[str]
    relevant_snippets: list[SqlSnippet]
    confidence: float
    reasoning: str
    table_selection_method: str = "configured"  # EXT-2


@dataclass
class SQLResult:
    is_valid: bool
    attempts: int
    sql: str = ""              # empty string on error paths
    explanation: str = ""      # empty string on error paths
    error: str | None = None   # populated when is_valid=False


@dataclass
class ClarifyResult:
    question: str


# ────────────────────────────────────────────────────────────────────────────
# Benchmark result types
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class BenchmarkResult:
    benchmark_id: str
    question: str
    expected_sql: str
    generated_sql: str | None
    sql_match: bool
    result_match: bool | None
    passed: bool
    error: str | None


@dataclass
class BenchmarkReport:
    room_id: str
    run_at: str
    total: int
    passed: int
    failed: int
    score: float
    results: list[BenchmarkResult]


# ────────────────────────────────────────────────────────────────────────────
# EXT-1: multi-query reasoning
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ReasoningStep:
    step_id: str
    description: str
    sql: str | None
    result: QueryResult | None
    depends_on: list[str]


@dataclass
class ReasoningPlan:
    question: str
    steps: list[ReasoningStep]
    synthesis_instruction: str


@dataclass
class SynthesizedAnswer:
    answer: str
    data_supports: list[str]
    data_does_not_support: list[str]
    would_need: list[str]
    confidence: str             # "high" | "medium" | "low"
    confidence_rationale: str


# ────────────────────────────────────────────────────────────────────────────
# EXT-11: hypothesis mode
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class Hypothesis:
    statement: str
    supporting_patterns: list[str]
    contradicting_patterns: list[str]
    testability: str              # "testable_in_room" | "requires_external_data" | "not_testable"
    suggested_test: str | None
    domain_knowledge_used: list[str]

    def __post_init__(self) -> None:
        # EXT-11 invariant #3 (CLAUDE.md / vision.md): a hypothesis with only
        # supporting evidence is not a hypothesis, it is a claim. Enforced at
        # the dataclass level so no code path — agent, test, or future
        # refactor — can construct a one-sided Hypothesis.
        if not self.contradicting_patterns:
            raise ValueError(
                "Hypothesis MUST have at least one contradicting_pattern "
                "(never present only supporting evidence)"
            )


_HYPOTHESIS_DISCLAIMER_DEFAULT = (
    "These are hypotheses derived from data patterns, not conclusions. "
    "Each should be independently verified before acting on it."
)


# ────────────────────────────────────────────────────────────────────────────
# EXT-5: MCP tool consumption
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict  # JSON Schema describing arguments


@dataclass
class MCPToolResult:
    tool_name: str
    content: str
    is_error: bool


@dataclass
class HypothesisResult:
    hypotheses: list[Hypothesis]
    confidence: str = "low"  # INVARIANT: always "low"
    disclaimer: str = _HYPOTHESIS_DISCLAIMER_DEFAULT

    def __post_init__(self) -> None:
        if self.confidence != "low":
            raise ValueError(
                f"HypothesisResult.confidence MUST be 'low'; got {self.confidence!r}"
            )
        if not self.disclaimer or not self.disclaimer.strip():
            raise ValueError("HypothesisResult.disclaimer MUST be non-empty")
        # Hypothesis.__post_init__ already enforces the contradicting_patterns
        # invariant — by the time a Hypothesis lands here it has already been
        # validated. Kept as a no-op sanity loop in case the dataclass is
        # mutated post-construction (e.g. via dict patching during tests).
        for i, h in enumerate(self.hypotheses):
            if not h.contradicting_patterns:
                raise ValueError(
                    f"Hypothesis at index {i} lost its contradicting_patterns "
                    "after construction — this should be unreachable"
                )


# ────────────────────────────────────────────────────────────────────────────
# ConversationTurn and ContextPackage
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ConversationTurn:
    """One complete exchange in a conversation.

    Mutual exclusion: exactly one of `sql`, `clarification_question`, or
    `error` MUST be set.

    All scalar identifier fields default to falsy values so the engine can
    construct a turn with only the result of a pipeline stage (sql / error /
    clarification) and fill `turn_id`, `duration_ms`, etc. before persisting.
    """

    room_id: str
    conversation_id: str = ""
    turn_id: str = ""
    question: str = ""
    sql: str | None = None
    query_result: QueryResult | None = None
    viz: VizResult | None = None
    clarification_question: str | None = None
    error: str | None = None
    duration_ms: int = 0
    feedback_signal: str | None = None
    # EXT-1
    synthesized_answer: SynthesizedAnswer | None = None
    # EXT-11
    hypothesis_result: HypothesisResult | None = None

    def __post_init__(self) -> None:
        set_count = sum(
            1
            for v in (self.sql, self.clarification_question, self.error)
            if v is not None
        )
        if set_count != 1:
            raise ValueError(
                "ConversationTurn MUST have exactly one of "
                "{sql, clarification_question, error} set "
                f"(got {set_count}); these are mutually exclusive."
            )


@dataclass
class ContextPackage:
    """The assembled bundle passed to every agent before any LLM call."""

    room_id: str
    table_schemas: dict[str, TableMeta]
    joins: list[JoinSpec]
    sql_snippets: list[SqlSnippet]
    metrics: list[Metric]      # named business concepts with dimensions
    text_instruction: str
    default_filters: list[str]
    # Room-level mandatory filters from RoomConfig.default_filters. Injected
    # into the SQL agent's prompt as constraints that MUST appear in every
    # generated query. Populated by ContextBuilder from RoomConfig.
    retrieved_examples: list[ExampleSQL]
    conversation_history: list[ConversationTurn]
    table_selection_method: str = "configured"
    # EXT-2: how `table_schemas` was selected. "configured" when all entries
    # in RoomConfig.tables are explicit FQNs; "dynamic_search" when every
    # entry was a wildcard expanded by TableSelector; "hybrid" when mixed.
    # IntentAgent copies this onto IntentResult.
    mcp_context: list[str] = field(default_factory=list)
    # EXT-5: resolved tool results from external MCP servers, one entry per
    # successful tool call, formatted as "tool_name: <result>". Empty when
    # the room declares no `mcp_servers` or every call failed. Surfaced in
    # IntentAgent / SQLAgent / SynthesisAgent prompts as additional context.
    domain_knowledge: list[str] = field(default_factory=list)
    # EXT-11: room author's domain axioms, copied from RoomConfig.domain_knowledge.
    # Injected into HypothesisAgent's prompt as targeted hypothesis-generation
    # context. Empty for rooms without hypothesis mode configured — the rest
    # of the pipeline ignores this field, so default-empty is zero-cost.
