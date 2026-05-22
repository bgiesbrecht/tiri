# Tiri

A data reasoning system — a natural-language interface to structured data that reasons across multiple queries, shows its work, and tells you what it cannot determine.

Named after **Tiresias**, the Greek prophet who revealed what was already true rather than predicting the future. Tiri does the same: it surfaces what the data actually says, with evidence, without bluffing.

Tiri is built on the Databricks platform alongside Genie — using the same foundation of Unity Catalog, Model Serving, and SQL Warehouses — and extends into reasoning questions that require multi-query planning, synthesis, explicit uncertainty, and a hard architectural ban on causal claims from observational data.

---

## What it is, in one screen

A user asks a business question in plain English. Tiri:

1. **Classifies the intent** — is this a question the room's data can answer?
2. **Plans the reasoning** — one query, or several? What's the dependency structure?
3. **Generates and validates SQL** — every query is `EXPLAIN`-checked against the warehouse before execution. Per-user credentials thread through end-to-end so Unity Catalog enforces row/column-level access at the validate boundary, not just at execute.
4. **Executes against the warehouse** — Databricks SQL Warehouse, DuckDB, or any `QueryProvider`.
5. **Synthesizes a defensible answer** — names what the data supports, what it does not support, and what additional data would be needed. Quantifies confidence with a one-sentence rationale.
6. **Renders a chart** — rule-based Vega-Lite (no LLM-generated JSON).
7. **Optionally generates hypotheses** — only in rooms that explicitly opt in, always provisional, always with contradicting evidence required.

It also exposes itself as an MCP server so any MCP-compatible client (Claude, Cursor, VS Code, other agents) can call Tiri rooms as tools — and consumes external MCP servers as authorized tools during the reasoning pipeline.

---

## The non-negotiable rule

**Tiri is a witness, not an analyst.** It never claims causation from observational data. The phrases `caused by`, `because of`, `due to`, `result of`, `led to` are forbidden in synthesized answers and hypothesis statements — enforced by a structural post-generation scan that raises rather than ships. In the one place where causal claims are useful (research, exploration), the agent is explicitly named `HypothesisAgent`, returns provisional candidates only, requires evidence both for AND against each hypothesis, and is opt-in per room.

The full reasoning is in [`docs/vision.md`](docs/vision.md). Every design decision in the codebase traces back to it.

---

## Quick start (local)

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Configure for fully-local development
cp .env.example .env.local
# Set LLM_PROVIDER=openai + OPENAI_API_KEY=<your key>, or use Ollama.
source .env.local

# 3. Run the unit test suite (no external calls)
pytest tests/unit/

# 4. Load a demo room and ask a question
python -m tiri.cli load-room demo/tpch_sales_config.json
python -m tiri.cli ask --room tpch-sales "What is our total revenue by region?"
```

The CLI also supports `benchmark`, `dump`, `serve` (FastAPI via uvicorn), and `import-genie` (translate an existing Databricks Genie Space export into a Tiri `RoomConfig`).

For Databricks-hosted operation, see [`docs/configuration.md`](docs/configuration.md).

---

## Architecture

```
question + ContextPackage
        │
        ▼
  IntentAgent ──── out_of_scope ──── error
        │
        ├─── clarify_needed ──── ClarifyAgent
        │
        ▼
  PlanningAgent ─── ReasoningPlan (1–5 steps)
        │
        ▼
  SQLAgent × N steps  ──── validate() → execute() per step
        │
        ▼
  SynthesisAgent ─── SynthesizedAnswer (with confidence + uncertainty)
        │
        ▼
  HypothesisAgent (only if hypothesis_mode_enabled, multi-step, causal Q)
        │
        ▼
  VizAgent ─── rule-based Vega-Lite + one-sentence summary
```

Seven provider interfaces (Python ABCs) isolate every external I/O dependency from the engine:

- `LLMProvider` — completions, streaming, embeddings (Databricks Model Serving / OpenAI / Anthropic / Ollama)
- `CatalogProvider` — what tables and columns *exist* (Unity Catalog / static JSON)
- `MetadataProvider` — what they *mean* (stacks: UC annotations, YAML, dbt, room-config overrides)
- `QueryProvider` — SQL execution and validation (Databricks Statement Execution API / DuckDB)
- `VectorProvider` — example similarity search (Databricks Vector Search / Chroma)
- `StoreProvider` — persistence (Delta + SQL Warehouse / SQLite)
- `MCPProvider` — external tool consumption (any MCP server)

Engine code (`tiri/engine/`, `tiri/knowledge/`) **never imports an SDK directly** — only the abstract interfaces. Enforced by a static-scan test.

Full architecture lives in [`docs/`](docs/). Start with [`docs/vision.md`](docs/vision.md), then [`docs/README.md`](docs/README.md) (system map), then individual component docs.

---

## Project status

**Feature-complete** as of Steps 1–13 + CLI + live-workspace Definition of Done validation.

| Room | Score | Notes |
|---|---|---|
| `tpch-sales` | **5/5 100% ✅** | All benchmarks pass on Databricks llama-3-3-70b and on Ollama qwen2.5-coder:14b |
| `tpch-supply` | **3/5 60%** | Remaining 2 failures are irreducible semantic gaps in the room's example coverage, not engine bugs. Resolution is example-engineering, not code |

`pytest tests/unit/ tests/integration/`: **426 passed, 3 skipped** in ~3 seconds. The 3 skipped tests require a live Databricks workspace (`INTEGRATION_TESTS=true`) to exercise EXT-6 per-user credential RBAC scenarios.

Open issues are tracked in [`fixme.md`](fixme.md). Future capability work is in [`docs/roadmap.md`](docs/roadmap.md).

---

## Repository layout

```
tiri/                main package
docs/                architecture and requirements (the spec)
tests/unit/          fast, mock-based — < 3s total
tests/integration/   require a live workspace; opt in via INTEGRATION_TESTS=true
demo/                TPC-H sales + supply room configs
CLAUDE.md            instructions for Claude Code working on this repo
step_summary.md      per-step build narrative
fixme.md             open issues and audit trail
```

The full repository structure with extension-by-extension annotations is in `CLAUDE.md`'s "Project layout" section.
