---
tags: [layer/extensions]
status: stable
depends_on: [vision, providers, agents, room_engine, api]
---

# Extensions

## In this system

**Linked from:** [[README]], [[vision]], [[demo]]
**Links to:** [[vision]], [[providers]], [[agents]], [[room_engine]], [[api]], [[data_models]]
**Layer:** extensions

---

## What this is

The capabilities that make Tiri genuinely different from Genie — not incremental improvements but architectural extensions that require deliberate design. Each extension is a named, bounded unit of work with its own interface contract and test cases.

Extensions are grouped by whether they are **core** (required for initial release) or **planned** (designed for but not blocking release). Every core extension must be implemented before Tiri is considered feature-complete relative to [[vision]].

The north star for all extensions: a junior analyst who reasons across multiple data sources, shows their work, and never bluffs. See [[vision]].

---

## Core extensions

### EXT-1 — Multi-query reasoning

**What Genie does:** One question → one SQL query → one result.

**What Tiri does:** One question → reasoning plan → multiple SQL queries → synthesized answer.

**Why it matters:** Most business questions cannot be answered by a single query. "Why did churn increase last quarter?" requires at minimum: churn rate over time, churn by segment, churn by cohort, and possibly external factors like contract renewals. A single query answers none of these. Tiri must plan, retrieve, and synthesize.

**Interface — PlanningAgent:**

```python
@dataclass
class ReasoningStep:
    step_id: str
    description: str        # "Calculate churn rate by month"
    sql: str | None         # populated after SQLAgent runs this step
    result: QueryResult | None
    depends_on: list[str]   # step_ids this step needs before running

@dataclass
class ReasoningPlan:
    question: str
    steps: list[ReasoningStep]
    synthesis_instruction: str   # how to combine results into a final answer

class PlanningAgent:
    async def plan(
        self,
        question: str,
        context: ContextPackage,
        llm: LLMProvider,
    ) -> ReasoningPlan:
        """
        Determine whether the question requires multiple queries.
        Single-query questions return a plan with one step — no overhead.
        Multi-query questions return an ordered plan with dependencies.
        """
```

**Prompt template:** `engine/prompt_templates/planning.txt`

```
You are analyzing a business question to determine how many SQL queries
are needed to answer it completely.

## Question
{question}

## Available tables and their relationships
{table_summary}

## Instructions
{text_instruction}

Respond with a JSON reasoning plan:
{
  "requires_multiple_queries": true | false,
  "steps": [
    {
      "step_id": "step_1",
      "description": "one sentence describing this query's purpose",
      "depends_on": []
    }
  ],
  "synthesis_instruction": "how to combine results — e.g. compare step_1 and step_2 to identify the dominant driver"
}

If requires_multiple_queries is false, return exactly one step.
Maximum steps: 5. If more are needed, prioritize the most informative.
```

**Synthesis — SynthesisAgent:**

```python
class SynthesisAgent:
    async def synthesize(
        self,
        question: str,
        plan: ReasoningPlan,
        results: list[QueryResult],
        llm: LLMProvider,
    ) -> str:
        """
        Given a plan and all its query results, produce a prose answer.

        Rules (from vision.md):
        - State what the data shows. Never state what caused it.
        - Quantify confidence where possible ("3 of 4 data sources agree...")
        - Name what cannot be determined from this data
        - Round all numbers consistently
        - End with: "Supporting data: [step descriptions]"
        """
```

**Integration into [[room_engine]]:**

`RoomEngine.chat()` gains a planning step before the SQL agent:

```
IntentAgent → PlanningAgent → [SQLAgent × N steps] → SynthesisAgent → VizAgent
```

Single-query questions go through a one-step plan — no performance cost for simple questions.

**Test cases:**

| # | Scenario | MUST |
|---|---|---|
| 1 | Simple aggregation question | MUST return a one-step plan |
| 2 | "Why did X change?" question | MUST return a multi-step plan with ≥ 2 steps |
| 3 | Multi-step plan | MUST execute steps in dependency order |
| 4 | Synthesis output | MUST NOT contain causal language ("X caused Y", "because of X") |
| 5 | Synthesis output | MUST reference the supporting steps by description |
| 6 | Any plan | MUST have ≤ 5 steps |

---

### EXT-2 — Dynamic table selection

**What Genie does:** Admin pre-selects up to 30 tables. Every question loads all of them into context.

**What Tiri does:** A room can be scoped to a catalog or schema. The `IntentAgent` dynamically selects the relevant tables per question using semantic search over table metadata.

**Why it matters:** Removes the 30-table ceiling entirely. Makes room configuration lighter — describe the domain, don't enumerate every table. Keeps prompts focused — only relevant schemas are injected.

**Interface additions to `IntentAgent`:**

```python
@dataclass
class IntentResult:
    # existing fields ...
    relevant_tables: list[str]    # NOW: dynamically selected, not pre-filtered
    table_selection_method: str   # "configured" | "dynamic_search" | "hybrid"

class TableSelector:
    def __init__(self, catalog: CatalogProvider, vector: VectorProvider,
                 llm: LLMProvider): ...

    async def select(
        self,
        question: str,
        room_config: RoomConfig,
        max_tables: int = 10,
    ) -> list[str]:
        """
        When room_config.tables is a wildcard pattern (e.g. "tpch.sf1.*"):
          1. catalog.list_tables() for all tables in scope
          2. Embed question + embed table names+comments
          3. Return top max_tables by cosine similarity
          4. Always include tables explicitly referenced in room_config.joins

        When room_config.tables is an explicit list (≤ 30):
          Return the list as-is — no dynamic selection needed.
        """
```

**RoomConfig change:**

```python
@dataclass
class RoomConfig:
    # tables can now be explicit list OR wildcard patterns
    tables: list[str]
    # e.g. ["tpch.sf1.customer", "tpch.sf1.orders"]  ← explicit (existing)
    # e.g. ["tpch.sf1.*"]                             ← wildcard (new)
    # e.g. ["tpch.*.*"]                               ← full catalog (new)
    max_tables_per_query: int = 10   # cap on dynamic selection
```

**Test cases:**

| # | Scenario | MUST |
|---|---|---|
| 1 | Explicit table list | MUST use configured tables, no dynamic selection |
| 2 | Wildcard `schema.*` with focused question | MUST select ≤ max_tables_per_query tables |
| 3 | Dynamic selection | MUST always include tables referenced in join specs |
| 4 | Question referencing a table not in top-k similarity | MUST still include it if it's in a join spec |
| 5 | Full catalog wildcard with 200 tables | MUST complete selection in < 2 seconds |

---

### EXT-3 — Multi-model routing with provider registry

**What Genie does:** One locked LLM for everything.

**What Tiri does:** A named provider registry allows multiple LLM backends to be declared simultaneously (Databricks, OpenAI, Anthropic, Ollama). Each agent task routes to a specific backend+model. Agents are unaware of routing — they receive a `LLMProvider` and the router is transparent.

**Why it matters:** Intent classification does not need a 70B model. SQL generation benefits from the best SQL model regardless of vendor. Synthesis may warrant a different model entirely. Embedding is always separate. More fundamentally: locking to one LLM vendor is a structural constraint. The registry removes it.

**The three-level design:**

```
Backend (vendor + credentials)   e.g. Databricks workspace, OpenAI account
    └── Model (which model)      e.g. llama-3.3-70b, gpt-4o
            └── Task assignment  e.g. sql → openai_main::gpt-4o
```

Configuration lives in `tiri.toml`. See [[configuration]] for the complete format and examples.

**Interface additions to [[providers]]:**

```python
@dataclass
class ModelRoute:
    task: str                # "intent" | "sql" | "planning" | "synthesis" |
                             # "clarify" | "viz_summary" | "embed"
    provider: LLMProvider    # instantiated backend for this task
    model_name: str          # passed to the backend on each call
    temperature: float = 0.0
    max_tokens: int = 2048

class RouterLLMProvider(LLMProvider):
    """
    Satisfies LLMProvider. Routes each call to the backend configured
    for that task. Always returned by container.py as the 'llm' entry —
    even with one backend. Agents never import or reference this class.
    """
    def __init__(self, routes: dict[str, ModelRoute]): ...

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",   # agents MUST pass this — routing depends on it
    ) -> LLMResponse: ...

    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
    ) -> AsyncIterator[str]: ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Always routes to the 'embed' task backend."""
        ...
```

**Task routing guidance:**

| Task | Recommended tier | Rationale |
|---|---|---|
| `intent` | Fast/small | Classification — accuracy matters more than reasoning depth |
| `planning` | Reasoning/large | Multi-step decomposition benefits from chain-of-thought |
| `sql` | Best available SQL model | Most critical task — use the strongest model you have |
| `synthesis` | Mid/large | Prose + uncertainty framing — instruction-following matters |
| `clarify` | Fast/small | Simple question generation |
| `viz_summary` | Fast/small | One sentence |
| `embed` | Dedicated embedding model | Never a completion model |

**Container wiring** — `container.py._build_llm_registry()`:
1. Parse `tiri.toml` (or env vars in simple mode) into `Config.llm_backends` dict
2. Instantiate one `LLMProvider` per named backend
3. Parse each `"name::model"` routing entry into a `ModelRoute` referencing the provider instance
4. Construct `RouterLLMProvider(routes={task: ModelRoute(...)})`

**Test cases:**

| # | Scenario | MUST |
|---|---|---|
| 1 | Two-backend config | MUST instantiate two separate `LLMProvider` objects |
| 2 | `RouterLLMProvider.complete(task="sql")` | MUST call the SQL-configured backend, not intent backend |
| 3 | `RouterLLMProvider.embed()` | MUST route to the `embed`-configured backend |
| 4 | Agent calls `llm.complete()` without `task=` | Enforced structurally via the SDK-import/agent-import scans in `test_config.py` and `test_agents.py` rather than at runtime; all agents explicitly pass `task=` by convention. The abstract default of `"sql"` is a safety net only |
| 5 | Single-backend config | MUST still return `RouterLLMProvider` — no special case |
| 6 | `RouterLLMProvider` | MUST satisfy full `LLMProvider` interface |
| 7 | `embed` route pointing to Anthropic backend | MUST raise `ConfigurationError` at startup |
| 8 | All agents | MUST pass `task=` on every `llm.complete()` and `llm.stream()` call |

---

### EXT-4 — MCP server exposure

**What Genie does:** Exposes a Conversation API. Cannot be called as an MCP tool.

**What Tiri does:** Exposes itself as an MCP server so any MCP-compatible client — Claude, Cursor, VS Code, other agents — can call a Tiri room as a tool.

**Why it matters:** Makes Tiri composable within larger agent systems. A Claude agent can call `tiri_query` as a tool alongside web search, document retrieval, and other capabilities. This is the integration model for the ecosystem.

**MCP tools to expose:**

```python
# Tool 1: query a room
@mcp_tool(name="tiri_query")
async def tiri_query(
    room_id: str,        # which room to query
    question: str,       # natural language question
    conversation_id: str | None = None,  # for follow-up questions
) -> dict:
    """
    Ask a natural language question to a Tiri room.
    Returns the answer, supporting SQL, and result data.
    """

# Tool 2: list available rooms
@mcp_tool(name="tiri_list_rooms")
async def tiri_list_rooms() -> list[dict]:
    """
    List all available Tiri rooms with their titles and descriptions.
    Use this before tiri_query to find the right room.
    """

# Tool 3: get room schema
@mcp_tool(name="tiri_room_schema")
async def tiri_room_schema(room_id: str) -> dict:
    """
    Return the tables and domain description for a room.
    Use to verify a room covers the data you need before querying.
    """
```

**Implementation:** FastAPI MCP adapter layer on top of the existing [[api]]. Use `fastapi-mcp` or implement the MCP SSE protocol directly. Mount at `/mcp` alongside the existing REST routes.

**Authentication:** MCP clients pass a Bearer token. Same auth middleware as the REST API.

**Test cases:**

| # | Scenario | MUST |
|---|---|---|
| 1 | MCP client calls `tiri_query` with valid room and question | MUST return answer in MCP tool result format |
| 2 | MCP client calls `tiri_list_rooms` | MUST return all rooms the caller has access to |
| 3 | `tiri_query` with `conversation_id` | MUST maintain conversation context across calls |
| 4 | MCP server | MUST be mountable alongside existing REST API without conflict |
| 5 | Unauthenticated MCP call | MUST return MCP error, not HTTP 401 |

---

### EXT-5 — MCP tool consumption

**What Genie does:** Cannot call external tools during a query.

**What Tiri does:** Agents can call registered MCP servers as tools during the reasoning pipeline — to resolve ambiguous terms, fetch external context, or look up documentation.

**Why it matters:** Some questions require context that isn't in the database. "What does 'ARR' mean for our company?" might be in Confluence. "What is the regulatory threshold for this metric?" might be in a policy document. Tiri can resolve these by calling external MCP servers rather than failing or hallucinating.

**Interface additions to [[providers]]:**

```python
@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict    # JSON Schema

@dataclass
class MCPToolResult:
    tool_name: str
    content: str
    is_error: bool

class MCPProvider(ABC):
    """New provider — not in the original five."""

    @abstractmethod
    async def list_tools(self) -> list[MCPTool]: ...

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict) -> MCPToolResult: ...
```

**Integration into [[agents]]:**

`ContextBuilder` gains an optional `mcp_provider`. When present, `IntentAgent` can call MCP tools to resolve ambiguous terms before SQL generation:

```python
async def resolve_term(
    term: str,
    mcp: MCPProvider,
    llm: LLMProvider,
) -> str | None:
    """
    If a term in the question is ambiguous and an MCP tool might resolve it,
    call the tool and return the resolution.
    Returns None if no relevant tool is found.
    Used by IntentAgent before routing to SQLAgent.
    """
```

**RoomConfig addition:**

```python
@dataclass
class RoomConfig:
    # ... existing fields ...
    mcp_servers: list[str] = field(default_factory=list)
    # URLs of MCP servers this room is permitted to call
    # e.g. ["https://confluence.mycompany.com/mcp", "https://glean.mycompany.com/mcp"]
```

**Test cases:**

| # | Scenario | MUST |
|---|---|---|
| 1 | Room with `mcp_servers` configured | MUST make MCP tools available to IntentAgent |
| 2 | Room with no `mcp_servers` | MUST behave identically to current behavior — no regression |
| 3 | MCP tool call timeout | MUST fall back gracefully, not block the pipeline |
| 4 | MCP tool call error | MUST log the error and continue without the tool result |
| 5 | MCP tool result | MUST be included in the ContextPackage and visible in the reasoning trace |

---

### EXT-6 — Per-user credential execution

**What Genie does:** Embeds the room creator's credentials. All users execute queries as that one person.

**What Tiri does:** Passes through the authenticated user's own credentials to `QueryProvider`. Unity Catalog row-level security and column masking apply correctly per user.

**Why it matters:** This is a governance requirement, not a feature. A user who cannot see PII columns should not be able to ask Tiri questions that expose them. The current Genie model breaks Unity Catalog's security model.

**Interface change to `QueryProvider`:**

```python
class QueryProvider(ABC):

    @abstractmethod
    async def execute(
        self,
        sql: str,
        limit: int = 10_000,
        user_token: str | None = None,   # NEW — pass-through user credential
    ) -> QueryResult: ...

    @abstractmethod
    async def validate(
        self,
        sql: str,
        user_token: str | None = None,   # NEW
    ) -> tuple[bool, str | None]: ...
```

**API layer change:**

`RoomEngine.chat()` accepts `user_token: str | None`. The [[api]] layer extracts it from the request's `Authorization` header and passes it through. When `user_token` is present, `DatabricksQueryProvider` uses it instead of the service credential.

**Databricks implementation:**

The Statement Execution API accepts a token in the Authorization header. `DatabricksQueryProvider` swaps the header when `user_token` is provided.

**Fallback:** When `user_token` is `None` (service-to-service calls, benchmark runs), fall back to the configured service credential. Never fail silently.

**Test cases:**

| # | Scenario | MUST |
|---|---|---|
| 1 | User with restricted column access queries a table with masked columns | MUST return masked values, not raw PII |
| 2 | User without SELECT on a table asks about it | MUST return a permission error, not silently return no rows |
| 3 | `user_token=None` (service account) | MUST use service credential — no regression |
| 4 | `user_token` passed to `validate()` | MUST validate with user's permissions, not service account's |
| 5 | Two users with different permissions query same question | MUST return different results if their access differs |

---

### EXT-7 — Explicit uncertainty

**What Genie does:** Returns an answer. Does not quantify confidence or name gaps.

**What Tiri does:** Every synthesized answer includes an explicit uncertainty statement — what the data supports, what it does not, and what would be needed to answer more completely.

**Why it matters:** This is the [[vision]] requirement most directly tied to the audience. Congressional staffers and executives presenting data need to know the limits of what they're presenting. See [[vision]] — "Tiri is a witness, not an analyst."

**Interface addition to `SynthesisAgent`:**

```python
@dataclass
class SynthesizedAnswer:
    answer: str                    # the main prose answer
    data_supports: list[str]       # bullet list of what the data directly shows
    data_does_not_support: list[str]  # what cannot be determined from this data
    would_need: list[str]          # what additional data/analysis would be needed
    confidence: str                # "high" | "medium" | "low"
    confidence_rationale: str      # one sentence explaining the confidence level
```

**Confidence levels:**

- `high` — question answered directly by a single unambiguous query with clean data
- `medium` — answer requires joining multiple tables or assumptions about business definitions
- `low` — answer requires inference across incomplete data, time gaps, or ambiguous terms

**Output format in API response:**

The `ConversationTurn` gains a `synthesized_answer: SynthesizedAnswer | None` field. When multi-query reasoning is used, this is always populated. For single-query questions, it is populated only when confidence is medium or low.

**Test cases:**

| # | Scenario | MUST |
|---|---|---|
| 1 | Direct aggregation question | MUST return `confidence="high"` |
| 2 | "Why did X change?" question | MUST return `confidence="low"` and non-empty `data_does_not_support` |
| 3 | Any answer | MUST NOT contain "because", "caused by", "due to", "result of" in `answer` field |
| 4 | `data_does_not_support` | MUST be non-empty for any question requiring causal inference |
| 5 | `would_need` | MUST suggest concrete additional data sources, not generic disclaimers |

---

## Planned extensions

These are designed for but not required in the initial release. Each has enough design here to be implementable when prioritized.

### EXT-8 — Cross-room federation

A meta-room that routes questions to specialized sub-rooms and synthesizes cross-domain answers. Example: "How does our marketing spend correlate with customer churn?" routes to a marketing room and a customer success room, runs both, and synthesizes.

**Design sketch:** A `FederatedRoom` contains a list of `room_ids` and a routing description per room. The `PlanningAgent` routes each step to the appropriate sub-room. `SynthesisAgent` combines results across rooms. Each sub-room is maintained independently by the team that owns that domain.

**Blocking dependency:** Requires EXT-1 (multi-query reasoning) to be complete first.

---

### EXT-9 — Workspace knowledge harvesting

Continuously scan the Databricks workspace for queries, notebook code, and dashboard SQL that represent implicit knowledge. Surface high-quality queries as candidate examples for room configs, with admin review.

**Design sketch:** A background job runs `GET /api/2.0/sql/history` to retrieve recent queries. Filter by quality signals (saved queries, queries used in dashboards, queries by known analysts). Embed and compare against existing room examples. Propose new examples via the [[feedback]] proposer pipeline.

**Blocking dependency:** Requires [[feedback]] proposer to be stable first.

---

### EXT-10 — Semantic layer integration

First-class support for dbt metrics, Unity Catalog metric views, and Cube.dev semantic models as inputs to room configuration. External semantic definitions are imported as `Metric` objects — the same type used for locally-defined metrics in `RoomConfig` — making the local and external paths consistent.

**Foundation (in place now):** The `Metric` type in [[data_models]] and `RoomConfig.metrics` already provide the local semantic layer. Room authors can define named business concepts with SQL definitions, declared dimensions, and filters without any external dependency. EXT-10 extends this by allowing those `Metric` objects to be sourced from external systems rather than hand-authored.

**Design sketch:** A `SemanticLayerProvider` ABC alongside the existing providers. Implementations for dbt Cloud API, UC metric views REST API, and Cube.dev. The provider fetches metric definitions from the external system and returns them as `list[Metric]` — the same type `RoomConfig.metrics` already uses. `ContextBuilder` receives metrics from both sources (local config + external provider) and merges them, with `RoomConfig.metrics` taking precedence (same last-writer-wins principle as the metadata stack).

**What it unlocks:**
- A room can reference an organization's dbt metric definitions without re-authoring them
- UC metric views become first-class inputs to the SQL agent
- Changes to upstream metric definitions flow through automatically on the next request

**Blocking dependency:** None — `Metric` type is in place. EXT-10 is purely additive.

**Planned gap — join graph traversal:** The current `JoinSpec` model is a flat list of table-pair relationships. At scale (20+ tables), the SQL agent needs to traverse a relationship graph to find multi-hop join paths — not look up a static list. EXT-10 is the natural time to introduce a `JoinGraph` abstraction that derives valid paths from the declared `JoinSpec` entries. Until then, room authors must explicitly declare every join path they want the agent to use.

---

### EXT-11 — Hypothesis mode

Enables Tiri to generate candidate explanations for observed data patterns — explicitly provisional, explicitly uncertain, always showing supporting and contradicting evidence. Closes the gap between "what happened" (what Tiri does by default) and "why it happened" (what domain experts and data scientists need).

**Why this is designed carefully:** Causal claims from observational data are frequently wrong. The default "witness" mode exists to protect users from confident-wrong answers. Hypothesis mode does not abandon that protection — it reframes caution explicitly. A hypothesis is not a conclusion. It is a candidate explanation the data does not contradict. The architecture enforces this distinction at the type level. See [[vision]] for the full reasoning.

**New types** (add to [[data_models]]):

```python
@dataclass
class Hypothesis:
    statement: str                      # "X may be contributing to Y" — never "X caused Y"
    supporting_patterns: list[str]      # what in the data is consistent with this
    contradicting_patterns: list[str]   # what in the data cuts against this
    testability: str                    # "testable_in_room" | "requires_external_data" | "not_testable"
    suggested_test: str | None          # if testable_in_room: what query would confirm or refute it
    domain_knowledge_used: list[str]    # which domain_knowledge entries from RoomConfig were used

@dataclass
class HypothesisResult:
    hypotheses: list[Hypothesis]
    confidence: str = "low"             # ALWAYS "low" — hypotheses are provisional by definition
    disclaimer: str = (
        "These are hypotheses derived from data patterns, not conclusions. "
        "Each should be independently verified before acting on it."
    )
```

**`confidence` is always `"low"` and `disclaimer` is mandatory.** These are not defaults that can be overridden — they are invariants enforced by the dataclass. The type system prevents Tiri from presenting a hypothesis with false confidence.

**New `RoomConfig` fields** (add to [[data_models]]):

```python
@dataclass
class RoomConfig:
    # ... existing fields ...
    hypothesis_mode_enabled: bool = False
    # Off by default. Room author opts in explicitly.
    # Rooms serving high-stakes non-technical audiences (congressional staffers,
    # executives in regulated industries) should leave this False.

    domain_knowledge: list[str] = field(default_factory=list)
    # Domain axioms the room author provides for hypothesis generation.
    # These are injected into HypothesisAgent's context.
    # Examples:
    #   "In this business, SMB customers are more price-sensitive than enterprise"
    #   "Q4 revenue spikes are normal for this industry — do not treat as anomalies"
    #   "Customer churn typically lags contract value changes by 1-2 quarters"
    # These are auditable, version-controlled, and visible to users — unlike
    # unverifiable LLM prior knowledge.
```

**New agent — `HypothesisAgent`:**

```python
class HypothesisAgent:
    def __init__(self, llm: LLMProvider): ...

    async def run(
        self,
        question: str,
        plan: ReasoningPlan,           # from PlanningAgent (EXT-1)
        results: list[QueryResult],    # all query results from the plan
        synthesized: SynthesizedAnswer,  # from SynthesisAgent (EXT-1)
        context: ContextPackage,       # for domain_knowledge and table metadata
    ) -> HypothesisResult:
        """
        Generate candidate hypotheses from observed patterns.

        Rules (enforced by prompt and output validation):
        - Never use causal language: "caused", "because", "due to", "result of"
        - Always use hedged language: "consistent with", "may contribute to",
          "one hypothesis is", "the data does not contradict"
        - For each hypothesis: identify what data supports it AND what contradicts it
        - If domain_knowledge is provided: use it to generate more informed hypotheses,
          and record which knowledge entries were used in Hypothesis.domain_knowledge_used
        - If a hypothesis is testable with data in this room: specify the test
        - Maximum 3 hypotheses — quality over quantity
        """
```

**Prompt template:** `engine/prompt_templates/hypothesis_generation.txt`

```
You are generating hypotheses — candidate explanations for observed data patterns.
A hypothesis is NOT a conclusion. It is a provisional statement that the data does not contradict.

CRITICAL LANGUAGE RULES:
- NEVER use: "caused", "because", "due to", "result of", "led to", "explains"
- ALWAYS use: "consistent with", "may contribute to", "one possible explanation",
  "the data does not contradict", "is associated with"

## What the data shows
{synthesized_answer}

## Observed patterns across all queries
{pattern_summary}

## Domain knowledge for this room
{domain_knowledge}
(Use this to generate more targeted hypotheses. State which entries you used.)

## Question being investigated
{question}

Generate up to 3 hypotheses. For each:
1. A hedged statement of the hypothesis (no causal language)
2. What patterns in the data support it
3. What patterns in the data cut against it
4. Whether it is testable with data available in this room
5. If testable: what specific analysis would confirm or refute it

Respond as JSON only. No markdown fences.
```

**Integration into [[room_engine]]:**

`HypothesisAgent` runs after `SynthesisAgent` only when:
1. `RoomConfig.hypothesis_mode_enabled == True`
2. The question is a causal/why question (detected by `IntentAgent` — add `"hypothesis_request"` as a new intent value)
3. EXT-1 has produced a `ReasoningPlan` with results (hypothesis requires multi-query context)

The updated pipeline when hypothesis mode is on:
```
IntentAgent → PlanningAgent → [SQLAgent × N] → SynthesisAgent → HypothesisAgent → VizAgent
```

`ConversationTurn` gains `hypothesis_result: HypothesisResult | None` — None when hypothesis mode is off or the question is not a why-question.

**Knowledge sources for domain context (in priority order):**

1. `RoomConfig.domain_knowledge` — room author's explicit axioms (preferred: auditable, controlled)
2. External MCP knowledge server (EXT-5) — if configured, retrieves relevant domain context from Confluence, wikis, etc.
3. LLM prior knowledge — augments 1 and 2 only; never sole source. Clearly marked as "general domain knowledge" in output.

**Test cases:**

| # | Scenario | MUST |
|---|---|---|
| 1 | `HypothesisAgent` output | MUST NOT contain "caused", "because", "due to", "result of" |
| 2 | `HypothesisResult.confidence` | MUST always be "low" — not configurable |
| 3 | `HypothesisResult.disclaimer` | MUST always be present and non-empty |
| 4 | Room with `hypothesis_mode_enabled=False` | MUST NOT call `HypothesisAgent` |
| 5 | Hypothesis with `testability="testable_in_room"` | MUST include a `suggested_test` |
| 6 | Hypothesis | MUST include at least one `contradicting_pattern` — never present only supporting evidence |
| 7 | `domain_knowledge_used` | MUST only reference entries actually in `RoomConfig.domain_knowledge` |
| 8 | Any hypothesis | MUST NOT assert causation — "X caused Y" is a test failure |
| 9 | `HypothesisAgent` | MUST NOT run without a completed `ReasoningPlan` from EXT-1 |
| 10 | Room with `hypothesis_mode_enabled=True`, non-causal question | MUST NOT call `HypothesisAgent` |

**Blocking dependency:** Requires EXT-1 (multi-query reasoning + `SynthesisAgent`) to be complete first. `HypothesisAgent` reasons over the full `ReasoningPlan` and all its results — it cannot operate on a single-query response.

---

## Extension build order

Implement core extensions in this order to avoid blocking dependencies:

```
EXT-3 (multi-model routing)     ← enables correct model assignment before other agents are built
EXT-2 (dynamic table selection) ← builds on IntentAgent which must exist first
EXT-6 (per-user credentials)    ← purely additive, no dependencies
EXT-7 (explicit uncertainty)    ← additive to SynthesisAgent
EXT-1 (multi-query reasoning)   ← requires all agents to be stable first
EXT-4 (MCP server exposure)     ← requires API to be complete first
EXT-5 (MCP tool consumption)    ← requires EXT-4 infrastructure
EXT-11 (hypothesis mode)        ← requires EXT-1 (SynthesisAgent) and benefits from EXT-5 (domain MCP)
```
