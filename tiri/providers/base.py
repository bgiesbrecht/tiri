"""Tiri provider abstract base classes.

Six ABCs that isolate every external I/O dependency from the engine. The
agents and room_engine import only these interfaces — never a concrete
implementation. Swapping Databricks for another system means writing a new
implementation file; the engine is untouched.

Default production implementations live in tiri/providers/databricks/.
Development/test implementations live in tiri/providers/local/.

See docs/providers.md for the specification and contract requirements.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from tiri.data_models import (
    LLMMessage,
    LLMResponse,
    MCPTool,
    MCPToolResult,
    QueryResult,
    RoomConfig,
    TableMeta,
    VectorMatch,
)


# ────────────────────────────────────────────────────────────────────────────
# Error hierarchy
#
# All provider errors inherit from ProviderError. Callers catch
# ProviderError — never raw HTTP or SDK exceptions.
# ────────────────────────────────────────────────────────────────────────────


class ProviderError(Exception):
    """Base for all provider-layer errors."""


class LLMProviderError(ProviderError):
    """Raised by LLMProvider implementations on failure."""


class CatalogProviderError(ProviderError):
    """Raised by CatalogProvider implementations on failure."""


class TableNotFoundError(CatalogProviderError):
    """The table does not exist or the caller lacks SELECT permission."""


class MetadataProviderError(ProviderError):
    """Raised by MetadataProvider implementations on failure."""


class QueryProviderError(ProviderError):
    """Raised by QueryProvider on infrastructure failure (warehouse down,
    timeout). NOT raised for SQL syntax errors during validate()."""


class VectorProviderError(ProviderError):
    """Raised by VectorProvider implementations on failure."""


class StoreProviderError(ProviderError):
    """Raised by StoreProvider implementations on failure."""


class MCPProviderError(ProviderError):
    """Raised by MCPProvider implementations on transport or protocol failure.

    NOT raised for tool-level errors — those come back as MCPToolResult
    with is_error=True so the caller can decide whether to continue. Only
    actual transport failures (timeout, malformed response, unreachable
    server) raise."""


# ────────────────────────────────────────────────────────────────────────────
# LLMProvider
# ────────────────────────────────────────────────────────────────────────────


class LLMProvider(ABC):
    """Abstracts language-model calls: completion, streaming, embedding.

    The embedding method is on this interface (not a separate provider)
    because in practice you use the same vendor for both.

    Contract (MUST):
      - complete() returns deterministically at temperature=0.0 for the same input
      - stream() yields the same total content as complete() would for the same input
      - embed() returns one vector per input text, in the same order
      - All methods raise LLMProviderError (not raw HTTP errors) on failure
    """

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        """Single-shot completion. Used by all agents.

        `task` is a routing hint for RouterLLMProvider (EXT-3). Single-backend
        implementations MUST accept this parameter from day one and MAY ignore
        it. Valid values: "intent" | "sql" | "planning" | "synthesis" |
        "clarify" | "viz_summary".

        `model` (EXT-3) overrides the provider's default model/endpoint for
        this single call. Forwarded by `RouterLLMProvider` from
        `ModelRoute.model_name` so that one backend can serve multiple models
        (e.g. intent=db::small, sql=db::big on the same backend). When None,
        the provider uses its constructor default. Implementations MUST accept
        this parameter and SHOULD honor it; ignoring it is acceptable only if
        the provider has a single model.
        """
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """Token-by-token streaming. Used by room_engine.stream_chat().

        Accepts the same `task` and `model` routing parameters as complete().
        """
        ...

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch embed. Always routes to the 'embed' task; no task= needed.

        Used by knowledge_store.example_indexer and context_builder.
        """
        ...


# ────────────────────────────────────────────────────────────────────────────
# CatalogProvider
# ────────────────────────────────────────────────────────────────────────────


class CatalogProvider(ABC):
    """Abstracts physical schema retrieval.

    Answers "what exists" — tables, columns, data types, row counts. Does NOT
    populate descriptive fields (description, synonyms, grain). That is the
    MetadataProvider stack's job.

    Contract (MUST):
      - get_table_meta() raises TableNotFoundError if missing or no permission
      - get_table_meta() populates columns with physical schema only;
        descriptive fields remain empty
      - list_tables() returns only tables the caller has SELECT permission on
    """

    @abstractmethod
    async def get_table_meta(self, full_name: str) -> TableMeta:
        """Fetch physical schema for one table.

        Returns a TableMeta with `columns` populated (name + data_type) but
        descriptive fields (description, synonyms, etc.) left at their
        dataclass defaults — those are for MetadataProvider implementations.

        Raises TableNotFoundError if the table does not exist or the caller
        lacks permission.
        """
        ...

    @abstractmethod
    async def list_tables(self, catalog: str, schema: str) -> list[str]:
        """List fully-qualified table names in a schema."""
        ...

    @abstractmethod
    async def list_schemas(self, catalog: str) -> list[str]:
        """List schema names within a catalog.

        Used by EXT-2 dynamic table selection to expand catalog-level
        wildcards (`tpch.*.*`) by enumerating every schema and then every
        table per schema. Implementations that don't support enumeration
        (e.g. a federation source with no catalog API) MAY return an empty
        list; callers handle that by falling back to schema-level wildcards.
        """
        ...

    @abstractmethod
    async def search_tables(
        self, query: str, limit: int = 10
    ) -> list[TableMeta]:
        """Find tables by name similarity. Used by management API and EXT-2."""
        ...


# ────────────────────────────────────────────────────────────────────────────
# MetadataProvider
# ────────────────────────────────────────────────────────────────────────────


class MetadataProvider(ABC):
    """Abstracts semantic metadata enrichment.

    Multiple implementations stack in priority order. See docs/metadata.md for
    the full stack design, merge rules, and YAML format.

    Contract (MUST):
      - enrich() extends list fields (synonyms, sample_values, etc.) — never
        replaces them
      - enrich() appends self.name to TableMeta.metadata_sources for each
        table it modifies
      - enrich() records a MetadataConflict when overriding a non-empty
        scalar field
      - enrich() silently skips tables it has no data for — never raises
        for missing tables
      - enrich() does NOT modify TableMeta.full_name, ColumnMeta.name, or
        ColumnMeta.data_type — these are physical facts owned by
        CatalogProvider
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for provenance tracking. Used in MetadataConflict records."""
        ...

    @abstractmethod
    async def enrich(
        self,
        tables: dict[str, TableMeta],
        room_config: RoomConfig,
    ) -> None:
        """Enrich TableMeta objects in place with metadata from this source.

        Rules:
          - Only set fields where you have data. Leave others unchanged.
          - Scalar fields: assign directly (last-writer-wins via stack order).
          - List fields: EXTEND, do not replace.
          - Append self.name to table.metadata_sources for every table touched.
          - Record MetadataConflict when overriding a non-empty scalar field.
        """
        ...


# ────────────────────────────────────────────────────────────────────────────
# QueryProvider
# ────────────────────────────────────────────────────────────────────────────


class QueryProvider(ABC):
    """Abstracts SQL execution.

    The only provider that runs user-facing queries against real data.
    `validate()` MUST be called before every `execute()` — enforced by
    SQLAgent's self-correction loop and asserted as a RoomEngine invariant.

    Contract (MUST):
      - execute() sets QueryResult.truncated=True if results were capped
      - validate() does NOT execute the query — no side effects
      - validate() returns (False, error_message) for any SQL that would fail
        at execution time
      - Both methods raise QueryProviderError on infrastructure failure
    """

    @abstractmethod
    async def execute(
        self,
        sql: str,
        limit: int = 10_000,
        user_token: str | None = None,
    ) -> QueryResult:
        """Execute SQL and return results, capped at `limit` rows.

        `user_token` (EXT-6): when provided, execute as that user rather than
        the service account. None = service account credentials. All
        implementations MUST accept this parameter from day one even if they
        ignore it pre-EXT-6 — avoids a breaking signature change later.
        """
        ...

    @abstractmethod
    async def validate(
        self,
        sql: str,
        user_token: str | None = None,
    ) -> tuple[bool, str | None]:
        """Dry-run / EXPLAIN. Returns (is_valid, error_message).

        MUST NOT execute the query or return any data rows.
        `user_token` (EXT-6): validate with the user's permissions.
        """
        ...


# ────────────────────────────────────────────────────────────────────────────
# VectorProvider
# ────────────────────────────────────────────────────────────────────────────


class VectorProvider(ABC):
    """Abstracts vector similarity search.

    Used by knowledge_store to index example SQLs and retrieve similar ones
    at query time.

    Contract (MUST):
      - query() results are ordered by descending score
      - upsert() with an existing id replaces the entry, does not duplicate
      - filter dict supports at minimum {"room_id": "<value>"} for scoping
        results to one room
    """

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
        """Return top_k most similar entries, ordered by descending score."""
        ...

    @abstractmethod
    async def delete(self, id: str) -> None:
        """Remove an entry by id. No-op if not found."""
        ...

    @abstractmethod
    async def list_ids(self, filter: dict | None = None) -> list[str]:
        """Return all entry IDs matching `filter`. Order is unspecified.

        Used by `ExampleIndexer.index()` to enumerate what's currently in the
        vector store so it can delete entries that have been removed from
        `RoomConfig.examples`. The same `{"room_id": "<value>"}` filter shape
        supported by `query()` MUST be supported here.
        """
        ...


# ────────────────────────────────────────────────────────────────────────────
# StoreProvider
# ────────────────────────────────────────────────────────────────────────────


class StoreProvider(ABC):
    """Abstracts key-value persistence for room configs, conversation history,
    and feedback.

    Key naming conventions (enforced by callers, see data_models.md store key
    layout):
      room:{room_id}:config
      room:{room_id}:conversations
      conv:{conv_id}:turn:{turn_id}
      conv:{conv_id}:index
      feedback:{conv_id}:{turn_id}

    Contract (MUST):
      - get() returns None for a missing key, does not raise
      - put() is atomic — a concurrent reader sees either the old or new
        value, never a partial write
      - list_keys() returns keys in lexicographic order
    """

    @abstractmethod
    async def get(self, key: str) -> dict | None:
        """Return the value for `key`, or None if not found."""
        ...

    @abstractmethod
    async def put(self, key: str, value: dict) -> None:
        """Insert or replace value at `key`."""
        ...

    @abstractmethod
    async def list_keys(self, prefix: str) -> list[str]:
        """Return all keys with the given prefix, in lexicographic order."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove `key`. No-op if not found."""
        ...


# ────────────────────────────────────────────────────────────────────────────
# MCPProvider — EXT-5 (external tool consumption)
# ────────────────────────────────────────────────────────────────────────────


class MCPProvider(ABC):
    """One external MCP server, addressed by URL. Used by Tiri agents to
    resolve ambiguous business terms or fetch contextual definitions from
    sources that aren't in the warehouse (Confluence, Glean, policy docs).

    Authorization is enforced at the room level — only servers listed in
    `RoomConfig.mcp_servers` may be called for a given room. The engine
    enforces this; provider implementations have no concept of "allowed".

    Failures (transport errors, timeouts) MUST raise `MCPProviderError`.
    Tool-level errors (the remote tool ran and returned an error) MUST
    be returned as `MCPToolResult(is_error=True)`. The pipeline degrades
    gracefully on either — a single MCP failure never blocks a turn.
    """

    @abstractmethod
    async def list_tools(self) -> list[MCPTool]:
        """Return the tools the server exposes."""
        ...

    @abstractmethod
    async def call_tool(
        self, name: str, arguments: dict
    ) -> MCPToolResult:
        """Invoke a tool by name with the given arguments. Returns the
        tool's response as an MCPToolResult. Transport failures raise
        MCPProviderError; tool-level failures come back with is_error=True."""
        ...
