---
tags: [reference, mapping]
status: stable
depends_on: []
---

# Concept map — Genie to Tiri

## In this system

**Linked from:** [[README]]
**Links to:** [[vision]], [[providers]], [[agents]], [[room_engine]], [[knowledge_store]], [[metadata]], [[extensions]], [[configuration]], [[data_models]], [[feedback]]
**Layer:** reference

---

## What this is

A precise mapping of Genie concepts to their Tiri equivalents, the underlying Databricks services each uses, and — where Tiri differs — the scenario that motivates the difference. Use this document to:

- Understand how Genie and Tiri relate to each other and to the Databricks platform
- Understand which scenarios call for Tiri's additional capabilities vs. Genie's built-in behavior
- Know which Databricks service each piece of the system actually calls at runtime

Read the column headers carefully. There are five:

| Column | Meaning |
|---|---|
| **Genie concept** | What Genie calls this thing or how Genie handles it |
| **Databricks service** | The concrete AWS/Azure/GCP service or API Genie uses underneath |
| **Tiri equivalent** | The corresponding Tiri component or design |
| **Tiri service** | The concrete service Tiri uses by default (may differ from Genie) |
| **Tiri status** | `parity` = same capability · `extension` = additional capability for specific scenarios · `different` = different approach to the same problem |

---

## Layer 1 — The model (LLM)

### Standard Genie behavior
Genie uses a single Databricks-hosted LLM, selected and managed by Databricks. It is not configurable or exposed as a settable parameter.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Single Databricks-hosted LLM | Databricks Model Serving (internal, not user-facing) | `LLMProvider` ABC + `RouterLLMProvider` | Any Model Serving endpoint, OpenAI, Anthropic, or Ollama | **extension** |
| No model choice | — | `tiri.toml` `[llm.providers]` registry | User-configured per backend | **extension** |
| No per-task routing | — | `[llm.routing]` in `tiri.toml` | Per-task model assignment (intent / sql / synthesis / embed) | **extension** |
| No embedding configuration | Databricks-internal embedding | `embed` route in `RouterLLMProvider` | Separate, configurable embedding endpoint | **extension** |

### When this matters
For most deployments Genie's hosted LLM is sufficient. This extension is relevant when integration requirements call for a specific vendor, BYO model, or per-task cost optimization. See [[extensions]] EXT-3 and [[configuration]].

---

## Layer 2 — The data (tables and schema)

### Standard Genie behavior
Genie requires all tables to be registered in Unity Catalog. You select up to 30 tables when configuring a Space.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Table selection (up to 30) | Unity Catalog | `RoomConfig.tables` + `TableSelector` (EXT-2) | UC + `CatalogProvider` | **extension** |
| Dynamic table selection per question | — | `TableSelector` (EXT-2) — semantic similarity per question | `CatalogProvider.list_tables()` + vector similarity | **extension** |
| UC catalog | Unity Catalog | `CatalogProvider` ABC — any catalog | UC, Hive, static file, DuckDB | **extension** |
| Schema loaded per-prompt | UC `TableInfo` API | `MetadataFetcher` + metadata stack | `CatalogProvider` + `MetadataProvider` stack | **different** |
| Column comments from UC | UC column metadata | Layer 2 of metadata stack | `UCAnnotationsMetadataProvider` | **parity** |
| External metadata sources | — | `MetadataProvider` ABC + ordered stack | YAML, Delta table, dbt, OpenMetadata | **extension** |
| Column synonyms | — | `ColumnMeta.synonyms` (accumulated across stack) | Any `MetadataProvider` | **extension** |
| Semantic column typing | — | `ColumnMeta.semantic_type` (date/currency/category/measure) | Any `MetadataProvider` | **extension** |
| Table grain description | — | `TableMeta.grain` ("one row per order") | Any `MetadataProvider` | **extension** |
| Table-level default filters | — | `TableMeta.default_filter` | `RoomConfig` or `MetadataProvider` | **extension** |
| Metadata conflict tracking | — | `TableMeta.conflicts: list[MetadataConflict]` | `MetadataFetcher` merge pass | **extension** |

### When this matters
For most deployments UC annotations are sufficient. When integration requirements call for stacking additional metadata sources — domain YAML, Delta tables, dbt manifests — Tiri's layered `MetadataProvider` stack handles the merge predictably (scalar: last-writer-wins; lists: accumulate). See [[metadata]] for the full stack design.

---

## Layer 3 — The knowledge store

### Standard Genie behavior
The knowledge store is a structured JSON document embedded in the Space configuration. It contains: text instructions, example SQL queries, JOIN specifications, SQL filters, SQL expressions, SQL measures, and sample questions. It is edited via the UI or the Management API. It is the primary mechanism for teaching Genie about your business.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Text instructions | `serialized_space.instructions.text_instructions` (max 1 item) | `RoomConfig.text_instruction` | `StoreProvider` (Delta table) | **parity** |
| Example SQL queries | `example_question_sqls` list | `RoomConfig.examples: list[ExampleSQL]` | `StoreProvider` + vector-indexed via `VectorProvider` | **extension** |
| Example retrieval | All examples injected per prompt | `ExampleIndexer.retrieve()` — top-k by semantic similarity | Databricks Vector Search | **extension** |
| JOIN specifications | `join_specs` with relationship type comment | `RoomConfig.joins: list[JoinSpec]` with typed enum | `StoreProvider` | **parity** |
| SQL filters | `sql_snippets.filters` | `RoomConfig.sql_filters: list[SqlSnippet]` | `StoreProvider` | **parity** |
| SQL expressions | `sql_snippets.expressions` | `RoomConfig.sql_expressions: list[SqlSnippet]` | `StoreProvider` | **parity** |
| SQL measures | `sql_snippets.measures` (added 2025) | `RoomConfig.sql_measures: list[SqlSnippet]` | `StoreProvider` | **parity** |
| Sample questions | `config.sample_questions` | `RoomConfig.sample_questions` | `StoreProvider` | **parity** |
| Snippet count | Up to 200 snippets | No limit | — | **extension** |
| Column descriptions (local) | Space-local column overrides | `RoomConfig.column_overrides: list[ColumnOverride]` | `StoreProvider` (applied by `RoomConfigMetadataProvider`) | **extension** |
| Column synonyms | — | `ColumnMeta.synonyms` in column overrides and metadata stack | `MetadataProvider` | **extension** |
| Knowledge store as config file | `serialized_space` JSON string (escaped) | `RoomConfig` dataclass → JSON → `StoreProvider` | Delta KV table | **different** |
| Programmatic update (PATCH + etag) | `PATCH /api/2.0/genie/spaces/{id}` | `PATCH /rooms/{id}` management API | Same pattern, cleaner schema | **parity** |

### When this matters
The Genie knowledge store and Tiri's `RoomConfig` are conceptually equivalent. Tiri adds vector-indexed example retrieval (top-k per question rather than all examples per prompt), no snippet count limit, and a typed dataclass schema for programmatic management.

---

## Layer 4 — The agents (compound AI)

### Standard Genie behavior
Genie uses what Databricks calls a "compound AI system" — multiple LLM calls in sequence, each with a focused prompt. The internal agent pipeline is not publicly documented, but from behavior and release notes it includes: intent detection, SQL generation, clarification generation, and visualization selection. The pipeline is managed by Databricks and runs as configured.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Intent detection agent | Managed by platform | `IntentAgent` | `LLMProvider` (intent route — fast model) | **parity** |
| SQL generation agent | Managed by platform | `SQLAgent` with self-correction loop | `LLMProvider` (sql route) | **extension** |
| Clarification agent | Managed by platform | `ClarifyAgent` | `LLMProvider` (clarify route — fast model) | **parity** |
| Visualization agent | Managed by platform | `VizAgent` (rule-based + one LLM call for summary) | `LLMProvider` (viz_summary route) + Python Vega-Lite builder | **different** |
| Planning agent | — | `PlanningAgent` (EXT-1) | `LLMProvider` (planning route — reasoning model) | **extension** |
| Synthesis agent | — | `SynthesisAgent` (EXT-1) | `LLMProvider` (synthesis route) | **extension** |
| Pipeline visibility | Managed by platform | `RoomEngine` — fully inspectable, every step logged | `StoreProvider` | **different** |
| Streaming reasoning trace | Thinking steps (added 2025) | `stream_chat()` SSE events per pipeline stage | FastAPI SSE | **extension** |
| `ContextPackage` | Platform-internal concept | `ContextPackage` dataclass — the assembled prompt context | Built by `ContextBuilder` | **extension** |
| SQL EXPLAIN validation | Internal | `QueryProvider.validate()` before every `execute()` | Databricks SQL Warehouse EXPLAIN | **parity** (enforced as invariant) |

### When this matters
Genie's pipeline is a managed service — reliable and well-maintained. Tiri's pipeline is a self-deployed, fully inspectable system where every agent has a typed input, typed output, and named prompt template. `PlanningAgent` and `SynthesisAgent` (EXT-1) add multi-step reasoning for scenarios that require it.

---

## Layer 5 — SQL execution

### Standard Genie behavior
Genie executes SQL against a Databricks SQL Warehouse (Pro or Serverless required). The warehouse credentials are embedded in the Space configuration — all users execute as the room creator. This is the appropriate default for many deployments but does not apply per-user Unity Catalog row-level security.

Genie has a limited OAuth passthrough capability on Serverless SQL Warehouses in certain workspace configurations (added 2025). When enabled, queries execute using the end user's OAuth token rather than the service principal. This is not the default, is restricted to Serverless warehouses, and the configuration surface is limited. It does not cover Pro warehouses.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| SQL Warehouse execution | Databricks SQL Statement Execution API | `QueryProvider` ABC → `DatabricksQueryProvider` | SQL Statement Execution API | **parity** |
| Room creator credentials (default) | Space config credentials | `user_token` pass-through (EXT-6) | Per-request Bearer token → Statement Execution API | **extension** |
| OAuth passthrough (Serverless, specific configs) | OAuth passthrough — Serverless + specific workspace config | EXT-6 works on both Serverless and Pro warehouses | `DatabricksQueryProvider` header swap | **extension** |
| Per-user UC enforcement | UC applies when OBO is enabled | EXT-6 passes user token to all warehouse calls | UC column masking + row filters apply per user | **extension** |
| SQL EXPLAIN dry-run | Internal | `QueryProvider.validate()` — always before `execute()` | SQL Warehouse EXPLAIN | **parity** |
| 10,000 row result cap (default) | Internal limit | `QUERY_ROW_LIMIT` configurable, default 10,000 | Statement Execution API `limit` parameter | **parity** (configurable) |
| Serverless or Pro warehouse required | Databricks limitation | Same requirement for Databricks impl; DuckDB for local | DuckDB (`DuckDBQueryProvider`) for dev | **extension** |
| No non-SQL execution | Genie = SQL only | `QueryProvider` is one tool; agents can call MCP tools (EXT-5) | MCP servers via `MCPProvider` | **extension** |

### When this matters
SQL execution is functionally the same — both use the Statement Execution API. EXT-6 is relevant when per-user UC enforcement is needed across both Serverless and Pro warehouses, or when the deployment requires explicit token pass-through as part of integration requirements.

---

## Layer 6 — Persistence and state

### Standard Genie behavior
Genie persists Space configuration, conversation history, and feedback signals in Databricks-managed storage, accessible through the Management API.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Space config storage | Databricks-internal | `StoreProvider` → `DatabricksStoreProvider` | Delta table (`main.tiri.kv_store`) | **different** |
| Conversation history | Databricks-internal | `conv:{id}:turn:{id}` keys in `StoreProvider` | Delta table | **different** |
| Room→conversation index | — | `room:{id}:conversations` key | Delta table | **extension** |
| Feedback signal storage | Databricks-internal | `feedback:{conv_id}:{turn_id}` keys | Delta table | **different** |
| Example vector index | Databricks Vector Search (internal) | `VectorProvider` → `DatabricksVectorProvider` | Databricks Vector Search (Direct Access) | **parity** |
| No local dev storage | Requires Databricks | `SQLiteStoreProvider` + `ChromaVectorProvider` | SQLite + ChromaDB | **extension** |

### When this matters
For most deployments Genie's managed storage is sufficient. Tiri's user-owned Delta tables are useful when integration requirements call for direct SQL queryability of conversation history, compliance auditing, or portability across workspaces.

---

## Layer 7 — Retrieval (vector search)

### Standard Genie behavior
Genie uses vector search internally for a feature called "prompt matching" — it matches user question terms to column values to correct spelling and match categorical values. This is not the same as vector-indexing example SQL queries.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Prompt matching (value matching) | Databricks Vector Search (internal) | `ColumnMeta.sample_values` injected into prompt | `UCAnnotationsMetadataProvider` or `YAMLMetadataProvider` | **parity** (different mechanism) |
| No example SQL vector index | — | `ExampleIndexer` — embeds questions, retrieves top-k | Databricks Vector Search (Direct Access) | **extension** |
| No semantic table search | — | `TableSelector` (EXT-2) — semantic similarity over table names+descriptions | Databricks Vector Search | **extension** |

### When this matters
Genie's prompt matching handles categorical value lookups effectively. Tiri additionally vector-indexes example SQL queries so only the top-k most semantically relevant are injected per question — useful when a room has a large example set and prompt size is a concern.

---

## Layer 8 — Feedback and improvement

### Standard Genie behavior
Genie has a thumbs-up/down UI. Thumbs-up turns are analyzed and proposed as new knowledge snippets for admin review. Admins can also mark generated SQL as correct or incorrect to create benchmarks. This is accessible via UI and partially via API (feedback endpoints are in Public Preview as of 2025).

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Thumbs up/down | Genie UI + Conversation API | `Collector.record()` → `feedback:` store keys | `StoreProvider` (Delta table) | **parity** |
| Knowledge proposal from feedback | Genie UI workflow | `Proposer.propose()` → returns candidates for admin review | `LLMProvider` + `StoreProvider` | **parity** |
| Admin review + approval | Genie UI | `POST /rooms/{id}/feedback/propose` → admin calls `PATCH /rooms/{id}` | REST API | **parity** |
| Benchmarks (question/SQL pairs) | Genie UI + API | `RoomConfig.benchmarks: list[Benchmark]` | `StoreProvider` | **parity** |
| Benchmark scoring | Genie UI | `BenchmarkRunner.run()` → `BenchmarkReport` | `RoomEngine.chat()` | **parity** |
| Benchmark score explanation (added 2025) | Genie UI | `BenchmarkResult.error` field | — | **parity** |
| Workspace query harvesting | — | EXT-9 (planned) — harvests existing workspace queries as candidate examples | Databricks SQL History API | **extension** |
| Auto-suggested benchmarks (added 2025) | Genie UI | Not yet designed | — | planned |

### When this matters
The feedback loop is conceptually equivalent. The difference is data ownership: Tiri's feedback data lives in a Delta table you own — queryable with SQL, portable, and auditable. You can run `SELECT * FROM main.tiri.kv_store WHERE key LIKE 'feedback:%'` to see every feedback signal ever recorded.

---

## Layer 9 — API surface

### Standard Genie behavior
Genie exposes two API types: a Conversation API (ask questions, get answers, stateful) and a Management API (create/update/delete Spaces, trigger indexing). Both use the Genie REST API at `/api/2.0/genie/spaces/...`. Rate limits apply: 20 QPM via UI, 5 QPM via API (Public Preview as of 2025).

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Conversation API | `POST /api/2.0/genie/spaces/{id}/start-conversation` | `POST /rooms/{id}/conversations/{cid}/messages` | FastAPI | **parity** |
| Stateful multi-turn | Internal conversation state | `conv:{id}:index` + history in `ContextPackage` | `StoreProvider` | **parity** |
| Management API (CRUD) | `POST/GET/PATCH /api/2.0/genie/spaces/...` | `POST/GET/PATCH/DELETE /rooms/...` | FastAPI | **parity** |
| SSE streaming | Not available in standard Genie | `GET /rooms/{id}/conversations/{cid}/messages/stream` | FastAPI SSE | **extension** |
| Throughput | 20 QPM (UI), 5 QPM (API) | Limited by warehouse and LLM endpoint on own deployment | LLM endpoint + SQL Warehouse | **different** |
| MCP server exposure | Genie exposed as MCP via AI Gateway (managed) | EXT-4 — Tiri exposes itself as MCP server | `fastapi-mcp` adapter | **extension** |
| MCP tool consumption | Available via Genie Code | EXT-5 — agents can call external MCP tools mid-pipeline | `MCPProvider` ABC | **extension** |

### When this matters
The API surface is broadly equivalent. As a self-deployed platform, Tiri's throughput is governed by the SQL Warehouse and LLM endpoint you configure. The SSE streaming endpoint adds progressive disclosure for integration scenarios that benefit from it.

---

## Layer 10 — Configuration and deployment

### Standard Genie behavior
Genie Spaces are configured via UI or the Management API. Configuration is stored as a JSON document in the Space. The Management API is in Beta as of November 2025.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| `serialized_space` JSON blob | Genie Management API | `RoomConfig` dataclass + JSON serialization | `StoreProvider` | **different** |
| UI + API config editing | Genie UI | `tiri.toml` + `PATCH /rooms/{id}` API | Any editor + REST | **parity** |
| Git-based room management | — | Room config as JSON files in Git + `load-room` CLI | `tiri.cli` | **extension** |
| Environment management | — | `tiri.toml` per environment + `${VAR}` substitution | Environment variables | **extension** |
| LLM vendor | Databricks-hosted | `tiri.toml` provider registry — any LLM | OpenAI, Anthropic, Ollama, Databricks | **extension** |
| Local development stack | — | Full local stack: DuckDB + ChromaDB + SQLite + Ollama | Local providers | **extension** |
| Catalog | Unity Catalog | `CatalogProvider` ABC — UC, Hive, static file, DuckDB | Any conforming impl | **extension** |

### When this matters
Genie Spaces are configured and managed through the Databricks UI and Management API. Tiri is a self-deployed platform: the `tiri.toml` provider registry, `RoomConfig` JSON files checked into Git, the `load-room` CLI command, and `${VAR}` substitution for secrets together enable a CI/CD workflow for room management.

---

## Summary: when to use Tiri alongside Genie

### Feature parity (Tiri covers the same ground as Genie, often with a cleaner interface)

- Natural language to SQL (text-to-SQL)
- Intent classification, clarification, visualization
- Knowledge store: instructions, example SQL, JOIN specs, SQL filters/expressions/measures, sample questions
- Thumbs-up/down feedback loop with knowledge proposals
- Benchmarks with pass/fail scoring
- Conversation API (stateful, multi-turn)
- Management API (CRUD for rooms)
- UC metadata (column comments, sample values)

### Additional capabilities (scenarios where Tiri is the right choice)

| Extension | What it addresses | Key doc |
|---|---|---|
| EXT-1: Multi-query reasoning | Questions requiring multiple queries, planning, and synthesis | [[extensions]] |
| EXT-2: Dynamic table selection | Rooms with large or wildcard table scopes; no 30-table cap | [[extensions]] |
| EXT-3: Multi-model routing | BYO LLM, vendor flexibility, cost-optimized routing per task | [[extensions]], [[configuration]] |
| EXT-4: MCP server exposure | Tiri rooms callable as MCP tools from Claude, Cursor, other agents | [[extensions]] |
| EXT-5: MCP tool consumption | Agents can call external MCP servers mid-pipeline | [[extensions]] |
| EXT-6: Per-user credentials | Per-user UC row-level security enforcement; Pro warehouse OBO | [[extensions]] |
| EXT-7: Explicit uncertainty | High-stakes audiences needing structured confidence and gap statements | [[extensions]] |

### Platform differences

| Capability | Genie | Tiri |
|---|---|---|
| Metadata sources | UC only | Stacked: UC → YAML → Delta table → dbt → OpenMetadata → room override |
| LLM vendors | Databricks-hosted | Any: Databricks, OpenAI, Anthropic, Ollama |
| Storage | Databricks-internal | User-owned Delta tables (queryable, portable) |
| Local development | Not possible | Full local stack (DuckDB + SQLite + ChromaDB + Ollama) |
| Throughput | 20 QPM (UI), 5 QPM (API) | Limited by your warehouse and LLM endpoint |
| Deployment | Hosted service | Self-deployed platform |
| CI/CD | Not supported | `tiri.toml` + Git + `load-room` CLI |
| Per-user security | Room creator credentials by default | Pass-through user token; UC security applies |
| Data ownership | Databricks-internal | Everything in your Delta tables |

### Planned extensions

| Extension | What it adds |
|---|---|
| EXT-8: Cross-room federation | Meta-room that routes to specialized sub-rooms; cross-domain answers |
| EXT-9: Workspace knowledge harvesting | Auto-propose example SQLs from workspace query history |
| EXT-10: Semantic layer integration | dbt metrics, UC metric views, Cube.dev as first-class inputs |
| EXT-11: Hypothesis mode | Candidate explanations for observed patterns — provisional, auditable, opt-in per room |

---

## Concrete Databricks service map

For completeness — every Databricks service Tiri uses and what it does:

| Databricks service | Tiri uses it for | Provider | Can be swapped with |
|---|---|---|---|
| **Model Serving** | LLM completions, streaming, embedding | `DatabricksLLMProvider` | OpenAI, Anthropic, Ollama |
| **Unity Catalog** | Physical table/column schema | `DatabricksCatalogProvider` | Static file, Hive, Glue |
| **Unity Catalog** | Table/column comments (metadata layer 2) | `UCAnnotationsMetadataProvider` | YAML, Delta table, dbt |
| **SQL Warehouse** | SQL execution + EXPLAIN validation | `DatabricksQueryProvider` | DuckDB (local dev) |
| **Vector Search** | Example SQL semantic retrieval; EXT-2 table search | `DatabricksVectorProvider` | ChromaDB, Pinecone |
| **Delta Lake** | KV store for room config, conversations, feedback | `DatabricksStoreProvider` | SQLite (local dev) |
| **Databricks SDK** | Catalog API calls (`client.tables.get()`) | `DatabricksCatalogProvider`, `UCAnnotationsMetadataProvider` | — |
| **SQL Statement Execution API** | Query execution, EXPLAIN | `DatabricksQueryProvider` | — |
| **AI Gateway (MCP)** | EXT-4: Tiri as MCP server (optional deployment) | MCP adapter on FastAPI | Any MCP-compatible host |

Nothing in this list is hardwired. Every Databricks service is behind a provider interface. The default is Databricks-on-Databricks. Swap any layer independently by changing `tiri.toml`.
