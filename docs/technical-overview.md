# Tiri — architectural overview

**For:** Databricks engineers and AI practitioners  
**Purpose:** Get oriented fast. This document covers what Tiri is, why it's built the way it is, how the pieces fit together, and where to read next.

---

## What it is

Tiri is a data reasoning system — a natural-language interface to structured data that plans across multiple queries, synthesizes results, and explicitly states what the data does and does not support.

It is built on the Databricks platform alongside Genie, using the same foundation of Unity Catalog, Model Serving, SQL Warehouses, and Vector Search. Tiri is designed for scenarios where Genie's built-in behavior doesn't meet integration requirements: multi-query reasoning, BYO LLM, per-user credential enforcement, MCP composability, or explicit uncertainty for high-stakes audiences.

**Named after Tiresias** — the Greek prophet who revealed what was already true rather than predicting the future. Tiri surfaces what the data says, with evidence, without bluffing.

---

## The one rule everything else follows

**Tiri is a witness, not an analyst.**

It never produces causal claims from observational data. The phrases `caused by`, `because of`, `due to`, `result of`, `led to` are **structurally forbidden** — enforced by a post-generation scan in `SynthesisAgent` and `HypothesisAgent` that raises rather than ships. This is not a prompt instruction. It is a hard invariant in code.

The reason: causal inference from observational data is genuinely hard. A system that produces confident-sounding causal claims in a high-stakes context — executive decisions, policy testimony, operational changes — will eventually be wrong in a way that matters. The architecture prevents this at the type level.

---

## Architecture in one pass

### The pipeline

A question enters through one of three surfaces — REST API, SSE streaming endpoint, or MCP tool call — and flows through a single pipeline:

```
question
    │
    ▼
IntentAgent          classify: sql_query | clarify_needed | out_of_scope
    │
    ▼
ContextBuilder       MetadataFetcher + ExampleIndexer + TableSelector + MCPResolver
    │                → ContextPackage (schemas, joins, snippets, examples, MCP context)
    ▼
PlanningAgent        ReasoningPlan: 1–5 steps with dependency ordering
    │
    ▼
SQLAgent × N         for each step: validate() → execute() with user_token
    │
    ▼
SynthesisAgent       SynthesizedAnswer: answer + data_supports + data_does_not_support
    │                + confidence ("high" | "medium" | "low") + confidence_rationale
    ▼
HypothesisAgent      (opt-in per room, multi-step plans only, causal questions only)
    │                HypothesisResult: always confidence="low", always has contradicting_patterns
    ▼
VizAgent             rule-based Vega-Lite — no LLM call for spec construction
    │
    ▼
ConversationTurn     persisted to StoreProvider
```

**Single-step questions** go through the same pipeline with a one-step plan — no overhead, identical turn shape to a pre-planning path.

**Every agent** receives a typed `ContextPackage` and returns a typed result. No agent imports an SDK. No agent makes a network call. All I/O goes through provider interfaces.

### The provider layer

Seven abstract Python ABCs isolate every external dependency from the engine:

| Provider | Abstracts | Default impl | Local dev swap |
|---|---|---|---|
| `LLMProvider` | Completions, streaming, embeddings | Databricks Model Serving | Ollama, OpenAI, Anthropic |
| `CatalogProvider` | What tables and columns *exist* | Unity Catalog | Static JSON |
| `MetadataProvider` | What tables and columns *mean* | UC annotations (stacked) | YAML file |
| `QueryProvider` | SQL execution + EXPLAIN validation | Databricks SQL Warehouse | DuckDB |
| `VectorProvider` | Example similarity retrieval | Databricks Vector Search | ChromaDB |
| `StoreProvider` | KV persistence | Delta table via SQL Warehouse | SQLite |
| `MCPProvider` | External tool consumption | `HttpMCPProvider` (JSON-RPC 2.0) | Mock transport |

**The zero-I/O rule:** `tiri/engine/` and `tiri/knowledge/` never import `databricks`, `openai`, `anthropic`, `httpx`, or any other SDK directly. Enforced by a static-scan test that fails the build on violations. To add external I/O, add a method to a provider interface and implement it in the provider layer.

**Swap anything.** The routing is in `tiri.toml`. Change `[providers.query] type = "duckdb"` and SQL runs locally. Change `[llm.providers.db] type = "anthropic"` and completions go to Claude. No code changes needed.

### Multi-model routing

`RouterLLMProvider` is always the `llm` entry point — even in single-backend deployments. Every agent passes `task=` on every LLM call. The router dispatches to the configured backend+model for that task:

```toml
[llm.routing]
intent      = "db::databricks-meta-llama-3-1-8b-instruct"   # fast/cheap
planning    = "db::databricks-meta-llama-3-3-70b-instruct"   # reasoning
sql         = "db::databricks-meta-llama-3-3-70b-instruct"   # best SQL model
synthesis   = "db::databricks-meta-llama-3-3-70b-instruct"
clarify     = "db::databricks-meta-llama-3-1-8b-instruct"
viz_summary = "db::databricks-meta-llama-3-3-70b-instruct"   # see note below
embed       = "db::databricks-bge-large-en"
```

**Important:** `viz_summary` routes to the 70B model in production despite being "one sentence." Reason: the 8B output guardrail false-fires on geopolitical content in result rows (nation names like IRAN, IRAQ combined with words like "supply" and "quantity"). `VizAgent` degrades gracefully to an empty summary on any LLM failure — the SQL result and chart still ship — but routing to a guardrail-free or larger model avoids the trip-up entirely.

**Calibration note:** a room's `text_instruction` and examples are tuned against the model in use at authoring time. Switching models is valid but may shift `IntentAgent`'s classification boundary — some models route more questions to `ClarifyAgent` than others. Re-run benchmarks after model changes.

---

## Key design decisions

### validate() before execute() — always

`QueryProvider.validate()` runs EXPLAIN against the warehouse before every `execute()`. The call must pass `user_token` — not just `execute()`. Without this, EXPLAIN runs as the service principal and a user without SELECT on a table would pass validation and fail at execution, bypassing Unity Catalog enforcement at the validate boundary.

This is EXT-6 (per-user credential enforcement). `RoomEngine` threads `user_token` from the request's Bearer token / `X-Forwarded-Access-Token` header through both calls. Databricks Apps injects the latter automatically for every logged-in user — EXT-6 requires zero client-side configuration in an Apps deployment.

### RoomConfig is the room

A `RoomConfig` is the complete specification of a data reasoning environment — what data it can access, what it knows about that data, how questions should be answered, and who can ask them. It's a Python dataclass serialized to JSON and stored in `StoreProvider`:

```python
@dataclass
class RoomConfig:
    room_id: str
    tables: list[str]              # explicit FQNs or wildcard patterns (EXT-2)
    warehouse_id: str
    text_instruction: str          # how to reason in this domain
    examples: list[SqlExample]     # vector-indexed, top-k retrieved per question
    joins: list[JoinSpec]          # explicit join paths (typed enum, not comments)
    sql_filters: list[SqlSnippet]
    sql_expressions: list[SqlSnippet]
    sql_measures: list[SqlSnippet] # named business metrics with SQL definitions
    metrics: list[Metric]          # named concepts with declared dimensions
    column_overrides: list[ColumnOverride]
    default_filters: list[str]     # SQL fragments applied to every query
    domain_knowledge: list[str]    # axioms for HypothesisAgent
    mcp_servers: list[str]         # authorized external MCP URLs (EXT-5)
    hypothesis_mode_enabled: bool  # off by default
    max_tables_per_query: int      # cap for EXT-2 dynamic selection
    benchmarks: list[Benchmark]    # question/expected_sql pairs for scoring
```

Rooms are managed via `POST/PATCH /rooms/{id}` or the CLI:

```bash
python -m tiri.cli load-room demo/my_room.json   # idempotent create-or-update
python -m tiri.cli benchmark --room my-room       # exits non-zero if score < 1.0
python -m tiri.cli import-genie --input space.json --output room.json  # migrate from Genie
```

### Metadata is a stack

`CatalogProvider` answers "what exists" (physical schema). `MetadataProvider` answers "what it means" (semantic enrichment). They're separate concerns with different sources and update frequencies.

Multiple `MetadataProvider` instances stack in declared order. Merge rules: scalar fields — last writer wins. List fields — accumulate across stack. `RoomConfigMetadataProvider` always runs last, so room-level overrides always win.

The stack typically looks like:

```
UCAnnotationsMetadataProvider    (column comments, sample values from Unity Catalog)
YAMLMetadataProvider             (team-maintained domain semantics)
DbtMetadataProvider              (dbt manifest: descriptions, tests, lineage)
RoomConfigMetadataProvider       (room-level column overrides — always last)
```

### The feedback loop

Every turn is persisted. Feedback signals (thumbs up/down) are stored as `feedback:{conv_id}:{turn_id}` keys. `FeedbackProposer` analyzes low-confidence turns and failed benchmarks and proposes additions to `text_instruction`, `examples`, and `sql_measures`. Proposals are human-reviewed before being applied — automatic room mutation without review is a correctness risk.

Benchmarks score on row count equality, not SQL string equality. A generated SQL that returns the correct rows with different aliases scores as a pass. A score of 3/5 where the two failures return valid results with a different interpretation is fundamentally different from a score of 3/5 where two queries return errors.

---

## Extensions summary

All eight core extensions are implemented:

| Extension | What it adds | Key invariant |
|---|---|---|
| EXT-1: Multi-query reasoning | PlanningAgent + SynthesisAgent — 1–5 step plans | Single-step path identical to pre-EXT-1 |
| EXT-2: Dynamic table selection | Wildcard patterns in `RoomConfig.tables`; semantic similarity selection per question | Join-spec tables always included regardless of rank |
| EXT-3: Multi-model routing | Named provider registry; per-task model assignment | `RouterLLMProvider` always returned; agents never see routing |
| EXT-4: MCP server exposure | Tiri as MCP server at `/mcp` — `tiri_query`, `tiri_list_rooms`, `tiri_room_schema` | Auth failures return HTTP 200 + JSON-RPC -32001, not HTTP 401 |
| EXT-5: MCP tool consumption | Agents call authorized external MCP servers mid-pipeline | `RoomConfig.mcp_servers` is the authorization list; empty = zero calls |
| EXT-6: Per-user credentials | `user_token` from request threads to both `validate()` and `execute()` | validate() must use user_token — not just execute() |
| EXT-7: Explicit uncertainty | Every synthesized answer has confidence + data_supports + data_does_not_support | Causal language raises, never ships |
| EXT-11: Hypothesis mode | Provisional candidate explanations for observed patterns | `confidence` always `"low"` — type-system invariant, not a default |

---

## Validation results

Benchmarked against TPC-H (`samples.tpch.*` on Databricks) across five LLM backends:

| Backend | tpch-sales | tpch-supply |
|---|---|---|
| Databricks llama-3-3-70b | 5/5 | 3/5 |
| Ollama qwen2.5-coder:14b | 5/5 | 3/5 |
| Anthropic Sonnet 4.6 (direct) | 4/5 | 2/5 |
| Claude Opus 4.7 (via Databricks) | 5/5 | 3/5 |
| Databricks Sonnet 4.6 | 4/5 | 2/5 |

The two persistent supply failures are confirmed irreducible semantic gaps in the benchmark questions — not engine bugs. Both questions are genuinely underspecified (compound ranking criterion and implicit result limit). Every model tested returns a valid, useful answer. The benchmark scores them as failures because the row counts don't match the expected SQL.

**426 unit tests, 3 integration tests** (require `INTEGRATION_TESTS=true` + live workspace). Full suite runs in ~3s.

Five real bugs were found and fixed during live validation:
1. `RoomConfig.from_dict` missing-kind fallback for snippets without explicit `kind`
2. `Config._from_toml` env fallthrough for Databricks LLM backends
3. `VizAgent` graceful degrade on LLM failure (guardrail false-positive on geopolitical row data)
4. `SQLAgent` markdown fence stripping (qwen wraps SQL in fences despite prompt instructions)
5. `AnthropicLLMProvider` SDK compatibility — `system=None` rejected by SDK ≥0.50; empty messages array rejected by Anthropic API

---

## Repository layout

```
tiri/engine/          agents, room_engine — zero I/O, zero SDK imports
tiri/knowledge/       context assembly — metadata, examples, table selection, MCP resolution
tiri/providers/       ABCs + implementations (databricks/, local/)
tiri/api/             FastAPI — REST routes, SSE, MCP server, auth
tiri/feedback/        collector, proposer, benchmark runner
tiri/cli.py           load-room, ask, benchmark, serve, import-genie
docs/                 18 architecture documents — start with vision.md, then README.md
demo/                 TPC-H sales + supply room configs with benchmarks
deploy/               app.yaml + tiri.toml.production for Databricks Apps deployment
```

---

## Where to read next

| Goal | Document |
|---|---|
| Understand *why* Tiri is built this way | `docs/vision.md` |
| Navigate the full system | `docs/README.md` (system map) |
| Understand how Tiri relates to Genie | `docs/concept_map.md` |
| Configure a deployment | `docs/configuration.md` |
| Improve a room's benchmark score | `docs/tuning.md` |
| Understand each extension | `docs/extensions.md` |
| Deploy to Databricks Apps | `deploy/README.md` |
| Open issues | `fixme.md` |
| Future capabilities | `docs/roadmap.md` |
