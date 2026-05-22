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

A precise mapping of every Genie concept to its Tiri equivalent, the underlying Databricks component it uses, and — where Tiri differs — why. Use this document to:

- Understand how Genie works by analogy to concepts you already know from this project
- Understand exactly how Tiri extends, replaces, or reimagines each Genie concept
- Know which Databricks service each piece of the system actually calls at runtime

Read the column headers carefully. There are five:

| Column | Meaning |
|---|---|
| **Genie concept** | What Genie calls this thing or how Genie handles it |
| **Databricks service** | The concrete AWS/Azure/GCP service or API Genie uses underneath |
| **Tiri equivalent** | The corresponding Tiri component or design |
| **Tiri service** | The concrete service Tiri uses by default (may differ from Genie) |
| **Tiri status** | `parity` = same capability · `extension` = meaningfully beyond Genie · `replaced` = different approach entirely |

---

## Layer 1 — The model (LLM)

### What Genie does
Genie uses a single Databricks-hosted LLM, selected and managed by Databricks. You cannot change it, configure it, or route different tasks to different models. It is not exposed as a settable parameter anywhere in the API.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Single locked LLM | Databricks Model Serving (internal, not user-facing) | `LLMProvider` ABC + `RouterLLMProvider` | Any Model Serving endpoint, OpenAI, Anthropic, or Ollama | **extension** |
| No model choice | — | `tiri.toml` `[llm.providers]` registry | User-configured per backend | **extension** |
| No per-task routing | — | `[llm.routing]` in `tiri.toml` | Per-task model assignment (intent / sql / synthesis / embed) | **extension** |
| No embedding configuration | Databricks-internal embedding | `embed` route in `RouterLLMProvider` | Separate, configurable embedding endpoint | **extension** |

### What this means in practice
When Genie classifies your intent and generates SQL, it uses the same model for both. Tiri can use a fast 8B model for classification (cheap, low latency) and a 70B reasoning model for SQL generation (expensive, high quality) — or route SQL to OpenAI and synthesis to Anthropic, all from the same room. The `RouterLLMProvider` is the implementation. See [[extensions]] EXT-3 and [[configuration]].

---

## Layer 2 — The data (tables and schema)

### What Genie does
Genie requires all tables to be registered in Unity Catalog. You select up to 30 tables when configuring a Space. All 30 schemas are loaded into every prompt for every question. The 30-table limit is hard — Databricks' own documentation recommends staying under 5 for best results.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Table selection (up to 30) | Unity Catalog | `RoomConfig.tables` + `TableSelector` (EXT-2) | UC + `CatalogProvider` | **extension** |
| Hard 30-table cap | UC API limit | No cap — dynamic selection per question (EXT-2) | `CatalogProvider.list_tables()` + vector similarity | **extension** |
| UC-only data source | Unity Catalog exclusively | `CatalogProvider` ABC — any catalog | UC, Hive, static file, DuckDB | **extension** |
| Schema loaded per-prompt | UC `TableInfo` API | `MetadataFetcher` + metadata stack | `CatalogProvider` + `MetadataProvider` stack | **replaced** |
| Column comments from UC | UC column metadata | Layer 2 of metadata stack | `UCAnnotationsMetadataProvider` | **parity** |
| No external metadata | — | `MetadataProvider` ABC + ordered stack | YAML, Delta table, dbt, OpenMetadata | **extension** |
| No column synonyms | — | `ColumnMeta.synonyms` (accumulated across stack) | Any `MetadataProvider` | **extension** |
| No semantic typing | — | `ColumnMeta.semantic_type` (date/currency/category/measure) | Any `MetadataProvider` | **extension** |
| No grain description | — | `TableMeta.grain` ("one row per order") | Any `MetadataProvider` | **extension** |
| No default filters | — | `TableMeta.default_filter` | `RoomConfig` or `MetadataProvider` | **extension** |
| No conflict detection | — | `TableMeta.conflicts: list[MetadataConflict]` | `MetadataFetcher` merge pass | **extension** |

### What this means in practice
Genie reads UC and that is the end of the story. Tiri treats schema retrieval and semantic enrichment as two separate concerns handled by two separate provider types. `CatalogProvider` knows what columns exist. `MetadataProvider` knows what they mean. You can stack as many metadata sources as you need — UC annotations → a domain YAML your team maintains → a Delta table your data governance tooling writes to → room-level overrides — and the merge rules (scalar: last-writer-wins; lists: accumulate) handle conflicts predictably. See [[metadata]] for the full stack design.

---

## Layer 3 — The knowledge store

### What Genie does
The knowledge store is a structured JSON document embedded in the Space configuration. It contains: text instructions, example SQL queries, JOIN specifications, SQL filters, SQL expressions, SQL measures, and sample questions. It is edited via the UI or the Management API. It is the primary mechanism for teaching Genie about your business.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Text instructions | `serialized_space.instructions.text_instructions` (max 1 item) | `RoomConfig.text_instruction` | `StoreProvider` (Delta table) | **parity** |
| Example SQL queries | `example_question_sqls` list | `RoomConfig.examples: list[ExampleSQL]` | `StoreProvider` + vector-indexed via `VectorProvider` | **extension** |
| Example retrieval | None — all examples injected into every prompt | `ExampleIndexer.retrieve()` — top-k by semantic similarity | Databricks Vector Search | **extension** |
| JOIN specifications | `join_specs` with magic comment for relationship type | `RoomConfig.joins: list[JoinSpec]` with clean enum | `StoreProvider` | **parity** (cleaner schema) |
| SQL filters | `sql_snippets.filters` | `RoomConfig.sql_filters: list[SqlSnippet]` | `StoreProvider` | **parity** |
| SQL expressions | `sql_snippets.expressions` | `RoomConfig.sql_expressions: list[SqlSnippet]` | `StoreProvider` | **parity** |
| SQL measures | `sql_snippets.measures` (added 2025) | `RoomConfig.sql_measures: list[SqlSnippet]` | `StoreProvider` | **parity** |
| Sample questions | `config.sample_questions` | `RoomConfig.sample_questions` | `StoreProvider` | **parity** |
| 200 snippet limit | Hard cap in Genie | No limit | — | **extension** |
| Column descriptions (local) | Space-local column overrides | `RoomConfig.column_overrides: list[ColumnOverride]` | `StoreProvider` (applied by `RoomConfigMetadataProvider`) | **extension** |
| No synonyms on columns | — | `ColumnMeta.synonyms` in column overrides and metadata stack | `MetadataProvider` | **extension** |
| Knowledge store as config file | `serialized_space` JSON string (escaped) | `RoomConfig` dataclass → JSON → `StoreProvider` | Delta KV table | **replaced** (cleaner) |
| Programmatic update (PATCH + etag) | `PATCH /api/2.0/genie/spaces/{id}` | `PATCH /rooms/{id}` management API | Same pattern, cleaner schema | **parity** |

### What this means in practice
The Genie knowledge store and Tiri's `RoomConfig` are conceptually equivalent — both teach the system about your data and business terms. The key differences: Tiri vector-indexes examples so only the most relevant ones are injected per question (Genie injects all of them, which hurts quality at scale); Tiri has no 200-snippet cap; the schema is a proper Python dataclass rather than an escaped JSON string; and column-level metadata is a first-class concept that participates in the metadata stack rather than being a local override bolted on.

---

## Layer 4 — The agents (compound AI)

### What Genie does
Genie uses what Databricks calls a "compound AI system" — multiple LLM calls in sequence, each with a focused prompt. The internal agent pipeline is not publicly documented, but from behavior and release notes it includes: intent detection, SQL generation, clarification generation, and visualization selection. The pipeline is managed by Databricks and runs as configured.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Intent detection agent | Internal (undocumented) | `IntentAgent` | `LLMProvider` (intent route — fast model) | **parity** (inspectable) |
| SQL generation agent | Internal | `SQLAgent` with self-correction loop | `LLMProvider` (sql route) | **extension** (self-corrects) |
| Clarification agent | Internal | `ClarifyAgent` | `LLMProvider` (clarify route — fast model) | **parity** (inspectable) |
| Visualization agent | Internal | `VizAgent` (rule-based + one LLM call for summary) | `LLMProvider` (viz_summary route) + Python Vega-Lite builder | **replaced** (more reliable) |
| No planning agent | — | `PlanningAgent` (EXT-1) | `LLMProvider` (planning route — reasoning model) | **extension** |
| No synthesis agent | — | `SynthesisAgent` (EXT-1) | `LLMProvider` (synthesis route) | **extension** |
| Black-box pipeline | No visibility | `RoomEngine` — fully inspectable, every step logged | `StoreProvider` | **replaced** |
| No streaming reasoning trace | Partial (thinking steps added 2025) | `stream_chat()` SSE events per pipeline stage | FastAPI SSE | **extension** |
| `ContextPackage` | Not a public concept | `ContextPackage` dataclass — the assembled prompt context | Built by `ContextBuilder` | **extension** (explicit) |
| SQL EXPLAIN validation | Internal | `QueryProvider.validate()` before every `execute()` | Databricks SQL Warehouse EXPLAIN | **parity** (enforced as invariant) |

### What this means in practice
Genie's compound AI pipeline runs as a managed service. Tiri's pipeline is a first-class inspectable system — every agent has a typed input, a typed output, a named prompt template file, and test cases. `PlanningAgent` and `SynthesisAgent` (EXT-1) are the biggest additions: they enable multi-step reasoning across several queries. The `VizAgent` approach also differs — Tiri builds Vega-Lite specs programmatically and only calls an LLM for the one-sentence summary, which produces more consistent output.

---

## Layer 5 — SQL execution

### What Genie does
Genie executes SQL against a Databricks SQL Warehouse (Pro or Serverless required). The warehouse credentials are embedded in the Space configuration — all users execute as the room creator. This is the appropriate default for many deployments but does not apply per-user Unity Catalog row-level security.

Genie has a limited OAuth passthrough capability on Serverless SQL Warehouses in certain workspace configurations (added 2025). When enabled, queries execute using the end user's OAuth token rather than the service principal. This is not the default, is restricted to Serverless warehouses, and the configuration surface is limited. It does not cover Pro warehouses.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| SQL Warehouse execution | Databricks SQL Statement Execution API | `QueryProvider` ABC → `DatabricksQueryProvider` | SQL Statement Execution API | **parity** |
| Embedded credentials (room creator) | Hardcoded in Space config | `user_token` pass-through (EXT-6) | Per-request Bearer token → Statement Execution API | **extension** |
| Limited OBO on Serverless only | OAuth passthrough — Serverless + specific workspace config | EXT-6 works on both Serverless and Pro warehouses | `DatabricksQueryProvider` header swap | **extension** |
| UC row-level security bypassed | — | EXT-6 enforces per-user execution | UC column masking + row filters apply | **extension** |
| SQL EXPLAIN dry-run | Internal | `QueryProvider.validate()` — always before `execute()` | SQL Warehouse EXPLAIN | **parity** |
| 10,000 row result cap (default) | Internal limit | `QUERY_ROW_LIMIT` configurable, default 10,000 | Statement Execution API `limit` parameter | **parity** (configurable) |
| Serverless or Pro warehouse required | Databricks limitation | Same requirement for Databricks impl; DuckDB for local | DuckDB (`DuckDBQueryProvider`) for dev | **extension** |
| No non-SQL execution | Genie = SQL only | `QueryProvider` is one tool; agents can call MCP tools (EXT-5) | MCP servers via `MCPProvider` | **extension** |

### What this means in practice
SQL execution is functionally the same — both use the Statement Execution API. EXT-6 adds per-user token pass-through so that Unity Catalog column masking and row-level filters apply correctly per user, and it works across both Serverless and Pro warehouses. Genie's OBO capability is restricted to Serverless in specific configurations and is not the default.

---

## Layer 6 — Persistence and state

### What Genie does
Genie persists Space configuration as a serialized JSON string in Databricks-managed storage (not directly accessible). Conversation history is stored internally. Feedback signals are stored internally. None of this is in a user-accessible location.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Space config storage | Databricks-internal | `StoreProvider` → `DatabricksStoreProvider` | Delta table (`main.tiri.kv_store`) | **replaced** (user-accessible) |
| Conversation history | Databricks-internal | `conv:{id}:turn:{id}` keys in `StoreProvider` | Delta table | **replaced** (user-accessible) |
| Room→conversation index | — | `room:{id}:conversations` key | Delta table | **extension** |
| Feedback signal storage | Databricks-internal | `feedback:{conv_id}:{turn_id}` keys | Delta table | **replaced** (user-accessible) |
| Example vector index | Databricks Vector Search (internal) | `VectorProvider` → `DatabricksVectorProvider` | Databricks Vector Search (Direct Access) | **parity** |
| No local dev storage | Requires Databricks | `SQLiteStoreProvider` + `ChromaVectorProvider` | SQLite + ChromaDB | **extension** |

### What this means in practice
Genie persists configuration and conversation data in Databricks-managed storage, accessible through the Management API. Tiri stores everything in user-accessible Delta tables and Vector Search indexes that you own — queryable with SQL, portable, and directly inspectable. This is useful for compliance, auditability, and workspace migration scenarios.

---

## Layer 7 — Retrieval (vector search)

### What Genie does
Genie uses vector search internally for a feature called "prompt matching" — it matches user question terms to column values to correct spelling and match categorical values. This is not the same as vector-indexing example SQL queries.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Prompt matching (value matching) | Databricks Vector Search (internal) | `ColumnMeta.sample_values` injected into prompt | `UCAnnotationsMetadataProvider` or `YAMLMetadataProvider` | **parity** (different mechanism) |
| No example SQL vector index | — | `ExampleIndexer` — embeds questions, retrieves top-k | Databricks Vector Search (Direct Access) | **extension** |
| No semantic table search | — | `TableSelector` (EXT-2) — semantic similarity over table names+descriptions | Databricks Vector Search | **extension** |

### What this means in practice
Genie uses vector search narrowly — for value matching to handle typos and categorical lookups. Tiri uses it for something more important: retrieving the right few-shot examples. A room can have 100 example SQL queries, but only the 5 most semantically similar to the current question are injected into the SQL generation prompt. This is why Tiri can scale to large example sets without degrading quality — the retrieval step does the filtering.

---

## Layer 8 — Feedback and improvement

### What Genie does
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
| No workspace query harvesting | — | EXT-9 (planned) — harvests existing workspace queries as candidate examples | Databricks SQL History API | **extension** |
| Auto-suggested benchmarks (added 2025) | Genie UI | Not yet designed | — | planned |

### What this means in practice
The feedback loop is conceptually equivalent. The difference is data ownership: Tiri's feedback data lives in a Delta table you own — queryable with SQL, portable, and auditable. You can run `SELECT * FROM main.tiri.kv_store WHERE key LIKE 'feedback:%'` to see every feedback signal ever recorded.

---

## Layer 9 — API surface

### What Genie does
Genie exposes two API types: a Conversation API (ask questions, get answers, stateful) and a Management API (create/update/delete Spaces, trigger indexing). Both use the Genie REST API at `/api/2.0/genie/spaces/...`. Rate limits apply: 20 QPM via UI, 5 QPM via API (Public Preview as of 2025).

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| Conversation API | `POST /api/2.0/genie/spaces/{id}/start-conversation` | `POST /rooms/{id}/conversations/{cid}/messages` | FastAPI | **parity** |
| Stateful multi-turn | Internal conversation state | `conv:{id}:index` + history in `ContextPackage` | `StoreProvider` | **parity** |
| Management API (CRUD) | `POST/GET/PATCH /api/2.0/genie/spaces/...` | `POST/GET/PATCH/DELETE /rooms/...` | FastAPI | **parity** |
| SSE streaming | Not available in standard Genie | `GET /rooms/{id}/conversations/{cid}/messages/stream` | FastAPI SSE | **extension** |
| 20 QPM rate limit | Databricks workspace limit | No platform rate limit — limited by your warehouse and LLM | LLM endpoint + SQL Warehouse | **extension** |
| 5 QPM API rate limit | Databricks API tier limit | No rate limit on own deployment | — | **extension** |
| MCP server exposure | Genie exposed as MCP via AI Gateway (managed) | EXT-4 — Tiri exposes itself as MCP server | `fastapi-mcp` adapter | **extension** |
| No MCP tool consumption | Genie Code only (not standard Spaces) | EXT-5 — agents can call external MCP tools mid-pipeline | `MCPProvider` ABC | **extension** |

### What this means in practice
The API surface is broadly equivalent. As a self-deployed platform, Tiri's throughput is limited only by the SQL Warehouse concurrency and the LLM endpoint rate limits you configure. The SSE streaming endpoint adds progressive disclosure — users see the SQL being generated and the query running rather than waiting for a blocking response.

---

## Layer 10 — Configuration and deployment

### What Genie does
Genie Spaces are configured via UI or the Management API. The entire configuration is one escaped JSON string (`serialized_space`). To update anything, you replace the whole string. There is no versioning, no Git integration, and no concept of environments (dev/staging/prod). The Management API is in Beta as of November 2025.

### The mapping

| Genie concept | Databricks service | Tiri equivalent | Tiri service | Tiri status |
|---|---|---|---|---|
| `serialized_space` JSON blob | Genie Management API | `RoomConfig` dataclass + JSON serialization | `StoreProvider` | **replaced** (typed, versioned) |
| UI-only config editing | Genie UI | `tiri.toml` + `PATCH /rooms/{id}` API | Any editor + REST | **extension** |
| No Git integration | — | Room config as JSON files in Git + `load-room` CLI | `tiri.cli` | **extension** |
| No environment concept | — | `tiri.toml` per environment + `${VAR}` substitution | Environment variables | **extension** |
| Single Databricks LLM only | — | `tiri.toml` provider registry — any LLM | OpenAI, Anthropic, Ollama, Databricks | **extension** |
| No local development | Requires Databricks workspace | Full local stack: DuckDB + ChromaDB + SQLite + Ollama | Local providers | **extension** |
| UC required for all data | Unity Catalog | `CatalogProvider` ABC — UC, Hive, static file, DuckDB | Any conforming impl | **extension** |

### What this means in practice
Genie Spaces are configured and managed through the Databricks UI and Management API. Tiri is a self-deployed platform: the `tiri.toml` provider registry, `RoomConfig` JSON files checked into Git, the `load-room` CLI command, and `${VAR}` substitution for secrets together enable a CI/CD workflow for room management.

---

## Summary: what Tiri is beyond Genie

### Feature parity (Tiri does what Genie does, often with a cleaner interface)

- Natural language to SQL (text-to-SQL)
- Intent classification, clarification, visualization
- Knowledge store: instructions, example SQL, JOIN specs, SQL filters/expressions/measures, sample questions
- Thumbs-up/down feedback loop with knowledge proposals
- Benchmarks with pass/fail scoring
- Conversation API (stateful, multi-turn)
- Management API (CRUD for rooms)
- UC metadata (column comments, sample values)

### Core extensions (in initial release)

| Extension | What it adds | Key doc |
|---|---|---|
| EXT-1: Multi-query reasoning | Plans and executes multiple SQL queries, synthesizes a prose answer with uncertainty | [[extensions]] |
| EXT-2: Dynamic table selection | Room can scope to a catalog; tables selected per-question by semantic similarity. No 30-table cap. | [[extensions]] |
| EXT-3: Multi-model routing | Named provider registry; different models for different tasks; mix vendors freely | [[extensions]], [[configuration]] |
| EXT-4: MCP server exposure | Tiri rooms are callable as MCP tools from Claude, Cursor, other agents | [[extensions]] |
| EXT-5: MCP tool consumption | Agents can call external MCP servers mid-pipeline (Confluence, Glean, APIs) | [[extensions]] |
| EXT-6: Per-user credentials | User's own token passed to SQL Warehouse; UC row-level security applies correctly | [[extensions]] |
| EXT-7: Explicit uncertainty | Every answer states what the data supports, what it does not, and confidence level | [[extensions]] |

### Platform extensions (beyond features)

| Capability | Genie | Tiri |
|---|---|---|
| Metadata sources | UC only | Stacked: UC → YAML → Delta table → dbt → OpenMetadata → room override |
| LLM vendors | Databricks only | Any: Databricks, OpenAI, Anthropic, Ollama |
| Storage | Databricks-internal | User-owned Delta tables (queryable, portable) |
| Local development | Not possible | Full local stack (DuckDB + SQLite + ChromaDB + Ollama) |
| Throughput | 20 QPM (UI), 5 QPM (API) | Limited only by your warehouse and LLM endpoint |
| Deployment | Hosted service | Self-deployed platform |
| CI/CD | Not supported | `tiri.toml` + Git + `load-room` CLI |
| Per-user security | Room creator credentials for all | Pass-through user token; UC security applies |
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
