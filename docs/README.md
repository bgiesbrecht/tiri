---
tags: [moc, entry-point]
status: stable
---

# Tiri — system map

This is the entry point for the Tiri architecture. Every component in the system is reachable from here. **Any document not linked from this map, or not linked from a document reachable from this map, is suspect.**

Tiri is a data reasoning system — a natural-language interface to structured data that reasons across multiple queries, shows its work, and tells you what it cannot determine. It is built on the Databricks platform — using the same foundation of Unity Catalog, Model Serving, and SQL Warehouses that Genie uses — and extends into reasoning questions that require planning, multi-query synthesis, and explicit uncertainty.

---

## Start here

Read [[vision]] first. It explains why this system exists, what it is trying to become, and where its responsibilities end. Every design decision in the component docs is traceable back to it. If two technically valid choices conflict, [[vision]] is the tiebreaker.

---

## How to read this map

Each node below is a component document. The indentation shows the primary dependency direction — lower nodes depend on higher ones. Follow links to read a component's behavior, interface, and test requirements. Return here to reorient.

---

## System graph

```
README  (you are here)
│
├── [[vision]]                 ← why this exists; read before anything else
│
├── [[data_models]]            ← foundation: all shared dataclasses
│
├── [[providers]]              ← abstract interfaces for all I/O
│     └── [[databricks_providers]]   implements →  providers
│     └── [[local_providers]]        implements →  providers  (dev/test)
│
├── [[knowledge_store]]        ← metadata fetcher + example indexer
│     uses → providers, data_models, metadata
│
├── [[metadata]]               ← metadata stack: providers, merge rules, YAML format
│     uses → providers, data_models
│
├── [[agents]]                 ← compound agent pipeline
│     uses → providers, data_models, knowledge_store
│     contains: IntentAgent, SQLAgent, ClarifyAgent, VizAgent
│
├── [[room_engine]]            ← pipeline orchestrator
│     uses → agents, providers, data_models
│
├── [[api]]                    ← REST + SSE surface
│     uses → room_engine, data_models
│
├── [[feedback]]               ← collector, proposer, benchmarks
│     uses → providers, data_models, room_engine
│
├── [[extensions]]             ← what makes Tiri different from Genie
│     uses → providers, agents, room_engine, api
│
├── [[configuration]]          ← all env vars, config.py, container wiring
│     uses → providers, databricks_providers, local_providers
│
└── [[concept_map]]            ← Genie and Tiri — concept mapping and scenario guidance
      uses → vision, providers, agents, room_engine, knowledge_store, metadata, extensions, configuration, data_models, feedback

[[roadmap]]                    ← Customer-validated capabilities tabled for future design
      uses → extensions, vision, data_models, metadata, providers

[[tuning]]                     ← Room tuning guide — diagnose and fix benchmark failures
      uses → agents, data_models, configuration, feedback, extensions

[[demo]]                       ← TPC-H demo rooms, benchmarks, first-run guide
      uses → room_engine, knowledge_store, agents, feedback
```

---

## Layers

| Layer | Documents | Purpose |
|---|---|---|
| North star | [[vision]] | Why Tiri exists and what it must never do |
| Foundation | [[data_models]] | Shared dataclasses — no dependencies within the system |
| Infrastructure | [[providers]], [[databricks_providers]], [[local_providers]], [[configuration]] | Abstract I/O interfaces, implementations, and wiring |
| Intelligence | [[knowledge_store]], [[agents]], [[metadata]] | Context assembly, metadata stack, and LLM-driven reasoning |
| Orchestration | [[room_engine]] | Wires agents into a coherent pipeline |
| Surface | [[api]], [[feedback]] | External interface and improvement loop |
| Extensions | [[extensions]] | Additional capabilities for specific integration requirements |
| Reference | [[concept_map]], [[roadmap]], [[tuning]] | Genie-to-Tiri mapping, customer-validated future capabilities, room tuning guide |
| Demo | [[demo]] | TPC-H rooms, benchmarks, first-run guide |

---

## Key design rules

These rules apply system-wide. Each component doc references whichever rules are relevant to it.

1. **Engine has zero I/O.** [[agents]] and [[room_engine]] import only provider interfaces from [[providers]]. Never import `requests`, `databricks`, or any SDK directly in the engine layer.
2. **SQL is validated before execution.** `QueryProvider.validate()` is always called before `QueryProvider.execute()`. No exceptions.
3. **RoomConfig is the source of truth.** Config is not cached beyond a single request. Always reload from `StoreProvider` at request start.
4. **Prompts are files, not f-strings.** All LLM prompt templates live in `engine/prompt_templates/*.txt`. Loaded at startup.
5. **No LLM call for Vega-Lite.** Chart specs are built programmatically in [[agents]]. LLMs produce inconsistent JSON.
6. **Orphan rule.** Any document not reachable from this map, and any code module not covered by a reachable document, is unspecified behavior.

---

## Dependency graph (Obsidian)

Open the Obsidian graph view on this vault to see the live link graph. Each `[[wikilink]]` in any document creates an edge. Nodes with no inbound edges (other than this README) are orphans — investigate before implementing.

Tags in graph view:
- `#layer/foundation` — gray
- `#layer/infrastructure` — teal
- `#layer/intelligence` — purple
- `#layer/orchestration` — amber
- `#layer/surface` — coral

---

## Glossary

**Room** — a configured instance of the system scoped to a set of tables and a knowledge store. Equivalent to a Genie Space.

**Knowledge store** — the collection of instructions, example SQLs, join specs, SQL snippets, and metadata that gives the agents context. Defined in [[data_models]], managed by [[knowledge_store]], consumed by [[agents]].

**Provider** — an abstract interface for one category of external I/O (LLM calls, catalog reads, SQL execution, vector search, persistence). Defined in [[providers]], implemented in [[databricks_providers]] and [[local_providers]].

**ContextPackage** — the assembled bundle of knowledge passed to every agent before any LLM call. Built by [[knowledge_store]], consumed by [[agents]].

**Pipeline** — the sequence IntentAgent → SQLAgent (or ClarifyAgent) → VizAgent, orchestrated by [[room_engine]].

**Benchmark** — a stored question/expected-SQL pair used to evaluate room quality. Managed by [[feedback]].
