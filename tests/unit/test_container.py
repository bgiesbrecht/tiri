"""Tests for tiri.container — RouterLLMProvider and build_container().

Covers configuration.md test cases 6, 7, 10 (which require build_container())
plus direct tests of RouterLLMProvider routing logic and ModelRoute. Concrete
provider modules (DatabricksLLMProvider, OpenAILLMProvider, …) don't exist
until Steps 5/6; we monkeypatch the lazy factories with stubs.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tiri import container as container_module
from tiri.config import Config, ConfigurationError, ProviderBackendConfig, RoutingConfig
from tiri.container import (
    ModelRoute,
    RouterLLMProvider,
    build_container,
)
from tiri.data_models import LLMMessage, LLMResponse, QueryResult, TableMeta
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    MetadataProvider,
    QueryProvider,
    StoreProvider,
    VectorProvider,
)


# ── Stub providers ──────────────────────────────────────────────────────────


class _StubLLM(LLMProvider):
    def __init__(self, label: str = "stub") -> None:
        self.label = label
        self.complete_calls: list[tuple[str, str, str | None]] = []  # (task, last_message, model)
        self.embed_calls: list[list[str]] = []
        self.stream_calls: list[tuple[str, str | None]] = []  # (task, model)

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        self.complete_calls.append((task, messages[-1].content if messages else "", model))
        return LLMResponse(content=f"{self.label}:{task}", usage={}, raw=None)

    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        self.stream_calls.append((task, model))
        yield f"{self.label}:{task}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(texts)
        return [[0.0] for _ in texts]


class _StubCatalog(CatalogProvider):
    async def get_table_meta(self, full_name: str) -> TableMeta:
        return TableMeta(full_name=full_name)

    async def list_tables(self, catalog: str, schema: str) -> list[str]:
        return []

    async def list_schemas(self, catalog: str) -> list[str]:
        return []

    async def search_tables(self, query: str, limit: int = 10) -> list[TableMeta]:
        return []


class _StubMetadata(MetadataProvider):
    def __init__(self, name: str = "stub_meta") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def enrich(self, tables, room_config) -> None:
        return None


class _StubQuery(QueryProvider):
    async def execute(self, sql, limit=10_000, user_token=None) -> QueryResult:
        return QueryResult(columns=[], rows=[], row_count=0, truncated=False, duration_ms=0)

    async def validate(self, sql, user_token=None) -> tuple[bool, str | None]:
        return (True, None)


class _StubVector(VectorProvider):
    async def upsert(self, id, vector, payload) -> None:
        return None

    async def query(self, vector, top_k=5, filter=None):
        return []

    async def delete(self, id) -> None:
        return None

    async def list_ids(self, filter=None):
        return []


class _StubStore(StoreProvider):
    async def get(self, key):
        return None

    async def put(self, key, value) -> None:
        return None

    async def list_keys(self, prefix):
        return []

    async def delete(self, key) -> None:
        return None


# ── Fixtures: stub the lazy factories so build_container works ─────────────


@pytest.fixture
def stubbed_factories(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Replace each lazy factory in tiri.container with a stub-returning one.

    Returns a dict capturing every instantiation so tests can assert
    instance identity / counts.
    """
    llm_instances: list[_StubLLM] = []
    catalog_instances: list[_StubCatalog] = []
    metadata_instances: list[_StubMetadata] = []
    query_instances: list[_StubQuery] = []
    vector_instances: list[_StubVector] = []
    store_instances: list[_StubStore] = []

    def fake_llm(bc: ProviderBackendConfig) -> _StubLLM:
        inst = _StubLLM(label=bc.name)
        llm_instances.append(inst)
        return inst

    def fake_catalog(cfg: Config) -> _StubCatalog:
        inst = _StubCatalog()
        catalog_instances.append(inst)
        return inst

    def fake_query(cfg: Config) -> _StubQuery:
        inst = _StubQuery()
        query_instances.append(inst)
        return inst

    def fake_vector(cfg: Config) -> _StubVector:
        inst = _StubVector()
        vector_instances.append(inst)
        return inst

    def fake_store(cfg: Config, query: _StubQuery) -> _StubStore:
        inst = _StubStore()
        store_instances.append(inst)
        return inst

    def fake_metadata_stack(
        cfg: Config, query: _StubQuery
    ) -> list[_StubMetadata]:
        if not cfg.metadata_provider_configs:
            inst = _StubMetadata("default")
            metadata_instances.append(inst)
            return [inst]
        out: list[_StubMetadata] = []
        for spec in cfg.metadata_provider_configs:
            inst = _StubMetadata(spec.get("name", spec.get("type", "stub")))
            metadata_instances.append(inst)
            out.append(inst)
        return out

    monkeypatch.setattr(container_module, "_instantiate_llm_backend", fake_llm)
    monkeypatch.setattr(container_module, "_build_catalog", fake_catalog)
    monkeypatch.setattr(container_module, "_build_query", fake_query)
    monkeypatch.setattr(container_module, "_build_vector", fake_vector)
    monkeypatch.setattr(container_module, "_build_store", fake_store)
    monkeypatch.setattr(
        container_module, "_build_metadata_providers", fake_metadata_stack
    )

    return {
        "llm": llm_instances,
        "catalog": catalog_instances,
        "metadata": metadata_instances,
        "query": query_instances,
        "vector": vector_instances,
        "store": store_instances,
    }


def _single_backend_config() -> Config:
    backend = ProviderBackendConfig(name="x", type="openai", api_key="sk-x")
    routing = RoutingConfig(
        intent="x::m",
        planning="x::m",
        sql="x::m",
        synthesis="x::m",
        clarify="x::m",
        viz_summary="x::m",
        embed="x::e",
    )
    return Config(
        llm_backends={"x": backend},
        llm_routing=routing,
        catalog_provider="static",
        query_provider="duckdb",
        vector_provider="chroma",
        store_provider="sqlite",
    )


def _two_backend_config() -> Config:
    db = ProviderBackendConfig(name="db", type="databricks", host="h", token="t")
    oai = ProviderBackendConfig(name="oai", type="openai", api_key="sk-oai")
    routing = RoutingConfig(
        intent="db::small",
        planning="db::big",
        sql="oai::gpt-4o",
        synthesis="db::big",
        clarify="db::small",
        viz_summary="db::small",
        embed="oai::text-embedding-3-small",
    )
    return Config(
        llm_backends={"db": db, "oai": oai},
        llm_routing=routing,
        catalog_provider="static",
        query_provider="duckdb",
        vector_provider="chroma",
        store_provider="sqlite",
        db_warehouse_id="wh-1",
        db_vector_endpoint="ep-1",
    )


# ─────────────────────────────────────────────────────────────────────────────
# RouterLLMProvider direct tests
# ─────────────────────────────────────────────────────────────────────────────


def test_router_requires_at_least_one_route() -> None:
    with pytest.raises(ConfigurationError, match="at least one route"):
        RouterLLMProvider(routes={})


def test_router_lookup_missing_task_raises() -> None:
    stub = _StubLLM()
    router = RouterLLMProvider(
        routes={"sql": ModelRoute(task="sql", provider=stub, model_name="m")}
    )

    async def go() -> None:
        await router.complete([LLMMessage(role="user", content="hi")], task="intent")

    import asyncio

    with pytest.raises(ConfigurationError, match="intent"):
        asyncio.run(go())


def test_router_dispatches_complete_to_correct_backend() -> None:
    intent_stub = _StubLLM(label="i")
    sql_stub = _StubLLM(label="s")
    routes = {
        "intent": ModelRoute(task="intent", provider=intent_stub, model_name="m1"),
        "sql": ModelRoute(task="sql", provider=sql_stub, model_name="m2"),
        "embed": ModelRoute(task="embed", provider=intent_stub, model_name="e"),
    }
    router = RouterLLMProvider(routes=routes)

    import asyncio

    async def go() -> tuple[str, str]:
        a = await router.complete(
            [LLMMessage(role="user", content="q1")], task="intent"
        )
        b = await router.complete(
            [LLMMessage(role="user", content="q2")], task="sql"
        )
        return a.content, b.content

    a, b = asyncio.run(go())
    assert a == "i:intent"
    assert b == "s:sql"
    assert intent_stub.complete_calls == [("intent", "q1", "m1")]
    assert sql_stub.complete_calls == [("sql", "q2", "m2")]


def test_router_embed_uses_embed_route() -> None:
    embed_stub = _StubLLM(label="e")
    other_stub = _StubLLM(label="o")
    routes = {
        "sql": ModelRoute(task="sql", provider=other_stub, model_name="m1"),
        "embed": ModelRoute(task="embed", provider=embed_stub, model_name="e1"),
    }
    router = RouterLLMProvider(routes=routes)

    import asyncio

    vecs = asyncio.run(router.embed(["a", "b"]))
    assert len(vecs) == 2
    assert embed_stub.embed_calls == [["a", "b"]]
    assert other_stub.embed_calls == []


def test_router_stream_routes_to_correct_backend() -> None:
    sql_stub = _StubLLM(label="s")
    other_stub = _StubLLM(label="o")
    routes = {
        "intent": ModelRoute(task="intent", provider=other_stub, model_name="m"),
        "sql": ModelRoute(task="sql", provider=sql_stub, model_name="m"),
        "embed": ModelRoute(task="embed", provider=sql_stub, model_name="e"),
    }
    router = RouterLLMProvider(routes=routes)

    import asyncio

    async def go() -> list[str]:
        out: list[str] = []
        async for c in router.stream([LLMMessage(role="user", content="hi")], task="sql"):
            out.append(c)
        return out

    out = asyncio.run(go())
    assert out == ["s:sql"]
    assert sql_stub.stream_calls == [("sql", "m")]
    assert other_stub.stream_calls == []


def test_router_is_an_llmprovider() -> None:
    stub = _StubLLM()
    router = RouterLLMProvider(
        routes={"embed": ModelRoute(task="embed", provider=stub, model_name="m")}
    )
    assert isinstance(router, LLMProvider)


# ── EXT-3: per-task model routing ────────────────────────────────────────


def test_router_forwards_route_model_name_to_provider() -> None:
    """EXT-3: when complete() is called without an explicit `model`, the
    route's model_name flows through to the underlying provider."""
    stub = _StubLLM(label="x")
    routes = {
        "intent": ModelRoute(task="intent", provider=stub, model_name="small"),
        "sql": ModelRoute(task="sql", provider=stub, model_name="big"),
        "embed": ModelRoute(task="embed", provider=stub, model_name="e"),
    }
    router = RouterLLMProvider(routes=routes)

    import asyncio

    async def go() -> None:
        await router.complete(
            [LLMMessage(role="user", content="hi")], task="intent"
        )
        await router.complete(
            [LLMMessage(role="user", content="hi")], task="sql"
        )

    asyncio.run(go())
    # Same provider instance received two different model names — exactly
    # what EXT-3 needs to support multi-model on a single backend.
    models = [call[2] for call in stub.complete_calls]
    assert models == ["small", "big"]


def test_router_explicit_model_overrides_route_model_name() -> None:
    """If the caller passes `model=`, it takes precedence over the route's."""
    stub = _StubLLM(label="x")
    routes = {
        "sql": ModelRoute(task="sql", provider=stub, model_name="default"),
        "embed": ModelRoute(task="embed", provider=stub, model_name="e"),
    }
    router = RouterLLMProvider(routes=routes)

    import asyncio

    asyncio.run(
        router.complete(
            [LLMMessage(role="user", content="hi")],
            task="sql",
            model="override",
        )
    )
    assert stub.complete_calls[-1][2] == "override"


def test_router_stream_forwards_model() -> None:
    stub = _StubLLM(label="x")
    routes = {
        "sql": ModelRoute(task="sql", provider=stub, model_name="big"),
        "embed": ModelRoute(task="embed", provider=stub, model_name="e"),
    }
    router = RouterLLMProvider(routes=routes)

    import asyncio

    async def go() -> None:
        async for _ in router.stream(
            [LLMMessage(role="user", content="hi")], task="sql"
        ):
            pass

    asyncio.run(go())
    assert stub.stream_calls[-1] == ("sql", "big")


def test_router_routes_property_is_a_copy() -> None:
    stub = _StubLLM()
    routes = {"embed": ModelRoute(task="embed", provider=stub, model_name="m")}
    router = RouterLLMProvider(routes=routes)
    snapshot = router.routes
    snapshot["sql"] = ModelRoute(task="sql", provider=stub, model_name="x")
    assert "sql" not in router.routes  # external mutation must not leak


# ─────────────────────────────────────────────────────────────────────────────
# build_container tests (configuration.md cases 6, 7, 10)
# ─────────────────────────────────────────────────────────────────────────────


def test_build_container_returns_router_for_single_backend(
    stubbed_factories: dict[str, list],
) -> None:
    """Case 7: build_container() with single-backend config MUST return
    RouterLLMProvider as the llm entry."""
    cfg = _single_backend_config()
    container = build_container(cfg)
    assert isinstance(container["llm"], RouterLLMProvider)
    assert len(stubbed_factories["llm"]) == 1
    # All seven routes resolve to the single provider instance.
    only_provider = stubbed_factories["llm"][0]
    for route in container["llm"].routes.values():
        assert route.provider is only_provider


def test_build_container_instantiates_two_separate_objects(
    stubbed_factories: dict[str, list],
) -> None:
    """Case 6: build_container() with two-backend config MUST instantiate two
    separate LLMProvider objects."""
    cfg = _two_backend_config()
    container = build_container(cfg)
    assert isinstance(container["llm"], RouterLLMProvider)
    assert len(stubbed_factories["llm"]) == 2
    instance_ids = {id(inst) for inst in stubbed_factories["llm"]}
    assert len(instance_ids) == 2  # truly distinct objects

    # Each route points at one of the two backend instances.
    by_label = {inst.label: inst for inst in stubbed_factories["llm"]}
    routes = container["llm"].routes
    assert routes["intent"].provider is by_label["db"]
    assert routes["sql"].provider is by_label["oai"]
    assert routes["embed"].provider is by_label["oai"]


def test_build_container_all_local_does_no_network(
    stubbed_factories: dict[str, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 10: build_container() with all-local config MUST complete with no
    network calls. We block socket connections during the call to enforce this."""
    cfg = _single_backend_config()

    def no_network(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Network access during build_container()")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)

    container = build_container(cfg)
    assert isinstance(container["llm"], RouterLLMProvider)
    assert container["catalog"] is stubbed_factories["catalog"][0]
    assert container["query"] is stubbed_factories["query"][0]
    assert container["vector"] is stubbed_factories["vector"][0]
    assert container["store"] is stubbed_factories["store"][0]


def test_build_container_returns_all_seven_entries(
    stubbed_factories: dict[str, list],
) -> None:
    cfg = _single_backend_config()
    container = build_container(cfg)
    assert set(container) == {
        "llm",
        "catalog",
        "metadata_providers",
        "query",
        "vector",
        "store",
        "mcp_providers",
    }
    assert isinstance(container["metadata_providers"], list)
    # EXT-5: registry exists, default empty. Config-driven population is L1
    # in fixme.md — until then deployments with MCP wire it externally.
    assert container["mcp_providers"] == {}


def test_build_container_metadata_stack_preserves_order(
    stubbed_factories: dict[str, list],
) -> None:
    cfg = _single_backend_config()
    cfg.metadata_provider_configs = [
        {"name": "uc", "type": "uc_annotations"},
        {"name": "yaml1", "type": "yaml", "path": "./a.yaml"},
        {"name": "yaml2", "type": "yaml", "path": "./b.yaml"},
    ]
    container = build_container(cfg)
    names = [p.name for p in container["metadata_providers"]]
    assert names == ["uc", "yaml1", "yaml2"]


def test_build_container_metadata_empty_stack_uses_default(
    stubbed_factories: dict[str, list],
) -> None:
    cfg = _single_backend_config()
    cfg.metadata_provider_configs = []
    container = build_container(cfg)
    # Stubbed _build_metadata_providers returns a single default entry.
    assert len(container["metadata_providers"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Helpers and parse_route
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_route_splits_correctly() -> None:
    stub = _StubLLM()
    registry = {"a": stub, "b": stub}
    assert container_module._parse_route("a::model-1", registry) == ("a", "model-1")
    assert container_module._parse_route("b::something::weird", registry) == (
        "b",
        "something::weird",
    )


def test_parse_route_unknown_backend_raises() -> None:
    stub = _StubLLM()
    registry = {"a": stub}
    with pytest.raises(ConfigurationError, match="undefined backend"):
        container_module._parse_route("ghost::m", registry)


def test_parse_route_malformed_raises() -> None:
    with pytest.raises(ConfigurationError, match="malformed"):
        container_module._parse_route("no-double-colon-here", {})


# ─────────────────────────────────────────────────────────────────────────────
# Concrete factories raise clear errors when modules don't exist (Step 4 state)
# ─────────────────────────────────────────────────────────────────────────────


def test_instantiating_databricks_backend_returns_llmprovider() -> None:
    """Lazy import resolves now that Step 5 has landed."""
    bc = ProviderBackendConfig(
        name="db", type="databricks", host="https://x", token="t"
    )
    instance = container_module._instantiate_llm_backend(bc)
    assert isinstance(instance, LLMProvider)


def test_instantiating_unknown_type_raises_configuration_error() -> None:
    bc = ProviderBackendConfig(name="x", type="not-a-real-type")
    with pytest.raises(ConfigurationError, match="Unknown LLM backend type"):
        container_module._instantiate_llm_backend(bc)
