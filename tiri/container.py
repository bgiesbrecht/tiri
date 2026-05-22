"""Tiri container — wires Config into provider instances.

`build_container()` is called once at startup. It returns a dict containing
one instance of each provider type, plus the metadata provider stack in
configured order. Agents and the room engine receive these instances at
construction time.

`RouterLLMProvider` lives here. It is always returned as the `llm` entry,
even for single-backend configurations. Multi-backend / per-task routing
(EXT-3) extends this same class — no rewrite.

See docs/configuration.md (the `container.py` section) for the spec.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

from tiri.config import Config, ConfigurationError, ProviderBackendConfig
from tiri.data_models import LLMMessage, LLMResponse
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    MetadataProvider,
    QueryProvider,
    StoreProvider,
    VectorProvider,
)


# ────────────────────────────────────────────────────────────────────────────
# ModelRoute + RouterLLMProvider
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ModelRoute:
    """One task→backend assignment.

    `provider` is an instantiated LLMProvider (one per declared backend name).
    `model_name` is the name from the routing entry — recorded here for
    provenance and used by per-call routing when EXT-3 lands. MVP backends
    do not need it per call because each backend has one model.
    """

    task: str
    provider: LLMProvider
    model_name: str
    temperature: float = 0.0
    max_tokens: int = 2048


class RouterLLMProvider(LLMProvider):
    """Meta-provider that routes each call to the backend configured for the
    task. Always returned by `build_container()` as the `llm` entry — even when
    only one backend is declared. Agents never know whether routing is active.

    For MVP single-backend configs, every route points at the same provider
    instance, so this is a thin pass-through. EXT-3 (multi-model routing) does
    not change the class structure — it changes which provider each route
    points at.
    """

    def __init__(self, routes: dict[str, ModelRoute]) -> None:
        if not routes:
            raise ConfigurationError(
                "RouterLLMProvider requires at least one route"
            )
        self._routes: dict[str, ModelRoute] = dict(routes)

    @property
    def routes(self) -> dict[str, ModelRoute]:
        return dict(self._routes)

    def _lookup(self, task: str) -> ModelRoute:
        if task not in self._routes:
            raise ConfigurationError(
                f"No route configured for task {task!r}; "
                f"available: {sorted(self._routes)}"
            )
        return self._routes[task]

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        route = self._lookup(task)
        # When the caller doesn't pass an explicit `model`, fall through to
        # the route's model_name so that two tasks on the same backend can
        # use different models. The concrete provider's constructor default
        # only kicks in when both `model` arg and `route.model_name` are
        # empty — that's the single-model-per-provider case.
        effective_model = model or route.model_name or None
        return await route.provider.complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            task=task,
            model=effective_model,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        route = self._lookup(task)
        effective_model = model or route.model_name or None
        async for chunk in route.provider.stream(
            messages,
            temperature=temperature,
            task=task,
            model=effective_model,
        ):
            yield chunk

    async def embed(self, texts: list[str]) -> list[list[float]]:
        route = self._lookup("embed")
        return await route.provider.embed(texts)


# ────────────────────────────────────────────────────────────────────────────
# build_container()
# ────────────────────────────────────────────────────────────────────────────


def build_container(cfg: Config) -> dict[str, Any]:
    """Instantiate all providers from Config and return them as a dict.

    Returns:
        {
          "llm":                RouterLLMProvider (always),
          "catalog":            CatalogProvider,
          "metadata_providers": list[MetadataProvider] in stack order,
          "query":              QueryProvider,
          "vector":             VectorProvider,
          "store":              StoreProvider,
          "mcp_providers":      dict[url, MCPProvider]  (EXT-5; empty by default),
        }

    Build order matters: `query` is built before `store` and the metadata
    stack, because both can use a QueryProvider for SQL-backed operations
    (DatabricksStoreProvider, UCAnnotationsMetadataProvider sample-value
    collection).

    EXT-5: `mcp_providers` is wired empty here for now — config-driven
    HttpMCPProvider instantiation (URLs + auth tokens from `tiri.toml`) is
    a follow-up. The engine accepts an empty dict and skips MCP resolution
    entirely when rooms declare no `mcp_servers`, so deployments without
    MCP wiring are unaffected.
    """
    query = _build_query(cfg)
    return {
        "llm": _build_llm_registry(cfg),
        "catalog": _build_catalog(cfg),
        "metadata_providers": _build_metadata_providers(cfg, query),
        "query": query,
        "vector": _build_vector(cfg),
        "store": _build_store(cfg, query),
        "mcp_providers": {},
    }


# ────────────────────────────────────────────────────────────────────────────
# LLM registry
# ────────────────────────────────────────────────────────────────────────────


def _routing_pairs(cfg: Config) -> Iterator[tuple[str, str]]:
    yield "intent", cfg.llm_routing.intent
    yield "planning", cfg.llm_routing.planning
    yield "sql", cfg.llm_routing.sql
    yield "synthesis", cfg.llm_routing.synthesis
    yield "clarify", cfg.llm_routing.clarify
    yield "viz_summary", cfg.llm_routing.viz_summary
    yield "embed", cfg.llm_routing.embed


def _parse_route(
    route_str: str, registry: dict[str, LLMProvider]
) -> tuple[str, str]:
    """Parse `provider_name::model_name`. Raises if backend is unknown."""
    if "::" not in route_str:
        raise ConfigurationError(
            f"Routing entry {route_str!r} is malformed; "
            "expected 'provider_name::model_name'"
        )
    backend_name, model_name = route_str.split("::", 1)
    if backend_name not in registry:
        raise ConfigurationError(
            f"Routing entry {route_str!r} references undefined backend "
            f"{backend_name!r}; defined backends: {sorted(registry)}"
        )
    return backend_name, model_name


def _build_llm_registry(cfg: Config) -> RouterLLMProvider:
    instances: dict[str, LLMProvider] = {}
    for name, bc in cfg.llm_backends.items():
        instances[name] = _instantiate_llm_backend(bc)

    routes: dict[str, ModelRoute] = {}
    for task, route_str in _routing_pairs(cfg):
        backend_name, model_name = _parse_route(route_str, instances)
        routes[task] = ModelRoute(
            task=task,
            provider=instances[backend_name],
            model_name=model_name,
        )
    return RouterLLMProvider(routes=routes)


def _instantiate_llm_backend(bc: ProviderBackendConfig) -> LLMProvider:
    """Instantiate one LLM backend. Concrete provider modules are imported
    lazily — they don't exist until Steps 5 and 6.
    """
    if bc.type == "databricks":
        from tiri.providers.databricks.llm import DatabricksLLMProvider

        return DatabricksLLMProvider(host=bc.host, token=bc.token)
    if bc.type == "openai":
        from tiri.providers.local.llm_openai import OpenAILLMProvider

        return OpenAILLMProvider(api_key=bc.api_key)
    if bc.type == "anthropic":
        from tiri.providers.local.llm_anthropic import AnthropicLLMProvider

        return AnthropicLLMProvider(api_key=bc.api_key)
    if bc.type == "ollama":
        from tiri.providers.local.llm_ollama import OllamaLLMProvider

        return OllamaLLMProvider(base_url=bc.base_url)
    raise ConfigurationError(
        f"Unknown LLM backend type {bc.type!r} for backend {bc.name!r}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Non-LLM providers
# ────────────────────────────────────────────────────────────────────────────


def _build_catalog(cfg: Config) -> CatalogProvider:
    if cfg.catalog_provider == "databricks":
        from tiri.providers.databricks.catalog import DatabricksCatalogProvider

        return DatabricksCatalogProvider(
            host=cfg.databricks_host, token=cfg.databricks_token
        )
    if cfg.catalog_provider == "static":
        from tiri.providers.local.catalog_static import StaticCatalogProvider

        return StaticCatalogProvider(schema_file=cfg.static_schema_file)
    raise ConfigurationError(
        f"Unknown catalog_provider {cfg.catalog_provider!r}"
    )


def _build_query(cfg: Config) -> QueryProvider:
    if cfg.query_provider == "databricks":
        from tiri.providers.databricks.query import DatabricksQueryProvider

        return DatabricksQueryProvider(
            host=cfg.databricks_host,
            token=cfg.databricks_token,
            warehouse_id=cfg.db_warehouse_id,
        )
    if cfg.query_provider == "duckdb":
        from tiri.providers.local.query_duckdb import DuckDBQueryProvider

        return DuckDBQueryProvider(data_dir=cfg.duckdb_data_dir)
    raise ConfigurationError(f"Unknown query_provider {cfg.query_provider!r}")


def _build_vector(cfg: Config) -> VectorProvider:
    if cfg.vector_provider == "databricks":
        from tiri.providers.databricks.vector import DatabricksVectorProvider

        return DatabricksVectorProvider(
            host=cfg.databricks_host,
            token=cfg.databricks_token,
            index=cfg.db_vector_index,
            endpoint=cfg.db_vector_endpoint,
        )
    if cfg.vector_provider == "chroma":
        from tiri.providers.local.vector_chroma import ChromaVectorProvider

        return ChromaVectorProvider(path=cfg.chroma_path)
    raise ConfigurationError(
        f"Unknown vector_provider {cfg.vector_provider!r}"
    )


def _build_store(cfg: Config, query: QueryProvider) -> StoreProvider:
    if cfg.store_provider == "databricks":
        from tiri.providers.databricks.store import DatabricksStoreProvider

        return DatabricksStoreProvider(table=cfg.db_store_table, query=query)
    if cfg.store_provider == "sqlite":
        from tiri.providers.local.store_sqlite import SQLiteStoreProvider

        return SQLiteStoreProvider(path=cfg.sqlite_path)
    raise ConfigurationError(f"Unknown store_provider {cfg.store_provider!r}")


# ────────────────────────────────────────────────────────────────────────────
# Metadata provider stack
# ────────────────────────────────────────────────────────────────────────────


def _build_metadata_providers(
    cfg: Config, query: QueryProvider
) -> list[MetadataProvider]:
    """Instantiate the metadata provider stack in configured order.

    Empty `cfg.metadata_provider_configs` falls back to a single
    `UCAnnotationsMetadataProvider` — equivalent to what Genie does.

    `RoomConfigMetadataProvider` is NOT included here; `MetadataFetcher`
    appends it as the always-last entry.

    `query` is forwarded to providers that need SQL access for enrichment
    (currently only `UCAnnotationsMetadataProvider` for sample-value
    collection).
    """
    if not cfg.metadata_provider_configs:
        from tiri.providers.databricks.metadata import (
            UCAnnotationsMetadataProvider,
        )

        return [
            UCAnnotationsMetadataProvider(
                host=cfg.databricks_host,
                token=cfg.databricks_token,
                query=query,
            )
        ]

    providers: list[MetadataProvider] = []
    for spec in cfg.metadata_provider_configs:
        providers.append(_instantiate_metadata_provider(spec, cfg, query))
    return providers


def _instantiate_metadata_provider(
    spec: dict, cfg: Config, query: QueryProvider
) -> MetadataProvider:
    type_name = spec.get("type")
    if type_name == "uc_annotations":
        from tiri.providers.databricks.metadata import (
            UCAnnotationsMetadataProvider,
        )

        return UCAnnotationsMetadataProvider(
            host=cfg.databricks_host,
            token=cfg.databricks_token,
            query=query,
            sample_values_enabled=spec.get("sample_values_enabled", True),
            sample_values_max_distinct=spec.get(
                "sample_values_max_distinct", 50
            ),
        )
    if type_name == "yaml":
        from tiri.providers.local.metadata_yaml import YAMLMetadataProvider

        return YAMLMetadataProvider(
            name=spec.get("name", "yaml"), path=spec["path"]
        )
    if type_name == "delta_table":
        from tiri.providers.databricks.metadata import (
            DeltaTableMetadataProvider,
        )

        return DeltaTableMetadataProvider(
            name=spec.get("name", "delta_table"),
            table=spec["table"],
            query=query,
        )
    if type_name == "dbt":
        from tiri.providers.local.metadata_dbt import DbtMetadataProvider

        return DbtMetadataProvider(
            name=spec.get("name", "dbt"),
            manifest_path=spec["manifest_path"],
            catalog_path=spec.get("catalog_path"),
        )
    if type_name == "static":
        from tiri.providers.local.metadata_static import (
            StaticMetadataProvider,
        )

        return StaticMetadataProvider(
            name=spec.get("name", "static"), data=spec.get("data", {})
        )
    raise ConfigurationError(
        f"Unknown metadata provider type {type_name!r}; spec: {spec!r}"
    )
