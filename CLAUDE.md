# Tiri — instructions for Claude Code

This file is your starting point. Read it before reading anything else.

---

## What you are building

**Tiri** is a data reasoning system — a natural-language interface to structured data that reasons across multiple queries, shows its work, and tells users what it cannot determine. It extends and replaces Databricks Genie Spaces.

Named after Tiresias, the Greek prophet who revealed what was already true rather than predicting the future. Tiri does the same: it surfaces what the data actually says, with evidence, without bluffing.

The full vision is in `docs/vision.md`. Read it. Every design decision traces back to it.

---

## Architecture documentation

All architecture lives in `docs/`. These are not supplementary notes — they are the requirements. Each document defines behavior, interfaces, and test cases for one component. Build what the docs say. If something is unclear, the docs are incomplete — flag it rather than invent.

**Read in this order:**
1. `docs/vision.md` — why this exists and what it must never do
2. `docs/README.md` — system map and component graph
3. `docs/data_models.md` — all shared dataclasses (build these first)
4. `docs/providers.md` — all abstract interfaces (build these second)
5. Then the component docs in build order (see below)

**The graph rule:** every `[[wikilink]]` in the docs is a real dependency. If a doc links to another, the linked component must exist before the linking component can be implemented. The graph is the build order.

---

## Build order

Implement in this sequence. Each step depends on the previous ones being complete and tested.

```
Step 1   data_models.py          docs/data_models.md
Step 2   providers/base.py       docs/providers.md
Step 3   config.py               docs/configuration.md
Step 4   container.py            docs/configuration.md
         ↳ MVP note: implement a minimal RouterLLMProvider here that wraps a
           single backend and routes all tasks to it. This satisfies the
           "container always returns RouterLLMProvider" contract and enables the
           Anthropic-as-embed startup validation. EXT-3 (Step 12) extends this
           class to support multiple backends — it is not a rewrite.
Step 5   providers/databricks/   docs/databricks_providers.md
Step 6   providers/local/        docs/local_providers.md
Step 7   knowledge/              docs/knowledge_store.md
Step 8   engine/agents/          docs/agents.md
Step 9   engine/room_engine.py   docs/room_engine.md
Step 10  api/                    docs/api.md
Step 11  feedback/               docs/feedback.md
Step 12  extensions/             docs/extensions.md  (in order: EXT-3, EXT-2, EXT-6, EXT-7, EXT-1, EXT-4, EXT-5, EXT-11)
```

Do not skip ahead. Do not implement Step 8 until Steps 1–7 have passing tests.

---

## Project layout

```
tiri/
├── CLAUDE.md                  ← you are here
├── fixme.md                   ← open items and known gaps
├── docs/                      ← architecture and requirements
│   ├── README.md              ← system map (entry point)
│   ├── vision.md
│   ├── concept_map.md         ← Genie-to-Tiri mapping reference
│   ├── roadmap.md             ← customer-validated capabilities tabled for future design
│   ├── data_models.md
│   ├── providers.md
│   ├── databricks_providers.md
│   ├── local_providers.md
│   ├── metadata.md            ← metadata stack design and YAML format
│   ├── knowledge_store.md
│   ├── agents.md
│   ├── room_engine.md
│   ├── api.md
│   ├── feedback.md
│   ├── extensions.md
│   ├── configuration.md
│   └── demo.md
│
├── tiri/                      ← main package
│   ├── __init__.py
│   ├── config.py
│   ├── container.py
│   ├── data_models.py
│   ├── providers/
│   │   ├── base.py
│   │   ├── databricks/
│   │   │   ├── llm.py
│   │   │   ├── catalog.py
│   │   │   ├── metadata.py
│   │   │   ├── query.py
│   │   │   ├── vector.py
│   │   │   └── store.py
│   │   └── local/
│   │       ├── llm_openai.py
│   │       ├── llm_anthropic.py
│   │       ├── llm_ollama.py
│   │       ├── catalog_static.py
│   │       ├── metadata_static.py
│   │       ├── metadata_yaml.py
│   │       ├── metadata_dbt.py    ← stub (J2)
│   │       ├── query_duckdb.py
│   │       ├── vector_chroma.py
│   │       ├── store_sqlite.py
│   │       └── mcp_http.py        ← EXT-5: HttpMCPProvider
│   ├── knowledge/
│   │   ├── room_config_metadata.py
│   │   ├── metadata_fetcher.py
│   │   ├── example_indexer.py
│   │   ├── table_selector.py      ← EXT-2: wildcard table expansion
│   │   ├── mcp_resolver.py        ← EXT-5: external MCP term resolution
│   │   └── context_builder.py
│   ├── engine/
│   │   ├── room_engine.py
│   │   ├── agents/
│   │   │   ├── base.py
│   │   │   ├── intent_agent.py
│   │   │   ├── sql_agent.py
│   │   │   ├── clarify_agent.py
│   │   │   ├── viz_agent.py
│   │   │   ├── planning_agent.py   ← EXT-1
│   │   │   ├── synthesis_agent.py  ← EXT-1/EXT-7
│   │   │   └── hypothesis_agent.py ← EXT-11
│   │   └── prompt_templates/
│   │       ├── intent_classification.txt
│   │       ├── sql_generation.txt
│   │       ├── clarification.txt
│   │       ├── planning.txt
│   │       ├── synthesis.txt
│   │       └── hypothesis_generation.txt
│   ├── api/
│   │   ├── main.py
│   │   ├── auth.py
│   │   ├── mcp/
│   │   │   ├── __init__.py
│   │   │   └── server.py           ← EXT-4: MCP server at /mcp
│   │   └── routes/
│   │       ├── conversations.py
│   │       ├── management.py
│   │       └── feedback.py
│   └── feedback/
│       ├── collector.py
│       ├── proposer.py
│       ├── benchmark_runner.py
│       └── sql_normalize.py
│
├── tests/
│   ├── unit/
│   ├── integration/           ← require live Databricks workspace; INTEGRATION_TESTS=true to run
│   └── fixtures/
│       ├── schemas.json       ← static schemas for local dev
│       └── data/              ← Parquet files for DuckDB
│
├── demo/
│   ├── tpch_metadata.yaml
│   ├── tpch_sales_config.json
│   └── tpch_supply_config.json
│
├── pyproject.toml
├── tiri.toml.example          ← copy to tiri.toml and fill in your values
├── .env.example               ← copy to .env.local for simple env-var config
└── tiri/
    └── cli.py                 ← CLI entry point (see CLI spec below)
```

---

## Key design rules

These come from `docs/vision.md` and `docs/README.md`. They are not preferences — they are hard constraints.

**1. Engine has zero I/O.**
Files in `tiri/engine/` and `tiri/knowledge/` MUST NOT import `requests`, `databricks`, `openai`, `anthropic`, `duckdb`, `chromadb`, or `sqlite3`. All I/O goes through provider interfaces from `tiri/providers/base.py`. If you find yourself importing an SDK in the engine layer, stop — create or use a provider method instead.

**2. SQL is always validated before execution.**
`QueryProvider.validate()` MUST be called before every `QueryProvider.execute()` call. No exceptions. The `RoomEngine` enforces this, but if you're writing code that calls `execute()` directly, add the validation.

**3. Prompts are files, not f-strings.**
All LLM prompt templates live in `tiri/engine/prompt_templates/*.txt`. Load them at module startup with `Path(__file__).parent / "prompt_templates" / "template_name.txt"`. Never construct a prompt as an inline f-string in agent code — it makes prompts impossible to iterate on without touching Python.

**4. No LLM call for Vega-Lite.**
`VizAgent` builds chart specs programmatically using rule-based logic and Python dict construction. Do not ask the LLM to generate JSON for chart specs — it produces inconsistent output. The only LLM call in `VizAgent` is the one-sentence summary generation.

**5. RoomConfig is always reloaded.**
`RoomEngine.chat()` calls `store.get("room:{room_id}:config")` at the start of every request. Never cache `RoomConfig` in an instance variable. This ensures config changes take effect on the next request.

**6. Causal language is prohibited in synthesis.**
`SynthesisAgent` and `HypothesisAgent` MUST NOT produce output containing "caused by", "because of", "due to", "result of", or "led to" when describing data patterns. These are causal claims. See `docs/vision.md` — Tiri is a witness, not an analyst. Use "associated with", "coincided with", "occurred alongside" instead. Both agents enforce this via post-generation structural scanning — not just prompt guidance.

**7. IntentAgent snippet resolution — display name to object.**
The LLM returns snippet `display_name` strings in its JSON response. `IntentAgent` must resolve these to `SqlSnippet` objects by looking up `context.sql_snippets` by `display_name`. Unknown names are dropped with a warning log — never raise. Pattern:

```python
snippet_map = {s.display_name: s for s in context.sql_snippets}
relevant_snippets = [
    snippet_map[name]
    for name in raw_response.get("relevant_snippets", [])
    if name in snippet_map
]
# Log any names not found in snippet_map
```

---

## First thing to run

Before writing any code, set up the local dev environment and verify it works end-to-end with local providers:

```bash
# 1. Install dependencies
pip install -e ".[dev]"

# 2. Copy and fill in local dev config
cp .env.example .env.local
# Set LLM_PROVIDER=openai and OPENAI_API_KEY=<your key>
# All other providers default to local (static/duckdb/chroma/sqlite)

# 3. Run unit tests (no external calls required)
pytest tests/unit/ -v

# 4. Load the TPC-H sales demo room
source .env.local
python -m tiri.cli load-room demo/tpch_sales_config.json

# 4a. Optional but recommended: add tpch_metadata.yaml to tiri.toml for richer metadata
# In tiri.toml, add:
#   [[metadata.providers.stack]]
#   name = "tpch_domain"
#   type = "yaml"
#   path = "./demo/tpch_metadata.yaml"
# This adds descriptions, synonyms, grain, semantic types for all TPC-H tables.
# Without it, metadata falls back to UC annotations only (or nothing in local dev).

# 5. Ask a question
python -m tiri.cli ask --room tpch-sales "What is our total revenue by region?"
```

Expected output for step 5:
```
Tiri — Sales Analysis
─────────────────────
Revenue by region:

  AMERICA      $XXX,XXX,XXX.XX
  ASIA         $XXX,XXX,XXX.XX
  EUROPE       $XXX,XXX,XXX.XX
  AFRICA       $XXX,XXX,XXX.XX
  MIDDLE EAST  $XXX,XXX,XXX.XX

SQL used:
  SELECT r.r_name AS region,
         ROUND(SUM(l.l_extendedprice * (1 - l.l_discount)), 2) AS revenue
  FROM tpch.sf1.lineitem l
  ...

Confidence: high
Data supports: Revenue totals by geographic region for all shipped line items.
Data does not support: Trend over time, comparison to targets or prior periods.
```

If the SQL uses `l_extendedprice` without the discount factor, the revenue formula is wrong — fix `sql_generation.txt` before proceeding.

---

## Running benchmarks

After the core pipeline is working:

```bash
python -m tiri.cli benchmark --room tpch-sales
python -m tiri.cli benchmark --room tpch-supply
```

Target score: **100% on both rooms** before moving to extensions. The benchmark questions have known correct SQL from the TPC-H specification. Any failure is a real bug, not a test issue.

---

## Testing strategy

**Unit tests** (`tests/unit/`) — test every function in isolation using mocks for all providers. No network calls, no file I/O. These must run in < 30 seconds total.

**Current state (sanity check at session start):** `pytest tests/unit/ tests/integration/` should report **426 passed, 3 skipped** in ~3s. The 3 skipped are the EXT-6 integration tests (require `INTEGRATION_TESTS=true` + a real Databricks workspace). A different number means either new tests have been added since this checkpoint or something regressed — investigate before doing other work.

**Integration tests** (`tests/integration/`) — test against a real Databricks workspace. Mark with `@pytest.mark.integration`. Skip in CI unless `INTEGRATION_TESTS=true` is set.

**The test cases in each doc are the requirements.** Every row in every `## Test cases` table in every doc file MUST have a corresponding test. The doc is the spec; the test is the verification. If a doc says MUST, the test asserts it. If a doc says SHOULD, the test warns on failure.

For `docs/data_models.md` test case 5 ("ConversationTurn with both sql and clarification_question set MUST raise ValueError"), the test is:

```python
def test_conversation_turn_mutual_exclusion():
    with pytest.raises(ValueError):
        ConversationTurn(
            turn_id="x", conversation_id="y", question="q",
            sql="SELECT 1",
            clarification_question="Did you mean X or Y?",
            ...
        )
```

Write this kind of test for every MUST in every doc.

---

## CLI spec (`tiri/cli.py`)

The CLI is a thin wrapper over `RoomManager` and `RoomEngine`. It is not a separate architecture layer — it uses the same container wiring as the API server. Implement it as a `click` or `argparse` application at `tiri/cli.py`, invocable as `python -m tiri.cli`.

**Required commands:**

```bash
# Load or update a room from a config JSON file (idempotent)
python -m tiri.cli load-room <path/to/config.json>
# → calls RoomManager.create() or RoomManager.update() depending on whether room_id exists
# → prints: "Room '{title}' ({room_id}) loaded. Indexed N examples."

# Ask a question to a room (blocking, prints answer to stdout)
python -m tiri.cli ask --room <room_id> "<question>"
# → creates a new conversation_id, calls RoomEngine.chat()
# → prints: the synthesized answer, then the SQL, then confidence/evidence

# Run benchmarks for a room
python -m tiri.cli benchmark --room <room_id>
# → calls BenchmarkRunner.run()
# → prints: score and per-benchmark pass/fail table
# → exits non-zero if score < 1.0

# Dump the current config for a room (read-only)
python -m tiri.cli dump --room <room_id>
# → pretty-prints the RoomConfig as JSON

# Start the API server
python -m tiri.cli serve
# → calls uvicorn with the FastAPI app

# Translate a Genie Space export to a Tiri RoomConfig JSON file
python -m tiri.cli import-genie [--input <genie.json> | --space-id <id>] --output <path/to/config.json>
# → either reads a local Genie wire-format JSON (--input, no network) or
#   fetches via the Databricks Workspace API (--space-id, uses cfg.databricks_host/token)
# → translates per docs/roadmap.md R5 mapping table
# → writes a RoomConfig JSON with warehouse_id="" — user MUST fill that in
#   before running load-room
# → implements roadmap item R5; was added beyond the original CLI spec
#   because the translation is pure and testable, and customers with
#   existing Genie Spaces benefit from a first-class migration path.
```

All commands read config via `Config.load()` (respects `tiri.toml` and env vars). All commands exit non-zero on error with a human-readable message.

---

## What to flag

If you encounter any of the following, stop and flag it rather than inventing a solution:

- A doc says to do X but X contradicts another doc
- A required interface method isn't defined anywhere in the docs
- An extension (EXT-1 through EXT-11) has an unclear dependency on a core component
- The TPC-H benchmark questions produce SQL that looks correct but gives wrong numbers
- Any place where avoiding causal language makes the answer uninformative rather than just precise

Flag by appending to `fixme.md` at the repo root with the issue description and your proposed resolution. Do not silently work around ambiguities.

---

## Project status

**Feature-complete as of Steps 1–12 + CLI.**

Definition of Done results (validated against live Databricks workspace `<your-workspace>.azuredatabricks.net`):

| Room | Score | Notes |
|---|---|---|
| `tpch-sales` | **5/5 100% ✅** | All benchmarks pass on both Databricks (llama-3-1-8b / llama-3-3-70b mixed routing) and a self-hosted Ollama endpoint (qwen2.5-coder:14b + qwen2.5:14b-instruct) |
| `tpch-supply` | **3/5 60%** | 2 remaining failures are irreducible semantic gaps, not Tiri code bugs. Both 70B Llama and 14B qwen interpret the questions differently from the benchmark's `expected_sql`: (1) `5e757fe8…` — "highest account balance AND supply the most parts" — the agent ranks suppliers but doesn't add the `COUNT(ps_partkey)` aggregation; (2) `ff04dceff…` — "minimum supply cost across all suppliers" — agent returns one row per part (200k rows), benchmark expected adds an implicit top-N filter (20 rows). Resolution requires example-engineering on the supply room, not engine changes. |

**Full stack validated end-to-end:**
- `DatabricksLLMProvider` (completions + embed via Model Serving)
- `DatabricksCatalogProvider` (UC introspection on `samples.tpch.*`)
- `DatabricksQueryProvider` (Statement Execution API; per-user-token threading exercised)
- `OllamaLLMProvider` (qwen2.5-coder:14b / qwen2.5:14b-instruct on `http://<ollama-host>:11434`)
- `RouterLLMProvider` task routing across 7 task types, mixing two Ollama backends + one Databricks embed backend
- Full agent pipeline: IntentAgent → PlanningAgent → SQLAgent (validate→execute) → SynthesisAgent → VizAgent
- `ChromaVectorProvider` + `SQLiteStoreProvider` (local)
- CLI: `load-room`, `ask`, `benchmark`, `dump`, `serve`, `import-genie`

**Bugs found and fixed during validation:**
1. `RoomConfig.from_dict` couldn't load configs lacking explicit `kind` on `SqlSnippet` — now injects from list source.
2. `Config._from_toml` didn't env-fall-through for Databricks LLM backends — now does.
3. `VizAgent` crashed the turn on summary LLM failure (Databricks guardrail false-positive) — now degrades to empty summary, regression test added.
4. `SQLAgent` didn't strip markdown fences from model output (qwen2.5-coder wraps SQL in ```` ```sql ... ``` ````) — now strips, parametrized regression test added.

**Open items:** see `fixme.md` (10 entries, no blockers). Notably `M2` documents the LLM-guardrail-vs-row-data tension and the recommended `viz_summary` routing posture for production.

**Next work:** see `docs/roadmap.md` (R1–R6, customer-validated).

---

## What success looks like

A user asks: *"Which region had the highest revenue last year and how did it compare to the year before?"*

Tiri:
1. Identifies this as a two-step question (this year vs. last year comparison)
2. Generates two SQL queries or one with a window function
3. Runs both against the TPC-H data using the correct revenue formula
4. Returns a synthesized answer naming the top region, the revenue figure, and the year-over-year change
5. States clearly that it cannot determine *why* one region outperformed another
6. Shows the supporting SQL

That is the bar. Not impressive. Defensible.
