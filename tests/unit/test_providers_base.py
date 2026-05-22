"""Tests for tiri.providers.base — the abstract provider interfaces.

The provider contract tests in docs/providers.md (cases 1–15) describe runtime
behavior of *concrete* implementations and run when those are built (Steps 5
and 6). This file covers what is verifiable at the ABC level: instantiability,
abstract method declaration, and the error type hierarchy.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

import pytest

from tiri.data_models import (
    LLMMessage,
    LLMResponse,
    QueryResult,
    RoomConfig,
    TableMeta,
    VectorMatch,
)
from tiri.providers.base import (
    CatalogProvider,
    CatalogProviderError,
    LLMProvider,
    LLMProviderError,
    MetadataProvider,
    MetadataProviderError,
    ProviderError,
    QueryProvider,
    QueryProviderError,
    StoreProvider,
    StoreProviderError,
    TableNotFoundError,
    VectorProvider,
    VectorProviderError,
)


ABCS = [
    LLMProvider,
    CatalogProvider,
    MetadataProvider,
    QueryProvider,
    VectorProvider,
    StoreProvider,
]


# ── ABCs cannot be instantiated directly ────────────────────────────────────


@pytest.mark.parametrize("abc_class", ABCS, ids=lambda c: c.__name__)
def test_abc_cannot_be_instantiated(abc_class) -> None:
    with pytest.raises(TypeError, match="abstract"):
        abc_class()


# ── Error hierarchy ─────────────────────────────────────────────────────────


def test_provider_error_is_an_exception() -> None:
    assert issubclass(ProviderError, Exception)


@pytest.mark.parametrize(
    "subclass",
    [
        LLMProviderError,
        CatalogProviderError,
        MetadataProviderError,
        QueryProviderError,
        VectorProviderError,
        StoreProviderError,
    ],
    ids=lambda c: c.__name__,
)
def test_provider_subclasses_extend_provider_error(subclass) -> None:
    assert issubclass(subclass, ProviderError)


def test_table_not_found_extends_catalog_provider_error() -> None:
    assert issubclass(TableNotFoundError, CatalogProviderError)
    assert issubclass(TableNotFoundError, ProviderError)


def test_provider_errors_are_raisable() -> None:
    with pytest.raises(ProviderError):
        raise LLMProviderError("boom")
    with pytest.raises(CatalogProviderError):
        raise TableNotFoundError("nope")


# ── Concrete subclass implementing all abstracts can be instantiated ────────


class _StubLLM(LLMProvider):
    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="ok", usage={}, raw=None)

    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        yield "ok"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


class _StubCatalog(CatalogProvider):
    async def get_table_meta(self, full_name: str) -> TableMeta:
        return TableMeta(full_name=full_name)

    async def list_tables(self, catalog: str, schema: str) -> list[str]:
        return []

    async def list_schemas(self, catalog: str) -> list[str]:
        return []

    async def search_tables(
        self, query: str, limit: int = 10
    ) -> list[TableMeta]:
        return []


class _StubMetadata(MetadataProvider):
    @property
    def name(self) -> str:
        return "stub"

    async def enrich(
        self, tables: dict[str, TableMeta], room_config: RoomConfig
    ) -> None:
        return None


class _StubQuery(QueryProvider):
    async def execute(
        self, sql: str, limit: int = 10_000, user_token: str | None = None
    ) -> QueryResult:
        return QueryResult(
            columns=[], rows=[], row_count=0, truncated=False, duration_ms=0
        )

    async def validate(
        self, sql: str, user_token: str | None = None
    ) -> tuple[bool, str | None]:
        return (True, None)


class _StubVector(VectorProvider):
    async def upsert(self, id: str, vector: list[float], payload: dict) -> None:
        return None

    async def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filter: dict | None = None,
    ) -> list[VectorMatch]:
        return []

    async def delete(self, id: str) -> None:
        return None

    async def list_ids(self, filter: dict | None = None) -> list[str]:
        return []


class _StubStore(StoreProvider):
    async def get(self, key: str) -> dict | None:
        return None

    async def put(self, key: str, value: dict) -> None:
        return None

    async def list_keys(self, prefix: str) -> list[str]:
        return []

    async def delete(self, key: str) -> None:
        return None


STUBS = [
    (LLMProvider, _StubLLM),
    (CatalogProvider, _StubCatalog),
    (MetadataProvider, _StubMetadata),
    (QueryProvider, _StubQuery),
    (VectorProvider, _StubVector),
    (StoreProvider, _StubStore),
]


@pytest.mark.parametrize(
    ("abc_class", "stub_class"), STUBS, ids=lambda x: x.__name__
)
def test_complete_subclass_can_be_instantiated(abc_class, stub_class) -> None:
    instance = stub_class()
    assert isinstance(instance, abc_class)


# ── Partial subclass (missing abstracts) cannot be instantiated ─────────────


def test_partial_llm_subclass_cannot_be_instantiated() -> None:
    class _PartialLLM(LLMProvider):
        async def complete(
            self,
            messages: list[LLMMessage],
            temperature: float = 0.0,
            max_tokens: int = 2048,
            task: str = "sql",
            model: str | None = None,
        ) -> LLMResponse:
            return LLMResponse(content="ok", usage={}, raw=None)

        # missing stream() and embed()

    with pytest.raises(TypeError, match="abstract"):
        _PartialLLM()


# ── Abstract method declarations match the docs ─────────────────────────────


def _abstract_methods(cls) -> set[str]:
    return set(getattr(cls, "__abstractmethods__", set()))


def test_llm_provider_abstract_methods() -> None:
    assert _abstract_methods(LLMProvider) == {"complete", "stream", "embed"}


def test_catalog_provider_abstract_methods() -> None:
    assert _abstract_methods(CatalogProvider) == {
        "get_table_meta",
        "list_tables",
        "list_schemas",
        "search_tables",
    }


def test_metadata_provider_abstract_methods() -> None:
    assert _abstract_methods(MetadataProvider) == {"name", "enrich"}


def test_query_provider_abstract_methods() -> None:
    assert _abstract_methods(QueryProvider) == {"execute", "validate"}


def test_vector_provider_abstract_methods() -> None:
    assert _abstract_methods(VectorProvider) == {
        "upsert",
        "query",
        "delete",
        "list_ids",
    }


def test_store_provider_abstract_methods() -> None:
    assert _abstract_methods(StoreProvider) == {
        "get",
        "put",
        "list_keys",
        "delete",
    }


# ── Signature checks for the parameters introduced by H1 / G4 ───────────────


def test_llm_complete_accepts_task_parameter() -> None:
    sig = inspect.signature(LLMProvider.complete)
    assert "task" in sig.parameters
    assert sig.parameters["task"].default == "sql"


def test_llm_stream_accepts_task_parameter() -> None:
    sig = inspect.signature(LLMProvider.stream)
    assert "task" in sig.parameters
    assert sig.parameters["task"].default == "sql"


def test_query_execute_accepts_user_token() -> None:
    sig = inspect.signature(QueryProvider.execute)
    assert "user_token" in sig.parameters
    assert sig.parameters["user_token"].default is None


def test_query_validate_accepts_user_token() -> None:
    sig = inspect.signature(QueryProvider.validate)
    assert "user_token" in sig.parameters
    assert sig.parameters["user_token"].default is None


# ── Module imports cleanly with no side effects ─────────────────────────────


def test_module_imports_cleanly() -> None:
    from tiri.providers import base

    assert base is not None


# ── A stub can actually be used: smoke test the basic flow ──────────────────


@pytest.mark.asyncio
async def test_stub_llm_complete_round_trip() -> None:
    p = _StubLLM()
    resp = await p.complete(
        [LLMMessage(role="user", content="hi")], task="intent"
    )
    assert resp.content == "ok"


@pytest.mark.asyncio
async def test_stub_llm_stream_round_trip() -> None:
    p = _StubLLM()
    chunks = []
    async for c in p.stream([LLMMessage(role="user", content="hi")], task="sql"):
        chunks.append(c)
    assert chunks == ["ok"]


@pytest.mark.asyncio
async def test_stub_query_validate_then_execute() -> None:
    q = _StubQuery()
    ok, err = await q.validate("SELECT 1")
    assert ok is True
    assert err is None
    result = await q.execute("SELECT 1", user_token="user-token-xyz")
    assert result.row_count == 0
