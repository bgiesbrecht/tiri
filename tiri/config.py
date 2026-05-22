"""Tiri configuration loader.

Reads from `tiri.toml` if present, else from environment variables. All
components import from this module — never from `os.environ` or `tomllib`
directly.

See docs/configuration.md for the specification.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_log = logging.getLogger("tiri.config")
_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_ANTHROPIC_TYPE = "anthropic"


class ConfigurationError(Exception):
    """Raised when configuration is malformed or missing a required value."""


# ────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ProviderBackendConfig:
    """One named LLM backend in the registry."""

    name: str            # the key from [llm.providers.NAME]
    type: str            # "databricks" | "openai" | "anthropic" | "ollama"
    host: str = ""       # databricks only
    token: str = ""      # databricks
    api_key: str = ""    # openai / anthropic
    base_url: str = ""   # ollama


@dataclass
class RoutingConfig:
    """Task-to-backend::model assignments."""

    intent: str
    planning: str
    sql: str
    synthesis: str
    clarify: str
    viz_summary: str
    embed: str


@dataclass
class Config:
    """Tiri runtime configuration.

    Populated by `Config.load()` from `tiri.toml` (if present) and environment
    variables. After construction, the instance is immutable in practice — the
    process reads it once at startup and never reloads.
    """

    llm_backends: dict[str, ProviderBackendConfig]
    llm_routing: RoutingConfig

    metadata_provider_configs: list[dict] = field(default_factory=list)

    # Non-LLM provider selection
    catalog_provider: str = "databricks"
    query_provider: str = "databricks"
    vector_provider: str = "databricks"
    store_provider: str = "databricks"

    # Databricks workspace credentials — used by the non-LLM Databricks
    # providers (catalog, query, vector, store, uc_annotations). Populated from
    # DATABRICKS_HOST and DATABRICKS_TOKEN env vars. Not present in tiri.toml
    # — these are secrets.
    databricks_host: str = ""
    databricks_token: str = ""

    # Non-LLM provider settings
    db_warehouse_id: str = ""
    db_vector_index: str = "main.tiri.example_index"
    db_vector_endpoint: str = ""
    db_store_table: str = "main.tiri.kv_store"
    static_schema_file: str = "schemas.json"
    duckdb_data_dir: str = "./data"
    chroma_path: str = ":memory:"
    sqlite_path: str = "./tiri_store.db"

    # Engine tuning
    intent_threshold: float = 0.7
    sql_max_retries: int = 3
    query_row_limit: int = 10_000
    example_top_k: int = 5
    history_window: int = 10
    plan_max_steps: int = 5
    metadata_cache_ttl: int = 0

    # API
    auth_disabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
    cors_origins: str = "*"

    @classmethod
    def load(cls, toml_path: str = "tiri.toml") -> Config:
        """Load configuration. Apply `${VAR}` substitution, validate, return.

        Reads `tiri.toml` if it exists, otherwise synthesizes a single-backend
        registry from environment variables. Engine-tuning and API settings are
        always taken from env vars (they have no `tiri.toml` representation).

        Raises `ConfigurationError` on any missing required value, unresolved
        `${VAR}` reference, undefined routing backend, or Anthropic-as-embed.
        """
        toml_data = _read_toml(toml_path)
        if toml_data is not None:
            cfg = cls._from_toml(toml_data)
        else:
            cfg = cls._from_env_only()
        _apply_engine_tuning_env(cfg)
        _apply_api_env(cfg)
        cfg._validate()
        if cfg.auth_disabled:
            _log.warning(
                "AUTH_DISABLED=true — Bearer token validation skipped. "
                "Never use in production."
            )
        return cfg

    # ── Construction paths ────────────────────────────────────────────────

    @classmethod
    def _from_toml(cls, data: dict) -> Config:
        llm = data.get("llm", {})

        # [llm.providers.NAME]
        backends_block = llm.get("providers", {})
        if not backends_block:
            raise ConfigurationError(
                "tiri.toml has no [llm.providers.NAME] blocks — at least one "
                "LLM backend must be declared"
            )
        backends: dict[str, ProviderBackendConfig] = {}
        for name, cfg_dict in backends_block.items():
            backend_type = cfg_dict.get("type", "")
            host = cfg_dict.get("host", "")
            token = cfg_dict.get("token", "")
            api_key = cfg_dict.get("api_key", "")
            base_url = cfg_dict.get("base_url", "")
            # When credentials are missing from TOML, fall through to the
            # provider-specific env var (DATABRICKS_HOST/TOKEN,
            # ANTHROPIC_API_KEY, OPENAI_API_KEY, OLLAMA_BASE_URL). Keeps
            # secrets out of TOML — tiri.toml describes wiring; env carries
            # credentials.
            if backend_type == "databricks":
                host = host or os.environ.get("DATABRICKS_HOST", "")
                token = token or os.environ.get("DATABRICKS_TOKEN", "")
            elif backend_type in ("anthropic", "openai"):
                api_key = api_key or _env_for_provider_api_key(backend_type)
            elif backend_type == "ollama":
                base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "")
            backends[name] = ProviderBackendConfig(
                name=name,
                type=backend_type,
                host=host,
                token=token,
                api_key=api_key,
                base_url=base_url,
            )

        # [llm.routing]
        routing_block = llm.get("routing", {})
        required_tasks = (
            "intent",
            "planning",
            "sql",
            "synthesis",
            "clarify",
            "viz_summary",
            "embed",
        )
        for task in required_tasks:
            if task not in routing_block:
                raise ConfigurationError(
                    f"[llm.routing] missing required task assignment: {task!r}"
                )
        routing = RoutingConfig(**{t: routing_block[t] for t in required_tasks})

        # [providers.*]
        providers = data.get("providers", {})
        catalog_section = providers.get("catalog", {})
        query_section = providers.get("query", {})
        vector_section = providers.get("vector", {})
        store_section = providers.get("store", {})

        # [[metadata.providers.stack]]
        meta_block = data.get("metadata", {}).get("providers", {})
        meta_stack = list(meta_block.get("stack", []))

        return cls(
            llm_backends=backends,
            llm_routing=routing,
            metadata_provider_configs=meta_stack,
            catalog_provider=catalog_section.get("type", "databricks"),
            query_provider=query_section.get("type", "databricks"),
            vector_provider=vector_section.get("type", "databricks"),
            store_provider=store_section.get("type", "databricks"),
            databricks_host=os.environ.get("DATABRICKS_HOST", ""),
            databricks_token=os.environ.get("DATABRICKS_TOKEN", ""),
            db_warehouse_id=_env_or(
                "DB_WAREHOUSE_ID", query_section.get("warehouse_id", "")
            ),
            db_vector_index=_env_or(
                "DB_VECTOR_INDEX",
                vector_section.get("index", "main.tiri.example_index"),
            ),
            db_vector_endpoint=_env_or(
                "DB_VECTOR_ENDPOINT", vector_section.get("endpoint", "")
            ),
            db_store_table=_env_or(
                "DB_STORE_TABLE",
                store_section.get("table", "main.tiri.kv_store"),
            ),
            static_schema_file=_env_or("STATIC_SCHEMA_FILE", "schemas.json"),
            duckdb_data_dir=_env_or("DUCKDB_DATA_DIR", "./data"),
            chroma_path=_env_or("CHROMA_PATH", ":memory:"),
            sqlite_path=_env_or("SQLITE_PATH", "./tiri_store.db"),
        )

    @classmethod
    def _from_env_only(cls) -> Config:
        """Simple-mode: synthesize a single-backend registry from env vars.

        All LLM tasks route to one backend chosen by `LLM_PROVIDER`. If that
        backend is Anthropic, the `embed` route falls back to an additional
        OpenAI backend (Anthropic has no embedding API).
        """
        provider_type = os.environ.get("LLM_PROVIDER", "databricks")
        backend_name = provider_type
        backend = ProviderBackendConfig(
            name=backend_name,
            type=provider_type,
            host=os.environ.get("DATABRICKS_HOST", ""),
            token=os.environ.get("DATABRICKS_TOKEN", ""),
            api_key=_env_for_provider_api_key(provider_type),
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        )
        completion_model = _default_completion_model(provider_type)
        embed_model = _default_embed_model(provider_type)
        embed_backend_name = backend_name

        if provider_type == _ANTHROPIC_TYPE:
            # Anthropic cannot embed — auto-add an OpenAI embed backend so the
            # validation contract holds and the user gets a usable embed route.
            embed_backend_name = "openai"
            backends: dict[str, ProviderBackendConfig] = {
                backend_name: backend,
                embed_backend_name: ProviderBackendConfig(
                    name=embed_backend_name,
                    type="openai",
                    api_key=os.environ.get("OPENAI_API_KEY", ""),
                ),
            }
            embed_model = os.environ.get(
                "OPENAI_EMBED_MODEL", "text-embedding-3-small"
            )
        else:
            backends = {backend_name: backend}

        routing = RoutingConfig(
            intent=f"{backend_name}::{completion_model}",
            planning=f"{backend_name}::{completion_model}",
            sql=f"{backend_name}::{completion_model}",
            synthesis=f"{backend_name}::{completion_model}",
            clarify=f"{backend_name}::{completion_model}",
            viz_summary=f"{backend_name}::{completion_model}",
            embed=f"{embed_backend_name}::{embed_model}",
        )

        return cls(
            llm_backends=backends,
            llm_routing=routing,
            catalog_provider=os.environ.get("CATALOG_PROVIDER", "databricks"),
            query_provider=os.environ.get("QUERY_PROVIDER", "databricks"),
            vector_provider=os.environ.get("VECTOR_PROVIDER", "databricks"),
            store_provider=os.environ.get("STORE_PROVIDER", "databricks"),
            databricks_host=os.environ.get("DATABRICKS_HOST", ""),
            databricks_token=os.environ.get("DATABRICKS_TOKEN", ""),
            db_warehouse_id=os.environ.get("DB_WAREHOUSE_ID", ""),
            db_vector_index=os.environ.get(
                "DB_VECTOR_INDEX", "main.tiri.example_index"
            ),
            db_vector_endpoint=os.environ.get("DB_VECTOR_ENDPOINT", ""),
            db_store_table=os.environ.get(
                "DB_STORE_TABLE", "main.tiri.kv_store"
            ),
            static_schema_file=os.environ.get(
                "STATIC_SCHEMA_FILE", "schemas.json"
            ),
            duckdb_data_dir=os.environ.get("DUCKDB_DATA_DIR", "./data"),
            chroma_path=os.environ.get("CHROMA_PATH", ":memory:"),
            sqlite_path=os.environ.get("SQLITE_PATH", "./tiri_store.db"),
        )

    # ── Validation ────────────────────────────────────────────────────────

    def _routing_pairs(self) -> list[tuple[str, str]]:
        return [
            ("intent", self.llm_routing.intent),
            ("planning", self.llm_routing.planning),
            ("sql", self.llm_routing.sql),
            ("synthesis", self.llm_routing.synthesis),
            ("clarify", self.llm_routing.clarify),
            ("viz_summary", self.llm_routing.viz_summary),
            ("embed", self.llm_routing.embed),
        ]

    def _validate(self) -> None:
        # Every routing reference must resolve to a declared backend.
        for task, route in self._routing_pairs():
            backend_name, _ = _split_route(route)
            if backend_name not in self.llm_backends:
                raise ConfigurationError(
                    f"llm_routing.{task} references undefined backend "
                    f"{backend_name!r}; declared backends: "
                    f"{sorted(self.llm_backends)}"
                )

        # embed route MUST NOT point at an Anthropic backend.
        embed_backend, _ = _split_route(self.llm_routing.embed)
        if self.llm_backends[embed_backend].type == _ANTHROPIC_TYPE:
            raise ConfigurationError(
                f"llm_routing.embed routes to Anthropic backend "
                f"{embed_backend!r}, but Anthropic has no embedding API. "
                "Assign embed to an openai / databricks / ollama backend."
            )

        # Required settings when type=databricks.
        if self.query_provider == "databricks" and not self.db_warehouse_id:
            raise ConfigurationError(
                "query_provider=databricks requires db_warehouse_id "
                "(set DB_WAREHOUSE_ID or [providers.query].warehouse_id)"
            )
        if self.vector_provider == "databricks":
            if not self.db_vector_index:
                raise ConfigurationError(
                    "vector_provider=databricks requires db_vector_index"
                )
            if not self.db_vector_endpoint:
                raise ConfigurationError(
                    "vector_provider=databricks requires db_vector_endpoint "
                    "(set DB_VECTOR_ENDPOINT or [providers.vector].endpoint)"
                )


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _env_or(env_var: str, fallback: str) -> str:
    """Env var if non-empty, else fallback. Env wins over `tiri.toml`."""
    value = os.environ.get(env_var, "")
    return value if value else fallback


def _env_for_provider_api_key(provider_type: str) -> str:
    if provider_type == "openai":
        return os.environ.get("OPENAI_API_KEY", "")
    if provider_type == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY", "")
    return ""


def _default_completion_model(provider_type: str) -> str:
    return {
        "databricks": os.environ.get(
            "DB_LLM_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct"
        ),
        "openai": os.environ.get("OPENAI_MODEL", "gpt-4o"),
        "anthropic": os.environ.get(
            "ANTHROPIC_MODEL", "claude-sonnet-4-20250514"
        ),
        "ollama": os.environ.get("OLLAMA_MODEL", "llama3.3"),
    }.get(provider_type, "")


def _default_embed_model(provider_type: str) -> str:
    return {
        "databricks": os.environ.get(
            "DB_EMBED_ENDPOINT", "databricks-bge-large-en"
        ),
        "openai": os.environ.get(
            "OPENAI_EMBED_MODEL", "text-embedding-3-small"
        ),
        "anthropic": "",  # caller falls back to a separate openai backend
        "ollama": os.environ.get("OLLAMA_MODEL", "llama3.3"),
    }.get(provider_type, "")


def _read_toml(toml_path: str) -> dict | None:
    path = Path(toml_path)
    if not path.exists():
        return None
    with path.open("rb") as f:
        data = tomllib.load(f)
    return _substitute_env_vars(data)


def _substitute_env_vars(value: Any) -> Any:
    if isinstance(value, str):
        return _VAR_PATTERN.sub(lambda m: _resolve_env(m.group(1)), value)
    if isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env_vars(v) for v in value]
    return value


def _resolve_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigurationError(
            f"Missing environment variable ${{{name}}} referenced in tiri.toml"
        )
    return value


def _split_route(route: str) -> tuple[str, str]:
    if "::" not in route:
        raise ConfigurationError(
            f"Routing entry {route!r} is malformed; "
            "expected 'provider_name::model_name'"
        )
    backend, model = route.split("::", 1)
    return backend, model


def _apply_engine_tuning_env(cfg: Config) -> None:
    if (v := os.environ.get("TIRI_INTENT_THRESHOLD")) is not None:
        cfg.intent_threshold = float(v)
    if (v := os.environ.get("TIRI_SQL_MAX_RETRIES")) is not None:
        cfg.sql_max_retries = int(v)
    if (v := os.environ.get("TIRI_QUERY_ROW_LIMIT")) is not None:
        cfg.query_row_limit = int(v)
    if (v := os.environ.get("TIRI_EXAMPLE_TOP_K")) is not None:
        cfg.example_top_k = int(v)
    if (v := os.environ.get("TIRI_HISTORY_WINDOW")) is not None:
        cfg.history_window = int(v)
    if (v := os.environ.get("TIRI_PLAN_MAX_STEPS")) is not None:
        cfg.plan_max_steps = int(v)
    if (v := os.environ.get("TIRI_METADATA_CACHE_TTL")) is not None:
        cfg.metadata_cache_ttl = int(v)


def _apply_api_env(cfg: Config) -> None:
    if (v := os.environ.get("AUTH_DISABLED")) is not None:
        cfg.auth_disabled = v.lower() in ("true", "1", "yes")
    if (v := os.environ.get("TIRI_HOST")) is not None:
        cfg.host = v
    if (v := os.environ.get("TIRI_PORT")) is not None:
        cfg.port = int(v)
    if (v := os.environ.get("TIRI_LOG_LEVEL")) is not None:
        cfg.log_level = v
    if (v := os.environ.get("TIRI_CORS_ORIGINS")) is not None:
        cfg.cors_origins = v
