---
tags: [layer/infrastructure]
status: stable
depends_on: [data_models]
---

# Providers

## In this system

**Linked from:** [[README]], [[knowledge_store]], [[agents]], [[room_engine]], [[feedback]]
**Links to:** [[data_models]], [[databricks_providers]], [[local_providers]]
**Layer:** infrastructure

---

## What this is

Seven abstract interfaces (Python ABCs) that isolate every external I/O dependency from the engine. The [[agents]] and [[room_engine]] import *only* these interfaces — never a concrete implementation. Swapping Databricks for another system means writing a new implementation file; the engine is untouched.

Default implementations are in [[databricks_providers]]. Development/test implementations are in [[local_providers]].

---

## The seven providers

| Provider | Abstracts | Default impl |
|---|---|---|
| `LLMProvider` | LLM completions, streaming, embeddings | Databricks Model Serving |
| `CatalogProvider` | Physical schema — what tables and columns *exist* | Unity Catalog |
| `MetadataProvider` | Semantic metadata — what tables and columns *mean* | UC Annotations (layer 2+) |
| `QueryProvider` | SQL execution and validation | Databricks SQL Warehouse |
| `VectorProvider` | Vector upsert and similarity search | Databricks Vector Search |
| `StoreProvider` | Key-value persistence | Delta table via SQL Warehouse |
| `MCPProvider` | MCP tool consumption — call external MCP servers | `HttpMCPProvider` |

**`CatalogProvider` vs `MetadataProvider`:** `CatalogProvider` answers "what exists" — a structural query against the catalog. `MetadataProvider` answers "what does it mean" — a semantic query that may draw from YAML files, Delta tables, dbt manifests, or other sources. They have different data sources, different failure modes, and different update frequencies. Keeping them separate is intentional. See [[metadata]] for the full stack design.

---

## LLMProvider

Abstracts all calls to language models: completions, streaming, and embeddings. The embedding method is on this interface (not a separate provider) because in practice you use the same vendor for both.

```python
class LLMProvider(ABC):

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        # Routing hint for RouterLLMProvider. Single-backend implementations
        # MUST accept this parameter from day one and MAY ignore it.
        # Valid values: "intent" | "sql" | "planning" | "synthesis" |
        #               "clarify" | "viz_summary"
        # RouterLLMProvider (EXT-3) uses this to route to the correct backend.
    ) -> LLMResponse:
        """Single-shot completion. Used by all agents."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        # Same routing hint as complete(). See above.
    ) -> AsyncIterator[str]:
        """Token-by-token streaming. Used by room_engine.stream_chat()."""
        ...

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch embed. Always routes to the 'embed' task. No task= parameter needed."""
        ...
```

**Supporting types** (defined in [[data_models]] — `LLMMessage`, `LLMResponse`):

`LLMMessage` has `role: str` ("system" | "user" | "assistant") and `content: str`.
`LLMResponse` has `content: str`, `usage: dict` (prompt/completion token counts), and `raw: Any`.

**Contract (MUST):**
- `complete()` MUST return deterministically at `temperature=0.0` for the same input
- `stream()` MUST yield the same total content as `complete()` would for the same input
- `embed()` MUST return one vector per input text, in the same order
- All methods MUST raise `LLMProviderError` (not raw HTTP errors) on failure

---

## CatalogProvider

Abstracts physical schema retrieval — what tables and columns *exist*, their data types, and row counts. Does not provide semantic metadata (descriptions, synonyms, grain). That is `MetadataProvider`'s job.

```python
class CatalogProvider(ABC):

    @abstractmethod
    async def get_table_meta(self, full_name: str) -> TableMeta:
        """
        Fetch physical schema for one table.
        Returns a TableMeta with columns populated but descriptive fields empty.
        Raises TableNotFoundError if the table does not exist or caller lacks permission.
        """
        ...

    @abstractmethod
    async def list_tables(self, catalog: str, schema: str) -> list[str]:
        """List fully-qualified table names in a schema."""
        ...

    @abstractmethod
    async def search_tables(self, query: str, limit: int = 10) -> list[TableMeta]:
        """Find tables by name similarity. Used by management API and EXT-2."""
        ...
```

**Contract (MUST):**
- `get_table_meta()` MUST raise `TableNotFoundError` if the table does not exist or the caller lacks permission
- `get_table_meta()` MUST populate `columns` with physical schema (name, data_type) but MUST leave descriptive fields (`description`, `synonyms`, etc.) empty — those are for `MetadataProvider`
- `list_tables()` MUST return only tables the caller has SELECT permission on

---

## MetadataProvider

Abstracts semantic metadata enrichment. Multiple implementations stack in priority order. See [[metadata]] for the full stack design, merge rules, and YAML format.

```python
class MetadataProvider(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for provenance tracking. Used in MetadataConflict records."""
        ...

    @abstractmethod
    async def enrich(
        self,
        tables: dict[str, TableMeta],   # keyed by full table name; mutate in place
        room_config: RoomConfig,
    ) -> None:
        """
        Enrich TableMeta objects with metadata from this source.

        Rules (MUST):
        - Only set fields where you have data. Leave others at their current value.
        - Scalar fields: assign directly (last-writer-wins via stack order).
        - List fields (synonyms, sample_values, recommended_joins, metadata_sources):
          extend with +=, never replace with =.
        - Append self.name to table.metadata_sources for every table you touch.
        - Record MetadataConflict when overriding a non-empty scalar field.
        """
        ...
```

**Contract (MUST):**
- `enrich()` MUST extend list fields, never replace them
- `enrich()` MUST append `self.name` to `TableMeta.metadata_sources` for each table it modifies
- `enrich()` MUST record a `MetadataConflict` when overriding a non-empty scalar field
- `enrich()` MUST silently skip tables it has no data for — never raise for missing tables
- `enrich()` MUST NOT modify `TableMeta.full_name`, `ColumnMeta.name`, or `ColumnMeta.data_type` — these are physical facts owned by `CatalogProvider`

---

## QueryProvider

Abstracts SQL execution. The only provider that runs user-facing queries against real data. Validation MUST be called before execution — this is enforced by [[room_engine]], but every implementation MUST support it.

```python
class QueryProvider(ABC):

    @abstractmethod
    async def execute(
        self,
        sql: str,
        limit: int = 10_000,
        user_token: str | None = None,
        # EXT-6: when provided, execute as this user rather than the service account.
        # None = use service account credentials (default, MVP behavior).
        # All implementations MUST accept this parameter from day one even if they
        # ignore it until EXT-6 is implemented — avoids a breaking signature change later.
    ) -> QueryResult:
        """Execute SQL and return results, capped at limit rows."""
        ...

    @abstractmethod
    async def validate(
        self,
        sql: str,
        user_token: str | None = None,
        # EXT-6: validate with the user's permissions, not the service account's.
    ) -> tuple[bool, str | None]:
        """
        Dry-run / EXPLAIN. Returns (is_valid, error_message).
        MUST NOT execute the query or return any data rows.
        """
        ...
```

**Contract (MUST):**
- `execute()` MUST set `QueryResult.truncated = True` if results were capped
- `validate()` MUST NOT execute the query — no side effects
- `validate()` MUST return `(False, error_message)` for any SQL that would fail at execution time
- Both methods MUST raise `QueryProviderError` on infrastructure failure (warehouse down, timeout)

---

## VectorProvider

Abstracts vector similarity search. Used by [[knowledge_store]] to index example SQLs and retrieve similar ones at query time.

```python
class VectorProvider(ABC):

    @abstractmethod
    async def upsert(
        self,
        id: str,
        vector: list[float],
        payload: dict,
    ) -> None:
        """Insert or replace a vector entry."""
        ...

    @abstractmethod
    async def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filter: dict | None = None,
    ) -> list[VectorMatch]:
        """Return top_k most similar entries."""
        ...

    @abstractmethod
    async def delete(self, id: str) -> None:
        """Remove an entry by id. No-op if not found."""
        ...

    @abstractmethod
    async def list_ids(self, filter: dict | None = None) -> list[str]:
        """Return all ids matching the filter, used for deletion bookkeeping.

        [[knowledge_store]] `ExampleIndexer` calls this to diff the current
        room's indexed examples against `RoomConfig.examples` and delete
        entries that have been removed. Without this, removed examples
        would leak in the vector store across config updates.
        """
        ...
```

**Supporting type** (defined in [[data_models]] — `VectorMatch`):

`VectorMatch` has `id: str`, `score: float` (higher = more similar), and `payload: dict` ({"question", "sql", "room_id"}).

**Contract (MUST):**
- `query()` results MUST be ordered by descending score
- `upsert()` with an existing `id` MUST replace the entry, not duplicate it
- `filter` dict MUST support at minimum `{"room_id": "<value>"}` for scoping results to one room

---

## StoreProvider

Abstracts key-value persistence for room configs, conversation history, and feedback. The interface is intentionally minimal — a key-value store, not a relational database.

```python
class StoreProvider(ABC):

    @abstractmethod
    async def get(self, key: str) -> dict | None:
        """Return the value for key, or None if not found."""
        ...

    @abstractmethod
    async def put(self, key: str, value: dict) -> None:
        """Insert or replace value at key."""
        ...

    @abstractmethod
    async def list_keys(self, prefix: str) -> list[str]:
        """Return all keys with the given prefix."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove key. No-op if not found."""
        ...
```

**Key naming conventions** (enforced by callers, not this interface):
```
room:{room_id}:config
conv:{conv_id}:turn:{turn_id}
conv:{conv_id}:index           ← sorted list of turn_ids for a conversation
feedback:{conv_id}:{turn_id}
```

**Contract (MUST):**
- `get()` MUST return `None` for a missing key, not raise an exception
- `put()` MUST be atomic — a concurrent reader MUST see either the old or new value, never a partial write
- `list_keys()` MUST return keys in lexicographic order

---

## MCPProvider

Abstracts consumption of external MCP (Model Context Protocol) servers as tools during the reasoning pipeline. Used by [[knowledge_store]] `MCPResolver` to resolve ambiguous terms and fetch external context before SQL generation. Added by EXT-5.

```python
@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict    # JSON Schema for this tool's arguments

@dataclass
class MCPToolResult:
    tool_name: str
    content: str
    is_error: bool        # True when the tool ran but returned an error
                          # (distinct from transport failures which raise MCPProviderError)

class MCPProvider(ABC):

    @abstractmethod
    async def list_tools(self) -> list[MCPTool]:
        """Return all tools available on this MCP server."""
        ...

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict) -> MCPToolResult:
        """
        Call a tool by name with the given arguments.
        Returns MCPToolResult — isError=True for tool-level errors.
        Raises MCPProviderError for transport failures (timeout, network, malformed response).
        """
        ...
```

**Failure split:**
- **Transport failures** (timeout, network error, malformed JSON-RPC response) → raise `MCPProviderError`
- **Tool-level errors** (the tool ran but produced an error result) → return `MCPToolResult(is_error=True)`

This mirrors the MCP protocol's own distinction between protocol errors and tool errors.

**Contract (MUST):**
- `call_tool()` MUST respect the per-call timeout configured at construction (default 5s)
- `call_tool()` MUST raise `MCPProviderError` on transport failure, never block indefinitely
- Neither method MUST raise for tool-level errors — return `MCPToolResult(is_error=True)` instead

**Authorization boundary (enforced by RoomEngine, not this interface):**
Only MCP servers listed in `RoomConfig.mcp_servers` may be called. An empty `mcp_servers` list means zero MCP calls regardless of what providers are registered. This is a security boundary — room authors explicitly opt in to external reach.

**Concrete implementation:** `HttpMCPProvider` in `tiri/providers/local/mcp_http.py` uses JSON-RPC 2.0 over HTTP (Streamable HTTP transport) — the same protocol Tiri uses for EXT-4 server exposure. Injectable `httpx.AsyncClient` for testability.

---

## Error types

All provider errors inherit from `ProviderError`. Callers catch `ProviderError` — never raw HTTP or SDK exceptions.

```python
class ProviderError(Exception): ...
class LLMProviderError(ProviderError): ...
class CatalogProviderError(ProviderError): ...
class TableNotFoundError(CatalogProviderError): ...
class MetadataProviderError(ProviderError): ...
class QueryProviderError(ProviderError): ...
class VectorProviderError(ProviderError): ...
class StoreProviderError(ProviderError): ...
class MCPProviderError(ProviderError): ...
```

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | `LLMProvider.complete()` at `temperature=0.0` called twice | MUST return identical `content` |
| 2 | `LLMProvider.embed([text1, text2])` | MUST return exactly 2 vectors in input order |
| 3 | `CatalogProvider.get_table_meta()` for nonexistent table | MUST raise `TableNotFoundError` |
| 4 | `CatalogProvider.get_table_meta()` | MUST return empty descriptive fields — not populate from catalog comments |
| 5 | `QueryProvider.validate()` with a syntax error | MUST return `(False, non-empty string)` |
| 6 | `QueryProvider.validate()` | MUST NOT appear in query history on the warehouse |
| 7 | `QueryProvider.execute()` with `limit=5` on a 100-row table | MUST return 5 rows and `truncated=True` |
| 8 | `VectorProvider.upsert()` same id twice | MUST result in exactly one entry |
| 9 | `VectorProvider.query()` with `filter={"room_id": "x"}` | MUST NOT return entries from room "y" |
| 10 | `StoreProvider.get()` missing key | MUST return `None`, not raise |
| 11 | `MetadataProvider.enrich()` for a table it has no data for | MUST skip silently, not raise |
| 12 | `MetadataProvider.enrich()` for a table with existing synonyms | MUST extend, not replace the synonyms list |
| 13 | `MetadataProvider.enrich()` overriding a non-empty description | MUST record a `MetadataConflict` |
| 14 | `MetadataProvider.enrich()` | MUST NOT modify `full_name`, `column.name`, or `column.data_type` |
| 15 | Any provider method on infrastructure failure | MUST raise the appropriate `ProviderError` subclass |
| 16 | `MCPProvider.list_tools()` and `call_tool()` on transport failure (timeout, network) | MUST raise `MCPProviderError` — never block indefinitely |
| 17 | `MCPProvider.call_tool()` when remote tool returned an error | MUST return `MCPToolResult(is_error=True)`, NOT raise |
| 18 | `MCPProvider.call_tool()` arguments | MUST honor the per-call timeout configured at construction (default 5s) |
