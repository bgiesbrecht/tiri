# Changelog

All notable changes to Tiri are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [Semantic Versioning](https://semver.org/) (pre-1.0:
the API is subject to change between minor versions).

## [0.1.0] — 2026-05-22

First feature drop on top of the MVP. Adds the QA/demo UI, the table
metadata inspector, schema-level metadata across the whole stack, and
multi-LLM per-question routing. Plus robustness fixes that surfaced
during live validation against the Databricks workspace.

### Added

- **QA / demo UI** (`ui/`) — Vite + React + TypeScript + Tailwind +
  shadcn/ui (Radix primitives). Four-tab shell: Rooms, Ask, Benchmarks,
  History. Warm-earth design tokens with light + dark themes. Bundle
  served by FastAPI at `/app` with SPA fallback. Includes:
  - SSE streaming chat with progressive section rendering (status, SQL,
    result, viz, synthesis, hypotheses, clarify, done)
  - Per-question backend selector with color-coded provider badges
  - Session-only credential override sheet
- **Table metadata inspector** — runs the full metadata stack (catalog
  → external providers → room-config overrides) and exposes the merged
  `TableMeta` on three surfaces:
  - `GET /rooms/{id}/tables` — list view with per-column conflict slice
  - `GET /rooms/{id}/tables/{table_name:path}` — single-table detail
  - `tiri inspect-table --room <id> [<fqn>]` CLI command
  - UI "Tables" tab inside the Rooms card inspector
- **Schema-level metadata** — `SchemaMeta` dataclass and
  `ContextPackage.schema_meta` field. New
  `MetadataProvider.enrich_schemas()` method with a default no-op so
  every existing provider stays backwards compatible. The YAML format
  gains an optional top-level `schemas:` block. SQLAgent prompts now
  include a `## Schema context` section above per-table schemas.
- **Multi-LLM routing** — `SingleModelLLMProvider` routes every task
  type to one backend+model (with a separate embed provider for
  Anthropic/Ollama backends that have no embed API).
  `RoomEngine.chat()` and `stream_chat()` accept `model_override` to
  pin a single backend per-question; the default RouterLLMProvider
  remains in place when no override is supplied.
- **Config endpoints**:
  - `GET /config/routing` — provider names + task routing (never
    credential values)
  - `POST/DELETE /config/credentials` — session-only credential
    overrides with provider-type validation
- **docs/technical-overview.md** — single-pass architectural overview
  for Databricks engineers.

### Changed

- `MetadataFetcher` gains `fetch_schemas()` and `fetch_all()`; `fetch()`
  is unchanged for backwards compatibility.
- `ContextBuilder.build()` switches to `fetch_all()` and attaches
  `schema_meta` to every `ContextPackage`.
- `sql_generation.txt` prompt template adds `{schema_context}`
  placeholder above the table schemas section.
- `StaticMetadataProvider` accepts an optional `schemas` dict alongside
  `data`.
- `YAMLMetadataProvider` parses an optional `schemas:` block; rejects
  misconfigured (list-shaped) schemas with `MetadataProviderError`.
- `demo/tpch_metadata.yaml` includes a `tpch.sf1` schema block (date
  range, scale factor, synthetic-data caveat) so the SQL agent has
  cross-table context.

### Fixed

- `AnthropicLLMProvider`: cap SDK timeout at 120s with 1 retry. The
  SDK's defaults (600s × 3 retries) meant a stuck request could hang
  the UI for half an hour without surfacing an error.
- `DatabricksLLMProvider`: auto-retry without the `temperature`
  parameter when the proxy rejects it for reasoning models; fallback
  to the Responses API path for GPT-5; better directive placeholder
  for Gemini-via-Databricks reasoning models.
- Test count: 446 → 467 (21 new tests covering the inspector, schema
  metadata, format helpers, and config routes).

## [0.0.1] — 2026-05-21

Initial release. Feature-complete as of Steps 1–12 + CLI per
`CLAUDE.md`. Validated end-to-end against a live Databricks workspace
on the `tpch-sales` and `tpch-supply` benchmark rooms with multiple
LLM backends (Anthropic Sonnet 4.6, Claude Opus 4.7, Llama 3.3 70B,
Qwen 2.5).

### Added

- Core data-reasoning pipeline: IntentAgent → PlanningAgent → SQLAgent
  → SynthesisAgent → VizAgent, with optional HypothesisAgent.
- Provider abstractions: `LLMProvider`, `CatalogProvider`,
  `QueryProvider`, `VectorProvider`, `StoreProvider`, `MetadataProvider`,
  `MCPProvider`.
- Databricks production implementations (LLM, Catalog, Query, Vector,
  Store) and local dev implementations (OpenAI, Anthropic, Ollama,
  Static, YAML, DuckDB, Chroma, SQLite).
- FastAPI server with REST + SSE streaming, MCP server endpoint, and
  the eight EXT-* extensions (multi-backend routing, wildcard tables,
  per-user UC enforcement, MCP host/consumer, planning, synthesis,
  hypothesis mode).
- CLI: `load-room`, `ask`, `benchmark`, `dump`, `serve`, `import-genie`.
- Causal-language ban enforced structurally in `SynthesisAgent` and
  `HypothesisAgent`.

[0.1.0]: https://github.com/bgiesbrecht/tiri/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/bgiesbrecht/tiri/releases/tag/v0.0.1
