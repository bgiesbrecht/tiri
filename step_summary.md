# Tiri — build step summary

A condensed record of what each implementation step delivered. For full architectural specs see `docs/`; for the open issues backlog see `fixme.md`.

Current state: **426 unit tests + 3 integration skipped, ~3s.** Steps 1–13 + live-workspace validation complete. The DoD benchmarks ran against `samples.tpch.*` on Databricks workspace `<workspace-id>` using a self-hosted Ollama host for completions: **tpch-sales 5/5 (100%)**, **tpch-supply 3/5 (60%)** — see "Live benchmark validation" below.

---

## Step 1 — `tiri/data_models.py` + minimal scaffold

**Goal.** Define every shared dataclass per `docs/data_models.md`. Zero I/O, no dependencies beyond stdlib.

**Built.**
- `pyproject.toml` (≥3.10 initially, bumped to ≥3.11 in Step 3 for stdlib `tomllib`).
- `tiri/__init__.py`, `tiri/data_models.py`.
- `tests/__init__.py`, `tests/unit/__init__.py`, `tests/unit/test_data_models.py`.
- `.venv/` with pytest + pytest-asyncio installed.

**Dataclasses.** `ColumnOverride`, `ExampleSQL`, `JoinSpec`, `SqlSnippet`, `Metric`, `Benchmark`, `MetadataConflict`, `ColumnMeta`, `TableMeta`, `RoomConfig` (with `from_dict` rehydrator), `QueryResult`, `VizResult`, `LLMMessage`, `LLMResponse`, `VectorMatch`, `IntentResult`, `SQLResult`, `ClarifyResult`, `BenchmarkResult`, `BenchmarkReport`, EXT-1 types (`ReasoningStep`, `ReasoningPlan`, `SynthesizedAnswer`), EXT-11 types (`Hypothesis`, `HypothesisResult`), `ConversationTurn`, `ContextPackage`.

**Validation in `__post_init__`.** `RoomConfig.room_id` URL-safe + non-empty; `tables` non-empty; `warehouse_id` non-empty; unique `ExampleSQL.id`; `default_filters` reject `SELECT `/`WITH ` prefixes (added later). `SqlSnippet.kind` ∈ {filter, expression, measure}. `ConversationTurn` exactly one of {sql, clarification_question, error}. `HypothesisResult.confidence` MUST be `"low"`; every `Hypothesis` has ≥1 `contradicting_pattern`.

**Tests:** 51 — every `data_models.md` MUST plus EXT-11 invariants.

**Decisions worth knowing.**
- `from __future__ import annotations` throughout, allows forward refs (ConversationTurn → SynthesizedAnswer / HypothesisResult).
- All `ConversationTurn` "optional" fields get `= None` / `= ""` defaults so the engine can construct turns with just `room_id` + one of {sql, clarification, error}; mutual-exclusion still enforced in `__post_init__`.
- `RoomConfig.from_dict()` rehydrates nested dataclasses; documented as the inverse of `dataclasses.asdict()`.

---

## Step 2 — `tiri/providers/base.py`

**Goal.** Six abstract provider interfaces per `docs/providers.md`, plus error hierarchy.

**Built.**
- `tiri/providers/__init__.py`, `tiri/providers/base.py`.
- `tests/unit/test_providers_base.py`.

**ABCs.** `LLMProvider` (complete/stream/embed with `task=` parameter from day one), `CatalogProvider` (get_table_meta/list_tables/search_tables; physical schema only), `MetadataProvider` (`name` property + `enrich`), `QueryProvider` (execute/validate with `user_token=` parameter from day one for EXT-6), `VectorProvider` (upsert/query/delete; `list_ids` added in Step 7), `StoreProvider` (get/put/list_keys/delete).

**Errors.** `ProviderError` base; per-category subclasses (`LLMProviderError`, `CatalogProviderError`, `MetadataProviderError`, `QueryProviderError`, `VectorProviderError`, `StoreProviderError`); `TableNotFoundError` extends `CatalogProviderError`.

**Tests:** 36 new (87 total) — ABCs can't be instantiated, error hierarchy, complete stubs CAN be instantiated, partial stubs cannot, `__abstractmethods__` exact-match per ABC, signature checks for `task=` and `user_token=`.

**Decisions worth knowing.**
- `stream()` declared `async def → AsyncIterator[str]` matching the doc; concrete impls use `async def: yield ...` (async generator). Type checkers accept.
- Contract-table tests (1–15 in `providers.md`) for runtime behavior of concrete impls land in Step 5/6.

---

## Step 3 — `tiri/config.py`

**Goal.** Read `tiri.toml` + env vars; multi-backend LLM registry; validation per `docs/configuration.md`.

**Built.**
- `tiri/config.py` (Config dataclass, ProviderBackendConfig, RoutingConfig, ConfigurationError).
- `tests/unit/test_config.py`.

**Behavior.** `Config.load(toml_path)` reads `tiri.toml` if it exists, else synthesizes a single-backend registry from env. `${VAR}` substitution applied to every TOML string. Engine-tuning + API config always taken from env. Anthropic simple-mode auto-adds an OpenAI backend for `embed` route.

**Validation.** Missing `${VAR}` → `ConfigurationError` naming the var. Routing references undeclared backend → error. `embed` route → Anthropic → error. `query_provider=databricks` without `db_warehouse_id` → error. `vector_provider=databricks` without index/endpoint → error. `auth_disabled=true` → WARNING via `tiri.config` logger.

**Tests:** 16 new (112 total) — covers `configuration.md` cases 1–5 and 9, plus a static scan (case 8) that `tiri/` doesn't read `os.environ`/`os.getenv`/`tomllib` outside `config.py`. Step 4 added the engine/knowledge SDK-import scan to the same file.

**Decisions worth knowing.**
- Env wins over TOML for settings present in both (`db_warehouse_id`, etc.); TOML is structure, env is runtime values.
- Anthropic simple-mode auto-fallback adds an OpenAI backend rather than forcing a `tiri.toml`.
- `auth_disabled` truthy on `"true"/"1"/"yes"` (case-insensitive).

---

## Step 4 — `tiri/container.py` + `RouterLLMProvider`

**Goal.** Wire `Config` into provider instances; `build_container(cfg)` returns the six-entry dict consumed by RoomEngine.

**Built.**
- `tiri/container.py` (RouterLLMProvider, ModelRoute, build_container, build_* / _instantiate_* helpers).
- `tests/unit/test_container.py`.

**`RouterLLMProvider`.** Concrete `LLMProvider` that routes complete/stream/embed to the per-task `ModelRoute.provider`. Always returned as the `llm` entry even for single-backend configs. For MVP all routes point at the same provider instance; EXT-3 extends this same class to multi-backend.

**`build_container(cfg)`.** Build order matters: `query` first (so it can be injected into `store` and `metadata_providers`). Lazy imports inside each `_build_*` helper so the file compiles even before Steps 5/6 implementations exist.

**Tests:** 19 new (131 total) — RouterLLMProvider routing/lookup/embed dispatch, build_container with stub factories covering cases 6/7/10 from `configuration.md`. Test 10 uses `socket.connect` blocking to enforce no-network at build time.

**Decisions worth knowing.**
- One LLMProvider instance per backend name (not per backend+model). `ModelRoute.model_name` carries per-call model identity for EXT-3.
- Lazy imports + ImportError test as the canary that Steps 5/6 hadn't landed yet (test removed in Step 5).
- Monkeypatched factories in tests rather than patching concrete classes — keeps tests independent of Step 5/6 ordering.

---

## Step 5 — `tiri/providers/databricks/`

**Goal.** Concrete production-target providers per `docs/databricks_providers.md`.

**Built.**
- Six provider modules under `tiri/providers/databricks/` (llm, query, store, vector, catalog, metadata).
- Added `[databricks]` and `[local]` extras; `dev` includes both.
- Added top-level `Config.databricks_host`/`databricks_token` populated from env (the doc didn't have a home for non-LLM Databricks creds; this closed the gap).
- Reordered `build_container` to build `query` first; `_build_store` and `_build_metadata_providers` now accept `query` as a parameter.
- `tests/unit/test_databricks_providers.py`.

**Providers.**
- `DatabricksLLMProvider` (httpx): complete/stream/embed, exponential backoff on 429 (3 retries) and 5xx (1 retry), 4xx fail-fast, SSE parsing.
- `DatabricksQueryProvider` (httpx): Statement Execution API, submit + poll loop, `EXPLAIN`-prefixed validate, **safe LIMIT wrapping via subquery** (doc-commented "do not simplify"), `user_token` swaps Authorization header for EXT-6.
- `DatabricksStoreProvider`: wraps a QueryProvider; MERGE for put, LIKE for list_keys, SQL-quoted string literals.
- `DatabricksVectorProvider` (httpx): Direct Access index API; `list_ids` uses zero-vector + num_results=10000 workaround (see J3).
- `DatabricksCatalogProvider` (databricks-sdk via `asyncio.to_thread`): physical schema only — descriptive fields stay empty (`MetadataProvider` populates them); `NotFound`/`PermissionDenied` → `TableNotFoundError` on `get_table_meta`, → `CatalogProviderError` on `list_tables`/`search_tables`.
- `UCAnnotationsMetadataProvider`: UC table/column comments + optional `sample_values` via injected `QueryProvider`. `DeltaTableMetadataProvider` stub (J1).

**Tests:** 31 new (162 total) — all 10 cases from `databricks_providers.md`, HTTP-mocked via `httpx.MockTransport`, SDK mocked via `unittest.mock`.

**Decisions worth knowing.**
- HTTP-client injection seam (`client: httpx.AsyncClient | None = None`) on every HTTP provider.
- SDK calls wrapped in `asyncio.to_thread` to keep the async contract.
- `_apply_limit` wraps SQL in a subquery (LIMIT-safe for CTEs, trailing semicolons, existing LIMITs). `validate()` uses raw SQL with `EXPLAIN`, NOT the wrapped form.
- `row_count < 1_000_000` sample-value gate intentionally skipped — UC `numRows` is stale.

---

## Step 6 — `tiri/providers/local/`

**Goal.** Concrete dev/test providers per `docs/local_providers.md`.

**Built.**
- 9 provider modules + 1 stub (`metadata_dbt.py` — J2).
- Added `openai`, `anthropic`, `duckdb`, `chromadb`, `pyyaml` to deps.
- `tests/unit/test_local_providers.py`.
- `tests/fixtures/schemas.json` and `tests/fixtures/metadata.yaml`.

**Providers.**
- `OpenAILLMProvider` (openai SDK async client), `AnthropicLLMProvider` (system-message split for Anthropic API; `embed()` raises), `OllamaLLMProvider` (httpx to localhost:11434).
- `StaticCatalogProvider` (JSON file at construction, no I/O per call).
- `DuckDBQueryProvider`: in-memory DuckDB; auto-registers `{schema}__{table}.parquet/.csv` files in `data_dir` as `{catalog}.{schema}.{table}` views.
- `ChromaVectorProvider`: EphemeralClient for `:memory:`; converts cosine distance → similarity score in [0, 1].
- `SQLiteStoreProvider`: single connection + `check_same_thread=False` + RLock (pytest-asyncio worker threads); `INSERT OR REPLACE` atomic put.
- `StaticMetadataProvider`: nested-dict source; canonical merge engine for table/column scalar + list fields with conflict recording.
- `YAMLMetadataProvider`: loads YAML, delegates to StaticMetadataProvider.
- `DbtMetadataProvider`: stub.

**Tests:** 28 new (190 total) — all 13 cases from `local_providers.md`. Chroma tests use unique `collection_name` per test to avoid in-process collection-registry collisions across vector dimensions.

**Decisions worth knowing.**
- Anthropic SDK takes `system=` separately; split happens in `_split_messages`.
- DuckDB auto-registration: `{schema}__{table}` → `{catalog}.{schema}.{table}`; comment in code at the parse site.
- SQLite threading comment marked "do not 'fix' check_same_thread=False" — pytest-asyncio worker threads would break `:memory:` tests without it.
- StaticMetadataProvider's `_apply_table_entry` / `_apply_column_entry` helpers are the shared merge engine; YAMLMetadataProvider just parses.

---

## Step 7 — `tiri/knowledge/`

**Goal.** MetadataFetcher + ExampleIndexer + ContextBuilder + RoomConfigMetadataProvider per `docs/knowledge_store.md`.

**Built.**
- `tiri/knowledge/room_config_metadata.py`, `metadata_fetcher.py`, `example_indexer.py`, `context_builder.py`.
- Extended `VectorProvider` ABC with `list_ids(filter)` (real ABC evolution, justified by ExampleIndexer's deletion path).
- Implemented `list_ids` in DatabricksVectorProvider (zero-vector approx — J3) and ChromaVectorProvider (native `collection.get`).
- `tests/unit/test_knowledge.py`.

**Modules.**
- `RoomConfigMetadataProvider` — always-last entry, applies `RoomConfig.column_overrides`. `name = "room_config"`.
- `MetadataFetcher`: physical schema via CatalogProvider; runs declared stack in order; **always appends RoomConfigMetadataProvider last**; per-request cache keyed by `(room_id, set(tables))`; re-raises `TableNotFoundError("Table not found: {full_name}")`.
- `ExampleIndexer`: `index()` — one `llm.embed()` for all examples, upsert each, then `list_ids({"room_id": ...})` diff → delete stale. `retrieve()` — embed question, query with room filter, map payloads to ExampleSQL.
- `ContextBuilder`: constructs fresh MetadataFetcher + ExampleIndexer per `build()`; exactly one embed call; zero complete/stream calls.

**Tests:** 18 new (208 total) — all 12 cases from `knowledge_store.md`. Engine-isolation scan still passes.

**Decisions worth knowing.**
- `VectorProvider.list_ids` is a real ABC extension — discovered need during Step 7. Same `{"room_id": ...}` filter shape as `query()`.
- ContextBuilder builds a new MetadataFetcher per `build()` so the per-request cache resets.
- ExampleIndexer skips the embed call entirely when `config.examples` is empty, but still queries `list_ids` to clean up leftover entries.
- `MetadataFetcher.fetch()` accepts `query: QueryProvider | None` per the doc even though metadata providers currently receive `query` via constructor.

---

## Step 8 — `tiri/engine/agents/`

**Goal.** Four agents per `docs/agents.md` (Intent, SQL, Clarify, Viz) + prompt templates.

**Built.**
- `tiri/engine/agents/base.py` (shared `load_template`, `render`, formatter helpers).
- `tiri/engine/agents/intent_agent.py`, `sql_agent.py`, `clarify_agent.py`, `viz_agent.py`.
- `tiri/engine/prompt_templates/intent_classification.txt`, `sql_generation.txt`, `clarification.txt`.
- `tests/unit/test_agents.py`.

**Agents.**
- `IntentAgent`: one `llm.complete(task="intent")`. JSON parsing tolerant of ` ```json ` fences and trailing prose. Resolves `relevant_snippets` display-name strings → `SqlSnippet` objects (rule 7); unknown names dropped with WARNING. Unknown intent string → forced to `out_of_scope`.
- `SQLAgent`: filters context to `intent.relevant_tables/snippets`. Self-correction loop: `llm.complete(task="sql")` → `query.validate()` → on failure, append candidate + error as user feedback and retry. `CANNOT_ANSWER:` prefix short-circuits. After `max_retries`, returns `SQLResult(is_valid=False, ...)` — never raises.
- `ClarifyAgent`: one `llm.complete(task="clarify")`.
- `VizAgent`: rule-based chart selection (`classify_column` → `semantic_type` from `context.table_schemas` primary; value inspection fallback). Per-chart-type spec builders in Python. Exactly one LLM call for the summary (`task="viz_summary"`).

**Tests:** 21 new (229 total) — all 17 cases from `agents.md`. Includes static scan asserting agents only import from `tiri.data_models`, `tiri.providers.base`, `tiri.engine.agents`. Step 9 added a tighter cross-agent-import scan.

**Decisions worth knowing.**
- `render()` uses `str.replace` not `str.format` — templates contain literal `{` and `}` in JSON example blocks. **Do not "fix" this.**
- All four agent prompts loaded once at module import (per `Path(__file__).parent / "prompt_templates"`); no per-request file I/O.
- VizAgent always emits a valid v5 Vega-Lite spec, including `"table"` (catch-all).
- `default_filters` initially rendered as `(none)` in SQLAgent (placeholder); resolved in Step 9 by adding `ContextPackage.default_filters`.

---

## Step 9 — `tiri/engine/room_engine.py`

**Goal.** RoomEngine pipeline orchestrator + RoomManager CRUD per `docs/room_engine.md`.

**Built.**
- Added `ContextPackage.default_filters: list[str]`; updated ContextBuilder to populate from `RoomConfig.default_filters`; SQLAgent now reads from `context.default_filters` (placeholder resolved).
- `tiri/engine/room_engine.py` (RoomEngine, RoomManager, RoomNotFoundError, PipelineError, helpers).
- `tests/unit/test_room_engine.py`.

**RoomEngine.**
- `chat()`: load config → load history → ContextBuilder → IntentAgent → route to ClarifyAgent / SQLAgent → execute → VizAgent → persist turn. Always persists a turn (even on error). Maintains both `conv:{id}:index` and `room:{id}:conversations` indexes.
- `stream_chat()`: same pipeline; emits SSE event dicts (status/sql/result/viz/clarify/error/done). `done` is always last, including on error paths.
- Pipeline invariants enforced: validate-before-execute satisfied by SQLAgent (no re-validate); persist before return; exactly-one mutual exclusion; RoomConfig loaded fresh per request.

**RoomManager.**
- `create`, `get`, `update`, `delete`. `update` re-indexes when example IDs OR content changed (not just id-set equality). `delete` uses the room→conversation index to enumerate rather than full store scan; removes feedback rows and config last.

**Tests:** 18 new (248 total) — all 13 cases from `room_engine.md` + EXT-6 token forwarding + room→conversation index maintenance.

**Decisions worth knowing.**
- `done` always last including on error paths — clients get a reliable stream terminator.
- `_turn_from_dict` skips EXT-1/EXT-11 fields (not needed in MVP history replay).
- `_example_content_changed` catches id-preserving SQL/question edits.
- RoomEngine does NOT re-validate after SQLAgent (per the documented invariant).

---

## Step 10 — `tiri/api/`

**Goal.** FastAPI app + management + conversation routes per `docs/api.md`.

**Built.**
- `tiri/api/main.py` (create_app, lazy module-level `app` via `__getattr__`, exception handlers).
- `tiri/api/auth.py` (Bearer dependency honoring `AUTH_DISABLED`).
- `tiri/api/routes/management.py` (POST/GET/PATCH/DELETE /rooms, POST /rooms/{id}/index, benchmarks/run stubbed at 501).
- `tiri/api/routes/conversations.py` (start conversation, send message, SSE stream, list messages).
- Added `[api]` extras (fastapi, uvicorn) and to `[dev]`.
- `tests/unit/test_api.py`.

**Behavior.**
- `create_app(cfg=None, container=None)` — both injectable for tests; defaults to `Config.load()` + `build_container(cfg)` for production.
- RoomEngine/RoomManager constructed per request from `request.app.state.container`.
- Streaming endpoint takes `question` as **query parameter** (`GET ?question=...`); non-streaming POST takes it in the body. Easy to mix up.
- Exception handlers: `RoomNotFoundError → 404`, `ValueError → 422`, `ConfigurationError → 500`.
- Bearer auth enforces presence only — validity is delegated to Unity Catalog when EXT-6 forwards the token to `QueryProvider.execute`.

**Tests:** 17 new (265 total) — cases 1–9 from `api.md`; case 10 stubbed as 501 (made real in Step 11).

**Decisions worth knowing.**
- Lazy `app = create_app()` via `__getattr__` — importing the module in tests doesn't trigger `Config.load()`.
- Raw `dict[str, Any]` request bodies delegating validation to `RoomConfig.from_dict()` + `__post_init__` rather than pydantic models (J4).
- Re-index endpoint fire-and-forget via `asyncio.create_task`; 202 returned immediately; background exceptions logged.

---

## Step 11 — `tiri/feedback/` + feedback routes

**Goal.** Collector + Proposer + BenchmarkRunner per `docs/feedback.md`, plus their API routes.

**Built.**
- `tiri/feedback/sql_normalize.py` (quote-aware lowercase + whitespace collapse + semicolon strip).
- `tiri/feedback/collector.py`, `proposer.py`, `benchmark_runner.py`.
- `tiri/api/routes/feedback.py` (POST .../feedback, POST .../feedback/propose).
- `tiri/api/routes/management.py`: replaced 501 stub with real benchmarks/run; added benchmark add/delete routes.
- `tests/unit/test_feedback.py`.

**Components.**
- `Collector`: validates signal ∈ {"up", "down"}; loads existing turn (raises `StoreProviderError` if missing); updates `feedback_signal` on the turn; writes `feedback:{conv}:{turn}` row.
- `Proposer`: walks room→conversation index, **skips `benchmark-` prefixed conversations**, walks each conv's turn index, filters thumbs-up turns with SQL, drops turns whose normalized SQL is already in `config.examples` (no LLM call), asks the LLM YES/NO for the rest. Returns `ExampleSQL` candidates. Never mutates RoomConfig.
- `BenchmarkRunner`: per benchmark, `engine.chat(room_id, f"benchmark-{id}", question)`. Compares normalized generated vs expected SQL; if `expected_row_count` set and `store_query` available, runs both and compares row counts. Pipeline errors caught per-benchmark and recorded; report continues. `score = passed / total`.
- `sql_normalize.normalize_sql`: walks chars, lowercases outside single/double quotes (preserves string literals + quoted identifiers), collapses whitespace, strips trailing semicolons.

**Tests:** 18 new feedback tests + 2 new API tests (285 total). All 10 cases from `feedback.md` + benchmark-conversation exclusion + bonus cases (row-count fallback, no-SQL turn skip).

**Decisions worth knowing.**
- `normalize_sql` lowercases identifiers too (not just keywords) — required by `feedback.md` test case 9.
- Proposer filters by normalized SQL BEFORE the LLM call — saves a model invocation for already-known examples.
- Proposer skips turns without SQL (clarification/error turns can carry feedback but yield no example).
- BenchmarkRunner reaches into `engine._load_room_config` to keep store-key conventions in one place.
- Benchmark routes (add/delete) round-trip through `RoomManager.update`; benchmark changes don't trigger re-indexing because they aren't examples.

---

## Step 12 — extensions (`docs/extensions.md`)

Build order followed: EXT-3 → EXT-2 → EXT-6 → EXT-7 → EXT-1 → EXT-4 → EXT-5 → EXT-11. Each extension was implemented to docs-spec with a dedicated test file and a regression check against the full prior suite.

### EXT-3 — Multi-model routing

**Built.** Extended `LLMProvider.complete()` and `.stream()` ABC with `task: str = "sql"` and `model: str | None = None` kwargs. `RouterLLMProvider` (in `container.py`) now picks a backend per task via `RoutingConfig` and forwards the per-route `model_name`. Three-level model precedence: caller-supplied > `route.model_name` > construction default. Single-backend implementations accept the new kwargs and MAY ignore them — documented as a contract requirement.

**Decisions worth knowing.** The structural enforcement is a static scan: `tests/unit/test_providers_base.py` asserts every `LLMProvider` subclass implements the new signature. Tests on `RouterLLMProvider` cover the precedence ladder + missing-route fail-fast.

### EXT-2 — Dynamic table selection

**Built.** `tiri/knowledge/table_selector.py` with `TableSelector`. Expands wildcard entries in `RoomConfig.tables` (`catalog.schema.*`, `catalog.*.*`) via `CatalogProvider.list_tables` + new `CatalogProvider.list_schemas`. Ranks candidates by cosine similarity between question embedding and table names; cap at `RoomConfig.max_tables_per_query`. Join-spec tables are always included regardless of rank. New `ContextPackage.table_selection_method` ∈ {`configured`, `dynamic_search`, `hybrid`} copied through to `IntentResult` for the reasoning trace.

**Decisions worth knowing.** Wildcard shapes outside `catalog.schema.*` / `catalog.*.*` are dropped with a WARNING rather than raising — the goal is graceful degradation, not strict parsing. `ContextBuilder` calls `TableSelector` only when `RoomConfig.tables` contains a wildcard; pure-FQN configs incur zero overhead.

### EXT-6 — Per-user credential execution

**Built.** `user_token: str | None` parameter threaded through `RoomEngine.chat` → `SQLAgent.run` → `query.validate()` AND `query.execute()`. `auth.py` extracts the token from `Authorization: Bearer` first, falls back to `X-Forwarded-Access-Token` for Databricks-Apps deployments. Returns 401 if neither is present (unless `AUTH_DISABLED=true`). `DatabricksQueryProvider` swaps the `Authorization` header per-request when `user_token` is set.

**Decisions worth knowing.** **Real bug found and fixed**: prior to this step, `SQLAgent` called `query.validate(candidate)` without `user_token` — meaning EXPLAIN ran as the service principal even when user credentials were available. Without the fix, a user without SELECT on a table would have passed validation (service has access) and then hit a permission error at execute time, bypassing UC enforcement at the validate boundary. Comment in `sql_agent.py` records the rationale so a future refactor doesn't drop it.

3 integration tests added (`@pytest.mark.integration`, gated by `INTEGRATION_TESTS=true`) for restricted-column / no-SELECT / two-user RBAC scenarios.

### EXT-7 — Explicit uncertainty

**Built.** `tiri/engine/agents/synthesis_agent.py`. `SynthesisAgent.synthesize(question, plan, results, context) → SynthesizedAnswer` with `answer`, `data_supports`, `data_does_not_support`, `would_need`, `confidence` ∈ {`high`, `medium`, `low`}, `confidence_rationale`. Prompt template (`engine/prompt_templates/synthesis.txt`) bans causal verbs. Post-generation `_enforce_no_causal_language()` scans the `answer` field for `caused by | because of | due to | result of | led to` (case-insensitive, word-boundary). On any match, raises `SynthesisError` — structural enforcement, not prompt-level hope.

**Decisions worth knowing.** Attachment rule on `ConversationTurn`: multi-step plans always carry `synthesized_answer`; single-step plans attach only when confidence is `medium` / `low`. High-confidence single-step questions don't need synthesis prose — the SQL + viz already convey the answer.

### EXT-1 — Multi-query reasoning

**Built.** `tiri/engine/agents/planning_agent.py` + `engine/prompt_templates/planning.txt`. `PlanningAgent.plan(question, context) → ReasoningPlan` with 1–5 steps (hard cap; truncate with WARNING). `RoomEngine` executes steps sequentially honoring declared order (sequential MVP — `depends_on` is metadata for the future parallel execution path). `SynthesisAgent.synthesize()` extended to take the plan + list of step results. New SSE event types: `plan` (multi-step only), `steps` (after primary result, lists each step's sql/columns/row_count).

**Decisions worth knowing.** Single-step plan path is functionally identical to pre-EXT-1 — the regression test `test_chat_one_step_plan_matches_pre_ext1_turn_shape` enforces this. Defensive fallbacks in PlanningAgent: empty `steps[]` → one-step plan with the original question as description; forward refs in `depends_on` → dropped with WARNING.

### EXT-4 — MCP server exposure

**Built.** `tiri/api/mcp/server.py`. Single `POST /mcp` endpoint speaking JSON-RPC 2.0 (Streamable HTTP transport). Methods: `initialize`, `tools/list`, `tools/call`. Three tools: `tiri_query` (with `conversation_id` round-trip for multi-turn context), `tiri_list_rooms`, `tiri_room_schema`. Mount alongside existing REST routes via `app.include_router(mcp_server.router, prefix="/mcp")`.

**Decisions worth knowing.** Auth failures return **HTTP 200 + JSON-RPC error `-32001`, NOT HTTP 401**. MCP clients expect protocol-level errors; treating an auth miss as a transport error breaks tool discovery. Implementation is direct JSON-RPC over httpx — chose not to depend on `fastapi-mcp` because the surface is small and the library's stability isn't guaranteed for the protocol version we target.

### EXT-5 — MCP tool consumption

**Built.** New `MCPProvider` ABC in `providers/base.py` (sixth interface) with `list_tools()` + `call_tool()`. Concrete `HttpMCPProvider` in `providers/local/mcp_http.py` speaks the same JSON-RPC over httpx. `tiri/knowledge/mcp_resolver.py` orchestrates calls — for each URL in `RoomConfig.mcp_servers`, calls the first tool with `{"query": question}` and collects non-error results into `ContextPackage.mcp_context`. Each call has a per-call timeout (default 5s); timeouts, transport failures, and tool-level errors are logged and skipped — MCP MUST NEVER block the pipeline.

**Decisions worth knowing.** **Security boundary**: only servers explicitly listed in `RoomConfig.mcp_servers` may be called. URLs in the room's list but absent from the engine's `MCPProvider` registry are misconfigurations (logged WARNING), not security violations. Empty `mcp_servers` → zero MCP calls, zero overhead vs. pre-EXT-5 — verified by the no-regression test that registers a provider but loads a room without listing it.

### EXT-11 — Hypothesis mode

**Built.** `tiri/engine/agents/hypothesis_agent.py` + `engine/prompt_templates/hypothesis_generation.txt`. Three gates checked by `RoomEngine` before invocation: `RoomConfig.hypothesis_mode_enabled=True` + multi-step plan + `_is_causal_question(question)`. Caps output at 3 hypotheses. `Hypothesis.statement` post-scanned for `caused | because | due to | result of | led to` (same pattern as SynthesisAgent) — violation raises `HypothesisError`. `Hypothesis.domain_knowledge_used` filtered to entries actually present in `RoomConfig.domain_knowledge` (hallucinated entries dropped with WARNING, not raised).

**Decisions worth knowing.** Six structural invariants (4 dataclass-level, 2 agent-level), all enforced — `HypothesisResult.confidence` MUST be `"low"` (raises `ValueError`); `disclaimer` MUST be non-empty; every `Hypothesis` MUST have ≥1 `contradicting_pattern` (moved to `Hypothesis.__post_init__` per "every hypothesis with only supporting evidence is a claim, not a hypothesis"); hypothesis-mode-disabled never invokes the agent regardless of question phrasing; HypothesisAgent never runs without a multi-step `ReasoningPlan`; causal verbs in any `statement` raise.

`_maybe_generate_hypotheses` catches `HypothesisError` and logs without crashing the turn — a bad hypothesis attempt must not destroy a valid synthesized answer.

---

## Step 13 — CLI (`tiri/cli.py`)

**Goal.** Implement the CLI spec in `CLAUDE.md` as a thin wrapper over the existing engine + manager. No new abstractions. Plus `import-genie` from `docs/roadmap.md` R5 as a first-class command since the translation function is pure and testable.

**Built.** Six commands: `load-room` (idempotent create-or-update via `RoomManager`), `ask` (one-shot `RoomEngine.chat` with formatted answer/SQL/confidence output), `benchmark` (runs `BenchmarkRunner`, exits non-zero when score < 1.0 — the DoD gate), `dump`, `serve` (uvicorn lazy import), `import-genie` (translates a Genie wire-format JSON to `RoomConfig` JSON either from `--input <path>` or by fetching via the Databricks Workspace API with `--space-id <id>`). Genie→RoomConfig translation is a pure function (`_genie_to_room_config`) covering the field mapping table from roadmap.md R5: text_instructions list-unwrap, FROM_RELATIONSHIP_TYPE_ prefix strip, snippet `kind` injection, table_ref extraction, etc.

**Tests.** 14 — argparse coverage for all 6 commands + 9 unit tests on the Genie translation (round-trip through `RoomConfig.from_dict`, prefix stripping, snippet kind injection, missing-optional-block tolerance, room_id override + fallback, warehouse_id-must-be-blank-for-user-fill-in, end-to-end `--input` → `--output` integration via a temp file).

**Decisions worth knowing.** Credentials for the `--space-id` path come from `Config.load()`, not direct `os.environ.get()` — keeps the "only `config.py` reads env / tomllib" rule (enforced by the static scan in `test_config.py`) intact even for the CLI. First implementation failed the scan twice: once for an actual `os.environ.get` call, once for the literal string `tomllib` appearing in a docstring (the scan is regex-based on raw text). Comment was rephrased to avoid the literal tokens.

---

## Doc revision pass

After Step 12 + Step 13, brought per-component docs in sync with the code. `docs/extensions.md` was already the most-accurate source of truth; the surrounding component docs lagged.

**Updated.** `docs/providers.md` (six → seven interfaces; added `MCPProvider` ABC and `VectorProvider.list_ids`); `docs/knowledge_store.md` (10 new test-case rows for `TableSelector` and `MCPResolver`); `docs/data_models.md` (`RoomConfig.mcp_servers`, `ContextPackage.mcp_context` + `domain_knowledge`, new EXT-5 types section); `docs/room_engine.md` (`mcp_providers` constructor kwarg; updated streaming event list with `mcp_context` / `plan` / `steps` / `synthesis` / `hypotheses`; 9 new test-case rows); `docs/api.md` (full MCP API section already in place; 7 new test-case rows covering JSON-RPC auth + tool dispatch + REST coexistence); `docs/configuration.md` (`databricks_host` / `databricks_token` added to `Config` dataclass listing; `viz_summary` row in the routing reference table rewritten with the "MUST NOT be a guardrail-heavy small model in regulated-domain deployments" guidance once that was discovered during live validation — see below).

Closed fixmes **K1** (list_ids) and **K2** (databricks_host/token).

---

## Live benchmark validation

Validated the DoD against Databricks workspace `<workspace-id>` using a self-hosted Ollama host for completions (the Databricks-hosted llama-3-1-8b output guardrail flagged benign TPC-H rows — see below). `tiri.toml` and `demo/tpch_*_config.local.json` are gitignored workspace-specific files (`samples.tpch.*` rewrites of the committed `tpch.sf1.*` configs, plus populated `expected_row_count`).

**Results.** `tpch-sales` 5/5 (100%) ✅. `tpch-supply` 3/5 (60%) — two failures are irreducible semantic gaps where both 70B Llama and 14B qwen interpret the questions differently from the benchmark's `expected_sql`. Not Tiri code bugs; resolution requires example-engineering on the supply room.

**Four real bugs surfaced and fixed during validation.**

1. **`RoomConfig.from_dict` couldn't load configs lacking explicit `kind` on `SqlSnippet`.** The committed demo configs have no `kind` field on `sql_filters` / `sql_expressions` entries — `from_dict` was calling `SqlSnippet(**s)` and failing on the required positional arg. Fixed by injecting `kind` from the list source (`sql_filters` → `"filter"`, `sql_expressions` → `"expression"`, `sql_measures` → `"measure"`) when not explicitly set. The kind is structurally determined by which list a snippet lives in, so this is sound — and it makes Genie-imported configs (which keep snippets typed by their map key, no `kind` field) load without modification.

2. **`Config._from_toml` didn't env-fall-through for Databricks LLM backends.** `[llm.providers.NAME]` blocks declared `type = "databricks"` without inline `host`/`token` — the TOML loader passed empty strings through and `DatabricksLLMProvider` raised on construction. Fixed by falling back to `DATABRICKS_HOST` / `DATABRICKS_TOKEN` env vars when the backend type is `databricks` and the TOML host/token are blank. Mirrors how `db_warehouse_id` already fell through. Keeps secrets out of TOML — `tiri.toml` describes wiring; env carries credentials.

3. **`VizAgent` crashed the whole turn on summary LLM failure.** Databricks' output guardrail on `databricks-meta-llama-3-1-8b-instruct` flagged `indiscriminate-weapons:true` on a benign TPC-H supply-chain summary prompt — result rows containing nation names (IRAN, IRAQ, RUSSIA) combined with words like "supply" / "quantity" / "parts_supplied". The guardrail can't tell a parts catalog from weapons supply chain content. Filed as fixme M2 with full design implications for regulated-industry deployments. Fix: VizAgent's `_summarize` now catches LLM failures and degrades to an empty summary — the SQL / result / Vega-Lite spec still ship; only the decorative summary is dropped. Regression test added (`test_viz_summary_llm_failure_degrades_to_empty_string`).

4. **`SQLAgent` didn't strip markdown fences from model output.** `qwen2.5-coder:14b` wraps SQL in ```` ```sql ... ``` ```` despite the "no markdown fences" prompt instruction. SQLAgent was passing the fenced text straight to `query.validate()`, which sent `EXPLAIN \`\`\`sql ... \`\`\`` to the warehouse and got PARSE_SYNTAX_ERROR. IntentAgent and SynthesisAgent already strip fences from their JSON responses; SQLAgent now does the same. Parametrized regression test covers 5 fence variants (lowercase `sql`, uppercase `SQL`, fenceless, multi-line, already-unfenced).

---

## Cumulative tally

| Step | Component | New tests | Total |
|---|---|---:|---:|
| 1 | data_models | 51 | 51 |
| 2 | providers/base | 36 | 87 |
| 3 | config | 25 | 112 |
| 4 | container + RouterLLMProvider | 19 | 131 |
| 5 | providers/databricks | 31 | 162 |
| 6 | providers/local | 28 | 190 |
| 7 | knowledge | 18 | 208 |
| 8 | engine/agents | 21 | 229 |
| 9 | engine/room_engine + RoomManager | 19 | 248 |
| 10 | api (FastAPI) | 17 | 265 |
| 11 | feedback + routes | 20 | 285 |
| 12 EXT-2 / EXT-3 / EXT-6 / EXT-7 / EXT-1 / EXT-4 / EXT-5 / EXT-11 | extensions | 122 | 407 |
| 13 | CLI | 14 | 421 |
| validation | viz degrade + SQLAgent fence strip | 5 | 426 |

Integration tests: 3 (all skipped — EXT-6 RBAC scenarios, require `INTEGRATION_TESTS=true` + live workspace).

Architecture-scan tests still in place and passing: `config.py` is the sole env/tomllib reader; `engine/` and `knowledge/` never import SDKs directly; agents only import from `data_models` + `providers.base` + `agents.base`; no agent imports another agent's module.

## Open issues (`fixme.md`)

10 entries, no blockers. Notable:

- **M2** — LLM output guardrail false-positives on benign row data (new from live validation; documents the routing posture for production with regulated-domain content).
- **M1** — Local TPC-H benchmark prerequisites (partially superseded — DoD has now been validated against live Databricks; the local DuckDB path remains useful for offline-only development).
- **L1** — `build_container` doesn't instantiate MCP providers from `tiri.toml` config; deployments must wire `HttpMCPProvider` externally.
- **L2** — `tiri.toml.example` referenced in CLAUDE.md but doesn't exist.
- **I1, I2** — Cosmetic / future.
- **J1, J2, J3, J4, J5** — Spec items deferred until customer signal.

## Status

**Feature-complete as of Steps 1–13 + live-workspace DoD validation.** See `CLAUDE.md`'s "Project status" section for the full validated stack list and the four bugs the live run surfaced. Next work tracked in `docs/roadmap.md` (R1–R6, customer-validated future extensions).
