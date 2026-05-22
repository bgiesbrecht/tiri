---
tags: [layer/intelligence]
status: stable
depends_on: [providers, data_models]
---

# Knowledge store

## In this system

**Linked from:** [[README]], [[agents]], [[room_engine]]
**Links to:** [[providers]], [[data_models]]
**Layer:** intelligence

---

## What this is

Five modules that together assemble everything the [[agents]] need before making any LLM call. No LLM completion calls are made here — this layer is pure data assembly, indexing, and retrieval.

- **MetadataFetcher** — runs the metadata provider stack to produce fully-resolved `TableMeta` objects for every table in the room
- **ExampleIndexer** — embeds example SQLs into the vector store and retrieves similar ones at query time
- **TableSelector** — expands wildcard table patterns to concrete FQNs using semantic similarity (EXT-2)
- **MCPResolver** — calls authorized MCP servers to resolve ambiguous terms before SQL generation (EXT-5)
- **ContextBuilder** — orchestrates all of the above plus history assembly to produce a complete `ContextPackage`

Together they produce a `ContextPackage` (defined in [[data_models]]) that contains the complete context for one question: resolved schemas, joins, snippets, metrics, MCP context, retrieved examples, and conversation history.

---

## MetadataFetcher

### Responsibility

Orchestrates the metadata stack to produce fully-resolved `TableMeta` objects for every table in a `RoomConfig`. Runs `CatalogProvider` first for physical schema, then applies each `MetadataProvider` in order following the merge rules defined in [[metadata]].

### Interface

```python
class MetadataFetcher:
    def __init__(
        self,
        catalog: CatalogProvider,
        metadata_providers: list[MetadataProvider],
        # Ordered list — applied left to right. RoomConfigMetadataProvider
        # is always appended last automatically; do not include it here.
    ): ...

    async def fetch(
        self,
        config: RoomConfig,
        query: QueryProvider | None = None,
        # Required if any MetadataProvider needs to execute SQL
        # (e.g. UCAnnotationsMetadataProvider for sample_values)
    ) -> dict[str, TableMeta]:
        """
        1. For each table in config.tables:
              TableMeta = await catalog.get_table_meta(table)
              (physical schema only — no descriptions yet)

        2. For each provider in self.metadata_providers:
              await provider.enrich(tables, config)
              (each provider mutates tables in place following merge rules)

        3. Always run RoomConfigMetadataProvider last:
              await RoomConfigMetadataProvider().enrich(tables, config)

        4. Return fully-resolved tables dict.
        Raises TableNotFoundError if any table in config.tables is missing.
        """
```

### Merge rules (applied by each MetadataProvider)

See [[metadata]] for the full specification. Summary:
- **Scalar fields** (`description`, `grain`, `semantic_type`, etc.): last writer wins
- **List fields** (`synonyms`, `sample_values`, `recommended_joins`, `metadata_sources`): all providers accumulate via `extend()`
- **Conflicts**: recorded on `TableMeta.conflicts` when a scalar is overridden

### Caching

`MetadataFetcher` caches the fully-resolved result for the lifetime of one request. It MUST NOT cache across requests — schema or metadata changes must be reflected on the next question. The cache TTL is configurable via `TIRI_METADATA_CACHE_TTL` (default 0 = no cache).

### Sample value population

Sample value collection (running `SELECT DISTINCT` queries) is the responsibility of `UCAnnotationsMetadataProvider` or whichever metadata provider is configured for it — not `CatalogProvider`. This keeps physical schema retrieval fast and separates the concern of "what columns exist" from "what values are in them".

---

## ExampleIndexer

### Responsibility

Two jobs: (1) keep the vector store in sync with `RoomConfig.examples` when the room config changes; (2) retrieve the most similar examples at query time for few-shot prompting.

### Interface

```python
class ExampleIndexer:
    def __init__(self, llm: LLMProvider, vector: VectorProvider): ...

    async def index(self, config: RoomConfig) -> None:
        """
        Called when a room is created or its examples list changes.

        1. Embed all example questions: llm.embed([ex.question for ex in config.examples])
        2. Upsert each into vector store:
              id      = example.id
              vector  = embedding
              payload = {"question": ex.question, "sql": ex.sql, "room_id": config.room_id}
        3. Delete any vector entries for example ids no longer in config.examples
              (fetch current index ids for this room, diff against config.examples ids)
        """

    async def retrieve(
        self,
        question: str,
        room_id: str,
        top_k: int = 5,
    ) -> list[ExampleSQL]:
        """
        Retrieve the top_k most similar examples for a question.

        1. llm.embed([question]) → query vector
        2. vector.query(vector, top_k, filter={"room_id": room_id})
        3. Map VectorMatch.payload back to ExampleSQL dataclass
        """
```

### Why vector retrieval for examples

A room may have dozens of example SQLs. Injecting all of them into every prompt wastes tokens and degrades SQL generation quality (too much noise). Retrieving only the top-5 most similar to the current question keeps the prompt focused. This is the same pattern as RAG, applied to few-shot examples rather than documents.

---

## TableSelector

### Responsibility

Expand wildcard table patterns in `RoomConfig.tables` to concrete FQNs using semantic similarity between the question and table names. Used by `ContextBuilder` before `MetadataFetcher` when wildcard entries are present. Added by EXT-2 (dynamic table selection).

### Interface

```python
class TableSelector:
    def __init__(
        self,
        catalog: CatalogProvider,
        llm: LLMProvider,
    ): ...

    async def select(
        self,
        question: str,
        config: RoomConfig,
        max_tables: int | None = None,   # defaults to config.max_tables_per_query
    ) -> list[str]:
        """
        When config.tables contains explicit FQNs only:
          Return them as-is — no dynamic selection.

        When config.tables contains wildcard patterns (e.g. "tpch.sf1.*"):
          1. catalog.list_tables() for each wildcard scope
          2. llm.embed([question] + all_candidate_table_names) → N+1 vectors
          3. Rank candidates by cosine similarity to question vector
          4. Return top max_tables by score
          5. Always append any join-spec tables not already in the set
        """
```

**Wildcard shapes supported:**
- `catalog.schema.*` — schema wildcard: calls `list_tables(catalog, schema)`
- `catalog.*.*` — catalog wildcard: calls `list_schemas(catalog)` then `list_tables` per schema
- Mixed entries (`["a.b.explicit", "a.b.*"]`) — handled per-entry
- Other shapes (`*.b.c`, `a.b`, `a.b.c.d`) — logged with WARNING and skipped

**Join-spec guarantee:** tables referenced in `RoomConfig.joins` (as `left_table` or `right_table`) are always included regardless of similarity rank. Without this, a join specified by the room author could be silently excluded from context.

**`selection_method` classification:** `TableSelector` returns a string alongside the table list indicating how selection was performed — `"configured"` (all explicit), `"dynamic_search"` (all wildcards), or `"hybrid"` (mixed). `ContextBuilder` copies this into `ContextPackage.table_selection_method`.

---

## MCPResolver

### Responsibility

Call authorized external MCP servers to resolve ambiguous terms in a question before SQL generation. Collects tool results and populates `ContextPackage.mcp_context`. Added by EXT-5 (MCP tool consumption).

### Interface

```python
class MCPResolver:
    def __init__(
        self,
        providers: dict[str, MCPProvider],   # url → provider instance
    ): ...

    async def resolve(
        self,
        question: str,
        allowed_urls: list[str],   # from RoomConfig.mcp_servers
        user_token: str | None = None,
    ) -> list[str]:
        """
        For each URL in allowed_urls that has a registered provider:
          1. Call the provider's first listed tool with {"query": question}
          2. Collect non-error results as strings
          3. Return the collected resolutions

        Empty allowed_urls → returns [] immediately with zero work.
        URLs in allowed_urls not in providers → logged as WARNING, skipped.

        Every failure mode short-circuits gracefully:
          - Per-call timeout (default 5s) → log + skip
          - MCPProviderError → log + skip
          - Tool isError=True → filtered out silently
          - Any other exception → log via logger.exception + skip

        The pipeline MUST NOT raise from MCP resolution.
        """
```

**Zero-overhead path:** when `allowed_urls` is empty OR `providers` is empty, `resolve()` returns `[]` immediately. No embed calls, no network calls, no latency. Rooms without `mcp_servers` configured are completely unaffected by EXT-5.

---

## ContextBuilder

### Responsibility

Orchestrates `MetadataFetcher` and `ExampleIndexer` plus history assembly to produce a complete `ContextPackage` for one question. This is what [[agents]] receive.

### Interface

```python
class ContextBuilder:
    def __init__(
        self,
        catalog: CatalogProvider,
        metadata_providers: list[MetadataProvider],
        query: QueryProvider,
        llm: LLMProvider,
        vector: VectorProvider,
        mcp_providers: dict[str, MCPProvider] | None = None,
    ): ...

    async def build(
        self,
        question: str,
        config: RoomConfig,
        history: list[ConversationTurn],
        history_window: int = 10,
        user_token: str | None = None,
    ) -> ContextPackage:
        """
        0. If config.tables contains wildcards:
             TableSelector.select(question, config) → resolved table list
             (explicit lists bypass this step entirely)

        1. MetadataFetcher(catalog, metadata_providers).fetch(config, query,
             tables_override=resolved_tables)
             → fully-resolved table_schemas (physical + semantic metadata)

        2. ExampleIndexer.retrieve(question, config.room_id)
             → retrieved_examples (top-k similar from vector store)

        3. Assemble from config:
             joins            = config.joins
             sql_snippets     = config.sql_filters + sql_expressions + sql_measures
             metrics          = config.metrics
             domain_knowledge = config.domain_knowledge
             default_filters  = config.default_filters
             text_instruction = config.text_instruction

        4. Trim history to last history_window turns

        5. Return ContextPackage(...)

        One embed call total (for example retrieval). No completion calls.
        MCP resolution happens AFTER build() — RoomEngine calls MCPResolver
        separately and mutates context.mcp_context before agent dispatch.
        """
```

### What ContextPackage contains

See [[data_models]] for the full definition. Summary of what each field comes from:

| Field | Source |
|---|---|
| `room_id` | `RoomConfig.room_id` |
| `table_schemas` | `CatalogProvider` + `MetadataProvider` stack via `MetadataFetcher` |
| `joins` | `RoomConfig.joins` |
| `sql_snippets` | `RoomConfig.sql_filters + sql_expressions + sql_measures` |
| `metrics` | `RoomConfig.metrics` |
| `default_filters` | `RoomConfig.default_filters` |
| `domain_knowledge` | `RoomConfig.domain_knowledge` |
| `text_instruction` | `RoomConfig.text_instruction` |
| `retrieved_examples` | `VectorProvider` via `ExampleIndexer.retrieve()` |
| `mcp_context` | `MCPResolver.resolve()` — called by `RoomEngine` after `ContextBuilder.build()` |
| `table_selection_method` | `TableSelector` (EXT-2) or `"configured"` for explicit table lists |
| `conversation_history` | `StoreProvider` (loaded by [[room_engine]], passed in) |

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | `MetadataFetcher.fetch()` with all tables present | MUST return one fully-resolved `TableMeta` per table |
| 2 | `MetadataFetcher.fetch()` with one missing table | MUST raise `TableNotFoundError` with the missing table name |
| 3 | `MetadataFetcher.fetch()` with two providers (UC → YAML) | YAML description MUST override UC; synonyms from both MUST accumulate |
| 4 | `MetadataFetcher.fetch()` | MUST always apply `RoomConfigMetadataProvider` last |
| 5 | `MetadataFetcher.fetch()` twice in same request | MUST make only one `CatalogProvider` call per table (cached) |
| 6 | `MetadataFetcher.fetch()` with empty metadata_providers list | MUST return TableMeta with physical schema only |
| 7 | `ExampleIndexer.index()` then `retrieve()` | MUST return the most similar example in top result |
| 8 | `ExampleIndexer.index()` after removing an example | MUST delete the removed example from the vector store |
| 9 | `ExampleIndexer.retrieve()` with `room_id` filter | MUST NOT return examples from a different room |
| 10 | `ContextBuilder.build()` | MUST make exactly one `llm.embed()` call |
| 11 | `ContextBuilder.build()` with 20-turn history and `history_window=10` | MUST include only the last 10 turns |
| 12 | `ContextBuilder.build()` | MUST NOT call `llm.complete()` or `llm.stream()` |
| 13 | `TableSelector.select()` with explicit FQN list | MUST return the list unchanged (no embed, no list_tables) |
| 14 | `TableSelector.select()` with `catalog.schema.*` wildcard | MUST expand via `catalog.list_tables(catalog, schema)` and rank by similarity |
| 15 | `TableSelector.select()` with `catalog.*.*` wildcard | MUST expand via `list_schemas` + `list_tables` |
| 16 | `TableSelector.select()` | MUST always include join-spec tables regardless of similarity rank |
| 17 | `TableSelector.select()` with unsupported wildcard shape | MUST log WARNING and skip the entry, not raise |
| 18 | `MCPResolver.resolve()` with empty `allowed_urls` | MUST return `[]` immediately with zero work (no embed, no network) |
| 19 | `MCPResolver.resolve()` with URL not in `providers` registry | MUST log WARNING and skip, not raise |
| 20 | `MCPResolver.resolve()` on per-call timeout | MUST log + skip that server, return partial results from others |
| 21 | `MCPResolver.resolve()` when remote tool returns `is_error=True` | MUST drop the result silently, not propagate as text |
| 22 | `MCPResolver.resolve()` on `MCPProviderError` from any provider | MUST log + skip, never raise out of the pipeline |
