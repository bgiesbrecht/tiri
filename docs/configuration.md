---
tags: [layer/infrastructure]
status: stable
depends_on: [providers, databricks_providers, local_providers]
---

# Configuration

## In this system

**Linked from:** [[README]]
**Links to:** [[providers]], [[databricks_providers]], [[local_providers]], [[extensions]]
**Layer:** infrastructure

---

## What this is

The complete reference for how Tiri is configured: the provider registry, model routing, and all environment variables. Also defines how `config.py` and `container.py` wire everything together at startup.

A Tiri process reads configuration once at startup. There is no hot-reload. To change configuration, restart the process.

---

## Configuration sources (priority order)

Tiri reads configuration from two sources, with the following priority:

1. **`tiri.toml`** — provider registry and routing (checked into version control; secrets use `${VAR}` placeholders)
2. **Environment variables** — secrets, overrides, and simple single-provider setups

Environment variables always win over `tiri.toml` values when both are present. For local development without a `tiri.toml`, all configuration can be expressed as env vars using the simple provider variables documented below.

---

## Provider registry (`tiri.toml`)

The provider registry is where you declare named LLM backends and assign models to tasks. This is the correct way to configure multi-provider and multi-model setups.

### Why a registry

The fundamental design: **a backend** (vendor + credentials) is separate from **a model** (which model on that backend) which is separate from **a task assignment** (which model handles which agent task).

Without a registry, configuring SQL generation on OpenAI and synthesis on Anthropic simultaneously is impossible — there is nowhere to put both sets of credentials. With a registry, you declare each backend once by name, then reference those names in the routing table.

### File format

```toml
# tiri.toml
# Secrets use ${VAR_NAME} — substituted from environment at startup.
# Missing env var for a ${} reference raises ConfigurationError.

# ── LLM backends ──────────────────────────────────────────────────────────────
# Declare one [llm.providers.NAME] block per backend you want to use.
# NAME is a short identifier you choose — used in [llm.routing] below.

[llm.providers.db_main]
type    = "databricks"
host    = "${DATABRICKS_HOST}"
token   = "${DATABRICKS_TOKEN}"

[llm.providers.openai_main]
type    = "openai"
api_key = "${OPENAI_API_KEY}"

[llm.providers.anthropic_main]
type    = "anthropic"
api_key = "${ANTHROPIC_API_KEY}"

[llm.providers.local_ollama]
type     = "ollama"
base_url = "http://localhost:11434"   # no credentials needed

# ── Model routing ──────────────────────────────────────────────────────────────
# Format: "provider_name::model_name"
# provider_name must match a [llm.providers.NAME] block above.
# All tasks must be assigned. No implicit defaults — be explicit.

[llm.routing]
intent      = "db_main::databricks-meta-llama-3-1-8b-instruct"
planning    = "db_main::databricks-meta-llama-3-3-70b-instruct"
sql         = "db_main::databricks-meta-llama-3-3-70b-instruct"
synthesis   = "db_main::databricks-meta-llama-3-3-70b-instruct"
clarify     = "db_main::databricks-meta-llama-3-1-8b-instruct"
viz_summary = "db_main::databricks-meta-llama-3-1-8b-instruct"
embed       = "db_main::databricks-bge-large-en"

# ── Non-LLM providers ─────────────────────────────────────────────────────────
# These have one instance each — no registry needed.

[providers.catalog]
type = "databricks"     # databricks | static

[providers.query]
type         = "databricks"   # databricks | duckdb
warehouse_id = "${DB_WAREHOUSE_ID}"

[providers.vector]
type     = "databricks"   # databricks | chroma
index    = "main.tiri.example_index"
endpoint = "${DB_VECTOR_ENDPOINT}"

[providers.store]
type  = "databricks"   # databricks | sqlite
table = "main.tiri.kv_store"
```

### Routing task reference

| Task key | Called by | Notes |
|---|---|---|
| `intent` | `IntentAgent` | Classification — fast/cheap model is fine |
| `planning` | `PlanningAgent` (EXT-1) | Benefits from reasoning model |
| `sql` | `SQLAgent` | Most critical task — use best available SQL model |
| `synthesis` | `SynthesisAgent` (EXT-1) | Prose generation and uncertainty framing |
| `clarify` | `ClarifyAgent` | Simple question generation — fast/cheap model |
| `viz_summary` | `VizAgent` | One sentence — fast/cheap model. **MUST NOT be a guardrail-heavy small model in deployments where result rows may contain geopolitical or regulated-domain content** (nation names, financial instruments, controlled substances, sanctioned-entity identifiers, etc). Route to a larger model or a guardrail-free endpoint if in doubt. See `fixme.md` M2 for the real-world observation that motivated this — Databricks' output guardrail on llama-3-1-8b false-flagged `indiscriminate-weapons:true` on benign TPC-H supply-chain rows mentioning IRAN / IRAQ / RUSSIA. VizAgent now degrades to an empty summary on LLM-side failure, but only the larger model avoids the trip-up entirely. |
| `embed` | `ExampleIndexer`, `ContextBuilder` | Must be an embedding model, not a completion model |

### Room calibration and model switching

A room's `text_instruction`, examples, and sample questions are calibrated against the models configured at the time the room was authored and benchmarked. The routing configuration and the room content are a matched pair.

Switching models after a room is in production is a valid operation, but it is not free. Different models interpret ambiguous questions differently, apply confidence thresholds differently, and produce SQL with different formatting conventions. Concretely:

- **IntentAgent routing rate** — some models are more conservative about routing to `ClarifyAgent` than others. Sonnet 4.6 routes more questions to clarification than llama-3-3-70b for the same prompts and the same `TIRI_INTENT_THRESHOLD` setting. Neither is wrong; they are differently calibrated.
- **SQL formatting** — some models wrap SQL in markdown fences despite prompt instructions. SQLAgent now strips these, but the prompt template was written for a model that does not fence.
- **`TIRI_INTENT_THRESHOLD`** — this is a global setting but is effectively a model-specific constant. 0.7 was validated against Databricks llama. Treat it as a tuning parameter when switching models.

**Recommendation:** when switching the model for a room that is already in production, re-run the room's benchmarks before deploying. If the score drops, iterate on `text_instruction` or add worked examples rather than adjusting the threshold globally — the threshold change will affect all rooms.

This is a deliberate tradeoff. Multi-model routing provides vendor flexibility and cost optimisation, but it places the calibration responsibility on the room author rather than on the platform. Operators who prefer predictability over flexibility should route all tasks to a single, stable model and treat that model as part of the room's specification.

### Supported backend types

| `type` value | Class | Notes |
|---|---|---|
| `databricks` | `DatabricksLLMProvider` | Model Serving endpoint |
| `openai` | `OpenAILLMProvider` | Any OpenAI-compatible API |
| `anthropic` | `AnthropicLLMProvider` | Note: no native embed — see below |
| `ollama` | `OllamaLLMProvider` | Local models via Ollama |

**Anthropic + embedding:** Anthropic does not provide an embedding API. If `anthropic_main` is used for any completion task, a separate backend providing embeddings MUST be assigned to the `embed` route. `container.py` validates this at startup and raises `ConfigurationError` if embed routes to an Anthropic backend.

---

## Simple env-var configuration (no `tiri.toml`)

For local development and simple single-provider deployments, all configuration can be expressed as environment variables without a `tiri.toml`. The `container.py` detects the absence of `tiri.toml` and falls back to this mode.

In simple mode, all LLM tasks route to a single backend with a single model. This is sufficient for getting started but does not support multi-provider routing.

### Authentication

| Variable | Required | Description |
|---|---|---|
| `DATABRICKS_HOST` | If using Databricks | Workspace URL |
| `DATABRICKS_TOKEN` | If using Databricks | PAT or OAuth token |
| `OPENAI_API_KEY` | If `LLM_PROVIDER=openai` | OpenAI API key |
| `ANTHROPIC_API_KEY` | If `LLM_PROVIDER=anthropic` | Anthropic API key |
| `AUTH_DISABLED` | No | `true` to skip Bearer validation. Dev only. |

### Provider selection (simple mode)

| Variable | Default | Valid values |
|---|---|---|
| `LLM_PROVIDER` | `databricks` | `databricks`, `openai`, `anthropic`, `ollama` |
| `CATALOG_PROVIDER` | `databricks` | `databricks`, `static` |
| `QUERY_PROVIDER` | `databricks` | `databricks`, `duckdb` |
| `VECTOR_PROVIDER` | `databricks` | `databricks`, `chroma` |
| `STORE_PROVIDER` | `databricks` | `databricks`, `sqlite` |

### Simple mode provider settings

| Variable | Default | Description |
|---|---|---|
| `DB_LLM_ENDPOINT` | `databricks-meta-llama-3-3-70b-instruct` | Completion endpoint |
| `DB_EMBED_ENDPOINT` | `databricks-bge-large-en` | Embedding endpoint |
| `DB_WAREHOUSE_ID` | — | SQL Warehouse ID (required) |
| `DB_VECTOR_INDEX` | `main.tiri.example_index` | Vector Search index |
| `DB_VECTOR_ENDPOINT` | — | Vector Search endpoint name |
| `DB_STORE_TABLE` | `main.tiri.kv_store` | Delta KV store table |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI completion model |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-small` | OpenAI embedding model |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Anthropic model |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `llama3.3` | Ollama model name |
| `STATIC_SCHEMA_FILE` | `schemas.json` | Static catalog schema file |
| `DUCKDB_DATA_DIR` | `./data` | DuckDB data directory |
| `CHROMA_PATH` | `:memory:` | ChromaDB path |
| `SQLITE_PATH` | `./tiri_store.db` | SQLite path |

---

## Engine tuning

These apply in both `tiri.toml` and simple env-var mode.

| Variable | Default | Description | Used by |
|---|---|---|---|
| `TIRI_INTENT_THRESHOLD` | `0.7` | Confidence below which routes to ClarifyAgent | `IntentAgent` |
| `TIRI_SQL_MAX_RETRIES` | `3` | Max self-correction attempts | `SQLAgent` |
| `TIRI_QUERY_ROW_LIMIT` | `10000` | Max rows per query | `QueryProvider` |
| `TIRI_EXAMPLE_TOP_K` | `5` | Similar examples retrieved per question | `ExampleIndexer` |
| `TIRI_HISTORY_WINDOW` | `10` | Past turns included in context | `ContextBuilder` |
| `TIRI_PLAN_MAX_STEPS` | `5` | Max steps in a reasoning plan | `PlanningAgent` |
| `TIRI_METADATA_CACHE_TTL` | `0` | Table metadata cache TTL in seconds. `0` = no cache | `MetadataFetcher` |

---

## API server

| Variable | Default | Description |
|---|---|---|
| `TIRI_HOST` | `0.0.0.0` | Bind host |
| `TIRI_PORT` | `8000` | Port |
| `TIRI_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `TIRI_CORS_ORIGINS` | `*` | Comma-separated allowed origins |

---

## Room config environment substitution

Any string field in a room config JSON may contain `${VAR_NAME}` placeholders. The loader substitutes from environment at load time.

```json
{"warehouse_id": "${DB_WAREHOUSE_ID}"}
```

**Rules:**
- Substitution applies to all string fields before validation
- Missing env var for a `${}` reference MUST raise `ConfigurationError` with the variable name
- Nested substitution is supported: `"tpch.${CATALOG_ENV}.customer"`
- Applies to both `tiri.toml` values and room config JSON

---

## Metadata provider stack (`tiri.toml`)

The metadata stack is configured as an ordered list in `tiri.toml`. Providers are applied left to right (top to bottom). For scalar fields, later entries override earlier ones. For list fields (`synonyms`, `sample_values`, `recommended_joins`), all entries accumulate. `RoomConfigMetadataProvider` is always appended last automatically.

```toml
# ── Metadata provider stack ───────────────────────────────────────────────────
# Applied in order. Omit this section to use UC annotations only (default).

[[metadata.providers.stack]]
name                    = "uc_annotations"
type                    = "uc_annotations"
sample_values_enabled   = true
sample_values_max_distinct = 50

[[metadata.providers.stack]]
name = "domain_yaml"
type = "yaml"
path = "./metadata/domain_metadata.yaml"

[[metadata.providers.stack]]
name  = "metrics_table"
type  = "delta_table"
table = "main.tiri_meta.metadata"

[[metadata.providers.stack]]
name          = "dbt_manifest"
type          = "dbt"
manifest_path = "./dbt/target/manifest.json"
catalog_path  = "./dbt/target/catalog.json"
```

See [[metadata]] for the full YAML format, Delta table schema, and merge rules.

**Supported `type` values:**

| Type | Implementation | Source |
|---|---|---|
| `uc_annotations` | `UCAnnotationsMetadataProvider` | Unity Catalog table/column comments |
| `yaml` | `YAMLMetadataProvider` | Human-authored YAML file |
| `delta_table` | `DeltaTableMetadataProvider` | Delta table with (table, column, field, value) rows |
| `dbt` | `DbtMetadataProvider` | dbt manifest.json |
| `static` | `StaticMetadataProvider` | In-memory dict (dev/test only) |

**Default behavior (no `[metadata.providers]` section):** `UCAnnotationsMetadataProvider` is used as the sole provider, with `sample_values_enabled = true` and `sample_values_max_distinct = 50`. This is equivalent to what Genie does.

---

## `config.py`

Reads configuration from `tiri.toml` (if present) and environment variables. All components import from `config` — never from `os.environ` or `tomllib` directly.

```python
@dataclass
class ProviderBackendConfig:
    """One named LLM backend in the registry."""
    name: str            # the key from [llm.providers.NAME]
    type: str            # "databricks" | "openai" | "anthropic" | "ollama"
    host: str = ""       # databricks only
    token: str = ""      # databricks / openai / anthropic
    api_key: str = ""    # openai / anthropic
    base_url: str = ""   # ollama

@dataclass
class RoutingConfig:
    """Task-to-backend::model assignments."""
    intent: str      # "provider_name::model_name"
    planning: str    # EXT-1: PlanningAgent — benefits from reasoning model
    sql: str
    synthesis: str   # EXT-1/EXT-7: SynthesisAgent — prose generation and uncertainty framing
    clarify: str
    viz_summary: str
    embed: str

@dataclass
class Config:
    # LLM registry (populated from tiri.toml or synthesized from simple env vars)
    llm_backends: dict[str, ProviderBackendConfig]   # name → config
    llm_routing: RoutingConfig

    # Metadata provider stack (populated from tiri.toml [[metadata.providers.stack]])
    metadata_provider_configs: list[dict] = field(default_factory=list)
    # Each dict has at minimum: {"name": str, "type": str}
    # Additional keys depend on type — see metadata provider section above
    # Empty list = use UCAnnotationsMetadataProvider as default

    # Non-LLM providers (one each)
    catalog_provider: str = "databricks"
    query_provider: str = "databricks"
    vector_provider: str = "databricks"
    store_provider: str = "databricks"

    # Non-LLM provider settings
    db_warehouse_id: str = ""
    db_vector_index: str = "main.tiri.example_index"
    db_vector_endpoint: str = ""
    db_store_table: str = "main.tiri.kv_store"
    static_schema_file: str = "schemas.json"
    duckdb_data_dir: str = "./data"
    chroma_path: str = ":memory:"
    sqlite_path: str = "./tiri_store.db"

    # Databricks workspace credentials. Populated from DATABRICKS_HOST /
    # DATABRICKS_TOKEN environment variables. Required for the Databricks
    # provider implementations (LLM, Catalog, Query, Vector, Store). Also
    # consumed by the CLI `import-genie --space-id` path. Empty when running
    # fully local with no Databricks dependencies.
    databricks_host: str = ""
    databricks_token: str = ""

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
    def load(cls, toml_path: str = "tiri.toml") -> "Config":
        """
        Load from tiri.toml if it exists, otherwise from environment variables.
        Apply ${VAR} substitution. Validate. Return Config.
        Raises ConfigurationError on any missing required value.
        """
```

**Validation rules (MUST):**
- Every `${VAR}` reference MUST resolve to a non-empty string
- Every backend referenced in `llm_routing` MUST have a corresponding entry in `llm_backends`
- `embed` route MUST NOT reference an Anthropic backend
- If `query_provider == "databricks"`, `db_warehouse_id` MUST be non-empty
- If `vector_provider == "databricks"`, `db_vector_index` and `db_vector_endpoint` MUST be non-empty
- `auth_disabled == True` MUST log a WARNING at startup

---

## `container.py`

Instantiates all providers from `Config`. The key change from the old design: `_build_llm_registry()` instantiates **N backend objects** (one per named provider in `llm_backends`), then wires them into `RouterLLMProvider` using the routing table.

```python
def build_container(cfg: Config) -> dict:
    """
    Returns:
    {
      "llm":                RouterLLMProvider instance (always)
      "catalog":            CatalogProvider instance
      "metadata_providers": list[MetadataProvider] in configured stack order
      "query":              QueryProvider instance
      "vector":             VectorProvider instance
      "store":              StoreProvider instance
    }

    MVP note (Step 4): implement a minimal RouterLLMProvider that wraps a single
    backend and routes all tasks to it. This satisfies the "always RouterLLMProvider"
    contract and allows the embed-route Anthropic validation check to run at startup.
    EXT-3 adds multi-backend support to this same class — it is not a rewrite.
    """

def _build_metadata_providers(cfg: Config) -> list[MetadataProvider]:
    """
    Instantiate MetadataProvider instances from cfg.metadata_provider_configs.
    Apply in the order declared in tiri.toml [[metadata.providers.stack]].
    If metadata_provider_configs is empty, return [UCAnnotationsMetadataProvider()]
    as the default.
    Never include RoomConfigMetadataProvider here — MetadataFetcher adds it last.
    """

def _build_llm_registry(cfg: Config) -> RouterLLMProvider:
    """
    1. For each (name, backend_cfg) in cfg.llm_backends:
          instantiate the appropriate LLMProvider subclass
          store in a dict: name → provider_instance
    2. Parse cfg.llm_routing: each "name::model" string splits into
          (backend_name, model_name)
    3. Construct a ModelRoute per task, referencing the provider instance
    4. Return RouterLLMProvider(routes={task: ModelRoute(...)})
    """

def _parse_route(route_str: str, registry: dict) -> ModelRoute:
    """
    Parse "provider_name::model_name" into a ModelRoute.
    Raises ConfigurationError if provider_name is not in registry.
    """
```

`RouterLLMProvider` is always returned as the `llm` entry — even when only one backend is configured. This means agents never need to know whether routing is active.

---

## Standard configurations

### Databricks production — single backend, cost-optimized routing (`tiri.toml`)

```toml
[llm.providers.db]
type  = "databricks"
host  = "${DATABRICKS_HOST}"
token = "${DATABRICKS_TOKEN}"

[llm.routing]
intent      = "db::databricks-meta-llama-3-1-8b-instruct"
planning    = "db::databricks-meta-llama-3-3-70b-instruct"
sql         = "db::databricks-meta-llama-3-3-70b-instruct"
synthesis   = "db::databricks-meta-llama-3-3-70b-instruct"
clarify     = "db::databricks-meta-llama-3-1-8b-instruct"
viz_summary = "db::databricks-meta-llama-3-1-8b-instruct"
embed       = "db::databricks-bge-large-en"

[providers.query]
type         = "databricks"
warehouse_id = "${DB_WAREHOUSE_ID}"

[providers.vector]
type     = "databricks"
endpoint = "${DB_VECTOR_ENDPOINT}"
```

### Multi-vendor — Databricks catalog/query, OpenAI SQL, Anthropic synthesis (`tiri.toml`)

```toml
[llm.providers.db]
type  = "databricks"
host  = "${DATABRICKS_HOST}"
token = "${DATABRICKS_TOKEN}"

[llm.providers.oai]
type    = "openai"
api_key = "${OPENAI_API_KEY}"

[llm.providers.ant]
type    = "anthropic"
api_key = "${ANTHROPIC_API_KEY}"

[llm.routing]
intent      = "db::databricks-meta-llama-3-1-8b-instruct"
planning    = "oai::gpt-4o"
sql         = "oai::gpt-4o"
synthesis   = "ant::claude-sonnet-4-20250514"
clarify     = "db::databricks-meta-llama-3-1-8b-instruct"
viz_summary = "db::databricks-meta-llama-3-1-8b-instruct"
embed       = "oai::text-embedding-3-small"

[providers.query]
type         = "databricks"
warehouse_id = "${DB_WAREHOUSE_ID}"

[providers.vector]
type     = "databricks"
endpoint = "${DB_VECTOR_ENDPOINT}"
```

### Local development — env vars only, no `tiri.toml`

```bash
LLM_PROVIDER=openai
OPENAI_API_KEY=<key>
CATALOG_PROVIDER=static
STATIC_SCHEMA_FILE=./schemas/tpch_sf1.json
QUERY_PROVIDER=duckdb
DUCKDB_DATA_DIR=./data/tpch_sf1
VECTOR_PROVIDER=chroma
CHROMA_PATH=./data/chroma
STORE_PROVIDER=sqlite
SQLITE_PATH=./data/tiri_store.db
AUTH_DISABLED=true
```

In this mode, all LLM tasks route to the single `openai` backend with `OPENAI_MODEL`.

### CI / testing — fully local, no external calls

```bash
LLM_PROVIDER=openai        # use a mock/test-double in tests
CATALOG_PROVIDER=static
STATIC_SCHEMA_FILE=./tests/fixtures/schemas.json
QUERY_PROVIDER=duckdb
DUCKDB_DATA_DIR=./tests/fixtures/data
VECTOR_PROVIDER=chroma
CHROMA_PATH=:memory:
STORE_PROVIDER=sqlite
SQLITE_PATH=:memory:
AUTH_DISABLED=true
```

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | `Config.load()` with valid `tiri.toml` | MUST parse all backends and routing correctly |
| 2 | `Config.load()` with no `tiri.toml` and `LLM_PROVIDER=openai` | MUST synthesize a single-backend registry |
| 3 | `tiri.toml` with `${MISSING_VAR}` | MUST raise `ConfigurationError` naming the missing variable |
| 4 | Routing entry referencing undefined backend name | MUST raise `ConfigurationError` at startup |
| 5 | `embed` route pointing to Anthropic backend | MUST raise `ConfigurationError` at startup |
| 6 | `build_container()` with two-backend config | MUST instantiate two separate `LLMProvider` objects |
| 7 | `build_container()` with single-backend config | MUST still return `RouterLLMProvider` as the `llm` entry |
| 8 | Any component | MUST import config from `config.py`, never `os.environ` or `tomllib` directly |
| 9 | `auth_disabled=True` | MUST log WARNING at startup regardless of environment |
| 10 | `build_container()` with all-local config | MUST complete with no network calls |
