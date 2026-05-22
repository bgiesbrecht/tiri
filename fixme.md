# Tiri — open issues to fix

Snapshot after the third doc revision pass. All prior blockers are resolved. One cosmetic item remains for the next pass; below it is the audit trail of resolved items.

Severity legend:
- **Blocker** — will produce wrong behavior or fail to compile/run.
- **Spec** — internal doc inconsistency that will cause confusion or rework during implementation.
- **Cosmetic** — doc polish; no behavioral impact.

---

## Open

### L4 — Gemini 2.5 Pro benchmark crash [Spec]

Smoke ping against `databricks-gemini-2-5-pro` succeeded (returned
`PONG` + 177 reasoning tokens) after the system-only message
normalization + larger `max_tokens` fix. Full benchmark crashed in
`DatabricksCatalogProvider.get` (Unity Catalog API call) — likely
token expiry mid-run or a transient workspace issue, NOT a Tiri code
bug. The catalog call worked moments earlier in the Opus 4.7 run.

**Fix in place:** system-only message normalization (`_normalize_messages`
injects a directive user placeholder) and the `"Please respond to the
instructions above"` text are correct for Gemini. The crash is
infrastructure-side.

**Retry plan:** refresh the Databricks PAT and re-run with full debug
output before concluding Gemini is unsupported. If the catalog call
still fails after a fresh token, file as a workspace-side issue.

---

### L3 — GPT-5.5 Pro / Responses API not supported [Spec]

`DatabricksLLMProvider` speaks the Chat Completions API
(`/serving-endpoints/{endpoint}/invocations`) only. GPT-5 family models
on Databricks (`databricks-gpt-5-5-pro`, `databricks-gpt-5-4`,
`databricks-gpt-5-mini`, etc.) require the **Responses API**:

```
Model databricks-gpt-5-5-pro only supports the Responses API.
Please use /serving-endpoints/responses or /serving-endpoints/open-responses instead.
```

**Fix.** Add a code path for the Responses API. Options:
- New `DatabricksResponsesLLMProvider` subclass — clear separation, but
  duplicates auth/retry logic.
- Extend `DatabricksLLMProvider` to detect GPT-5-family endpoints and
  route to `/responses` — less duplication, more conditional logic.
- Catch the specific "only supports the Responses API" 400 and retry
  through the new endpoint — same pattern as the
  temperature-retry from this session.

No other model currently requires this, so non-urgent. Tracked here so
a future GPT-5 deployment doesn't blindside the next session.

---

### M2 — LLM output guardrail false-positives on benign row data [Real-world observation]

The llama-3-1-8b output guardrail fires `indiscriminate-weapons:true`
on TPC-H supply chain queries where result rows contain nation names
(IRAN, IRAQ, etc.) combined with words like "supply", "quantity",
"parts_supplied". The guardrail cannot distinguish a parts catalog
from weapons supply chain content.

VizAgent already degrades gracefully (fix in `viz_agent.py` —
summary drops to empty string, SQL/result/spec still ship; covered
by `test_viz_summary_llm_failure_degrades_to_empty_string`). This
pattern will hit any tenant in regulated industries: defense, pharma,
healthcare, finance, sanctions screening. Row values like
`customer.country='IRAN' + "transaction" + "amount"` will trip
identical guardrails.

**Design implications for production deployment:**

- Route `viz_summary` and `synthesis` to the larger model (70B) which
  has less aggressive guardrails, not the small/fast model. The cost
  delta for one-sentence summaries is negligible.
- Added a routing note to `docs/configuration.md` under the routing
  task reference table.
- `SynthesisAgent` intentionally does NOT degrade silently — its
  output IS the answer for medium/low confidence turns. A
  guardrail-blocked synthesis should surface as an error turn, not
  a silent empty answer.
- Consider routing `viz_summary` separately from `intent` and
  `clarify` in `tiri.toml` so operators can assign a guardrail-safe
  model to it independently.
- The fastest validation workaround during this session: switch all
  completion tasks to Ollama (no output classifier). Documented in
  `tiri.toml` (gitignored).

---

### M1 — Local TPC-H benchmark validation prerequisites missing [Spec / partially superseded]

**Update (2026-05-22):** the DoD has now been validated against a
live Databricks workspace using a self-hosted Ollama host for
completions (see `Project status` in CLAUDE.md). The local-DuckDB
path described below remains useful for offline-only development,
but is no longer the only route to running the benchmarks.


**Where:** repo root + `tests/fixtures/`.

**State.** The CLAUDE.md Definition of Done targets **100% on both
`tpch-sales` and `tpch-supply` benchmarks** (5 each, 10 total). The
CLI is now in place (`python -m tiri.cli benchmark --room tpch-sales`),
but three prerequisites are missing:

1. **TPC-H Parquet data is not in the repo.** No `tests/fixtures/data/`,
   no `demo/data/`, no `*.parquet` anywhere. The demo configs reference
   `tpch.sf1.{customer,orders,lineitem,nation,region,supplier,part,partsupp}`
   — eight tables. `DuckDBQueryProvider` auto-registers files named
   `{schema}__{table}.parquet` from `DUCKDB_DATA_DIR` (default
   `./tests/fixtures/data` per `.env.example`).

2. **`tests/fixtures/schemas.json` is a 4-line unit-test stub.** Has
   only `tpch.sf1.lineitem` and `tpch.sf1.orders` with ~4 columns each
   and `row_count: 6 / 3`. Real validation needs all 8 TPC-H tables
   with the full TPC-H spec columns. (Alternative: introduce a
   `DuckDBCatalogProvider` that introspects the connection's
   INFORMATION_SCHEMA — would auto-derive schemas from the loaded
   Parquet files and eliminate the need to maintain `schemas.json`
   in lockstep with the data. New work, but small.)

3. **LLM credentials are user-supplied.** Expected — not a code gap.
   `.env.example` documents `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`
   + OpenAI for embeddings, or `OLLAMA_BASE_URL` for fully local).

**Fix.** A small generation script (or a `make tpch-data` target) using
DuckDB's built-in `CALL dbgen(sf=0.01)` would produce the 8 Parquet
files in seconds. SF0.01 is ~10 MB total — small enough to gitignore
and regenerate, large enough to make benchmark answers non-trivial.
Pair with either an expanded `schemas.json` covering all 8 tables OR
a new `DuckDBCatalogProvider` that introspects.

**Why this matters.** Until M1 is resolved, the DoD is unverifiable
locally. The 100% target is the real validation gate for whether the
agent pipeline + prompt templates are actually correct end-to-end —
unit tests verify structure, benchmarks verify behavior. A new
session picking this up should treat M1 as the next concrete unit of
work after the doc revision pass.

---

### L2 — `tiri.toml.example` referenced in CLAUDE.md but doesn't exist [Cosmetic]

CLAUDE.md "Project layout" lists `tiri.toml.example ← copy to tiri.toml
and fill in your values` but no such file is in the repo. Only
`.env.example` exists. Either create a `tiri.toml.example` documenting
the TOML-based config shape (mirroring `.env.example`'s env-var path),
or remove the reference. The env-var path works today, so this is
purely about which entrypoint we want to highlight to operators.

---

### L1 — `build_container` doesn't instantiate MCP providers from config [Spec]

**Where:** `tiri/container.py` `build_container()`, `tiri/config.py`.

**State.** EXT-5 added `RoomConfig.mcp_servers: list[str]` (per-room
authorization list) and the engine accepts a `dict[url, MCPProvider]`
registry, but `build_container` currently returns `mcp_providers={}`. To
exercise MCP in production, the user has to wire `HttpMCPProvider`
instances manually or via a fork of `build_container`.

**Fix.** Add a `mcp_server_urls` (and optional `mcp_server_tokens`) section
to `Config` / `tiri.toml` and have `build_container` construct one
`HttpMCPProvider` per declared URL. Auth token comes from a sibling
config key, never inline. Not urgent — deployments without MCP work today;
this only blocks the "set up MCP via config alone" UX.

---


### I1 — Most agents don't show their `task=` value in prose [Cosmetic]

**Where:** `docs/agents.md` — IntentAgent, ClarifyAgent, VizAgent sections.

**Problem.** SQLAgent shows `await llm.complete(messages, task="sql")` (line 170). The other three agents describe their LLM calls in prose without showing the explicit `task=` value. The abstract `LLMProvider.complete()` defaults `task="sql"`, so omitting it in agent code silently routes intent/clarify/viz_summary calls to the SQL backend once EXT-3 (multi-model routing) is implemented.

For MVP (single backend) this is harmless — all routes point at the same provider. It becomes a correctness issue at EXT-3.

**Correct values** (confirmed by user):
- `IntentAgent` → `task="intent"`
- `ClarifyAgent` → `task="clarify"`
- `VizAgent` (the summary call) → `task="viz_summary"`

**Fix.** In the next doc pass, show the explicit `task=` value in each agent's section so the canonical pattern is visible everywhere, not just in SQLAgent. Optional companion: add an explicit test case in `docs/agents.md` test-case table mirroring `docs/extensions.md` EXT-3 test 8.

---

### J1 — DeltaTableMetadataProvider not implemented [Spec]

**Where:** `tiri/providers/databricks/metadata.py`, `tiri/container.py`.

**State.** `container.py` wires `type = "delta_table"` to a `DeltaTableMetadataProvider` stub that raises `MetadataProviderError("not yet implemented")` on `enrich()`. The design (Delta table schema, field mapping, list-field comma-splitting) is fully specified in `docs/metadata.md`.

**Fix.** Implement when a customer actually needs it. No urgency — `YAMLMetadataProvider` covers the common case where metadata lives in version-controlled files; the Delta-table path is for orgs that manage metadata as data with governance tooling that writes to Delta.

---

### J5 — `BenchmarkRunner` accesses engine internals [Spec]

**Where:** `tiri/feedback/benchmark_runner.py`.

**State.** `BenchmarkRunner._load_config` calls `engine._load_room_config(room_id)` to avoid duplicating the `room:{id}:config` store-key convention. This is a minor coupling — `BenchmarkRunner` reads a leading-underscore attribute of `RoomEngine`.

**Fix.** If `BenchmarkRunner` ever needs to run without an engine (e.g. for a standalone CLI `tiri benchmark` command), accept `StoreProvider` directly and use the canonical key pattern from `docs/data_models.md`'s store key layout. Not urgent — the engine-dependent path is fine today.

---

### J4 — API request bodies use raw dict, not pydantic models [Spec]

**Where:** `tiri/api/routes/management.py`, `tiri/api/routes/conversations.py`.

**State.** `POST /rooms` and `PATCH /rooms/{id}` accept `dict[str, Any]` and delegate validation to `RoomConfig.from_dict()` + `__post_init__`. This avoids duplicating every `RoomConfig` field in a pydantic shadow model, but it means OpenAPI exposes `additionalProperties: true` for room bodies — client codegen tools see opaque dicts rather than typed shapes.

**Fix.** If client codegen or strict API contracts become important, introduce pydantic request models as a thin layer in front of `from_dict()`. No behavior change required — just type exposure. Keep the validation logic in `__post_init__` as the canonical layer; pydantic only describes the wire shape.

---

### J3 — `DatabricksVectorProvider.list_ids` uses zero-vector approximation [Spec]

**Where:** `tiri/providers/databricks/vector.py`.

**State.** `list_ids()` queries the Vector Search index with a zero-vector and `num_results=10_000` as a workaround for the absence of a native enumeration API. This is bounded and acceptable for the example-store use case (rooms with < 10k examples).

**Fix.** If the Direct Access index REST API gains a native list/scan endpoint, replace the workaround. Re-evaluate if a room approaches the 10k example boundary — at that point we either page the query, query the underlying Delta source directly, or both.

---

### J2 — DbtMetadataProvider not implemented [Spec]

**Where:** `tiri/providers/local/metadata_dbt.py`, `tiri/container.py`.

**State.** `container.py` wires `type = "dbt"` to a `DbtMetadataProvider` stub that raises `MetadataProviderError("not yet implemented")` on `enrich()`. The spec is in `docs/metadata.md`: `description` from `node.description`; `is_primary_key` inferred from `unique` + `not_null` tests; `recommended_joins` from `relationships` tests; `catalog_path` optional for row counts.

**Fix.** Implement when a customer has a dbt project to integrate. Separate from J1 because the implementations diverge — Delta needs a warehouse and a known table schema, dbt needs `manifest.json` parsing.

---

### I2 — Consider RouterLLMProvider's home for EXT-3 [Cosmetic / future]

**Where:** `tiri/container.py`.

**Observation.** `RouterLLMProvider` currently lives in `container.py` because that's where it's wired. Agents type-hint against `LLMProvider` (the ABC), not `RouterLLMProvider`, so today this works fine — only `container.py` and tests need to import it.

When EXT-3 lands, the router gains real multi-backend logic and may need to be imported by code that doesn't already depend on the whole container (e.g. test helpers, alternative wiring paths, the FastAPI app). At that point consider moving it to `tiri/providers/router.py` (or alongside the ABCs in `tiri/providers/base.py`). Not a blocker today; flag for EXT-3.

---

## Resolved — current round (K1, K2, K3, EXT-6 fix, doc-pass cleanup)

- **K1** (`VectorProvider.list_ids` not in `providers.md`) → **resolved** in the Step 12 doc revision pass. Method now documented on the ABC with the [[knowledge_store]] `ExampleIndexer` deletion-bookkeeping rationale spelled out inline.
- **K2** (`Config.databricks_host` / `databricks_token` not in `configuration.md`) → **resolved** in the same pass. Both fields now appear in the `Config` dataclass listing alongside the other provider settings, with a note about CLI `import-genie --space-id` consumption.

## Resolved — earlier (H1–H9, K3, EXT-6 fix)

- **K3** (`docs/api.md` missing X-Forwarded-Access-Token fallback) → **resolved**. EXT-6 doc cleanup added the fallback to the auth section and test cases 8b/8c. Plus, the end-to-end mock chain test `tests/unit/test_ext6_token_chain.py` caught a real bug: `SQLAgent.run()` was calling `query.validate(candidate)` without `user_token=`, so EXPLAIN ran as the service principal even when user credentials were present. Fixed by adding `user_token` to `SQLAgent.run()` and forwarding to `validate()`; `RoomEngine` passes the token from both `chat()` and `stream_chat()`. This means EXT-6 now applies to ALL warehouse calls — not just `execute()`. Without this fix, a user without SELECT on a table would have passed validate (service has access) and then hit a permission error on execute — the exact scenario integration test #2 was designed to detect.


- **H1** (`LLMProvider.complete()` missing `task=`) → **resolved**. Abstract `complete()` and `stream()` now declare `task: str = "sql"` with a comment explaining single-backend impls MUST accept it and MAY ignore it. SQLAgent call site (`agents.md` line 170) matches.
- **H2** (Supply Chain benchmark joined `tpch.sf1.region` not in the room's tables) → **resolved**. Supply config now lists `tpch.sf1.region` in `tables`, includes the nation→region `JoinSpec`, and `text_instruction` lists region in table naming. `demo.md` line 109 "Tables" updated to include `region`.
- **H3** (providers.md said "Five abstract interfaces") → **resolved**. Now says "Six".
- **H4** (CLAUDE.md layout omitted `metadata.md`/`concept_map.md`) → **resolved**. Both listed.
- **H5** (CLAUDE.md "What to flag" said "EXT-1 through EXT-7") → **resolved**. Now "EXT-1 through EXT-11".
- **H6** (CLAUDE.md `providers/local/` omitted `llm_ollama.py`) → **resolved**. Now listed.
- **H7** (`docs/flags/` referenced but didn't exist) → **resolved**. CLAUDE.md now says "append to `fixme.md` at the repo root".
- **H8** (`SQLResult` construction missing required fields) → **resolved**. New shape `(is_valid, attempts, sql="", explanation="", error=None)`. All call sites use keyword args; reordering is safe.
- **H9** (SQLAgent helpers undefined) → **resolved**. One-line note added clarifying they're internal helpers in `sql_agent.py`.

---

## Resolved — prior rounds (audit trail)

- **G1** demo configs mismatched JoinSpec/RoomConfig shape → flat schema + required fields in place.
- **G2** CLAUDE.md Step 12 missing EXT-11 → added.
- **G3** `data_models.md` missing `## Test cases` heading → present.
- **G4** `QueryProvider` missing `user_token` → present on `execute()` and `validate()`.
- **G5** Step 4 implicitly required EXT-3 RouterLLMProvider → MVP note added.
- **G6** `room_engine.md` invariant #1 implied double-validate → clarified.
- **G7** SQLAgent pseudocode referenced undeclared `messages` → initialization shown.
- **G8** `VizAgent.run` had two signatures → consolidated to one.
- **G9** Anthropic-embed validation depended on EXT-3 → covered by Step 4 MVP note.
- **F3/F4/F5** prior-round flags → resolved before this round.
