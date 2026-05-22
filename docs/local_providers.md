---
tags: [layer/infrastructure]
status: stable
depends_on: [providers, data_models]
---

# Local providers

## In this system

**Linked from:** [[README]], [[providers]]
**Links to:** [[providers]], [[data_models]]
**Layer:** infrastructure

---

## What this is

Lightweight, zero-dependency implementations of every interface in [[providers]] for local development and testing. No Databricks workspace required. Run the full system against local files and in-memory state.

Set provider selections in environment:
```
LLM_PROVIDER=openai       # or anthropic — real LLM, no Databricks
CATALOG_PROVIDER=static   # reads from a local JSON schema file
QUERY_PROVIDER=duckdb     # runs SQL against local Parquet or CSV files
VECTOR_PROVIDER=chroma    # local ChromaDB
STORE_PROVIDER=sqlite     # local SQLite file
```

---

## OpenAILLMProvider

Implements `LLMProvider`. Drop-in swap for `DatabricksLLMProvider` using the OpenAI Python SDK.

**Configuration:**
```
OPENAI_API_KEY
OPENAI_MODEL        default: gpt-4o
OPENAI_EMBED_MODEL  default: text-embedding-3-small
```

**Notes:**
- `complete()` and `stream()` use `openai.chat.completions.create()`
- `embed()` uses `openai.embeddings.create()`
- Error mapping: `openai.RateLimitError` → `LLMProviderError`, etc.

---

## AnthropicLLMProvider

Implements `LLMProvider`. Uses the Anthropic Python SDK.

**Configuration:**
```
ANTHROPIC_API_KEY
ANTHROPIC_MODEL    default: claude-sonnet-4-20250514
```

**Notes:**
- Anthropic does not provide an embedding API — `embed()` MUST raise `LLMProviderError("Anthropic does not support embeddings")`
- When using `AnthropicLLMProvider` for any completion task, a separate backend providing embeddings MUST be assigned to the `embed` route in `tiri.toml`
- `container.py` validates this at startup — Anthropic as `embed` route raises `ConfigurationError`

---

## OllamaLLMProvider

Implements `LLMProvider`. Uses locally-hosted models via Ollama's OpenAI-compatible API.

**Configuration:**
```
OLLAMA_BASE_URL    default: http://localhost:11434
OLLAMA_MODEL       default: llama3.3
```

**Notes:**
- Uses Ollama's `/api/chat` endpoint (OpenAI-compatible format)
- `embed()` uses Ollama's `/api/embed` endpoint — most Ollama models support this
- No authentication required for local Ollama
- Useful for fully air-gapped development or testing with local models
- In `tiri.toml`, configure as `type = "ollama"` with `base_url` field

---

## StaticMetadataProvider

Implements `MetadataProvider`. In-memory provider constructed from a Python dict or loaded from the `STATIC_SCHEMA_FILE` JSON (extended format). No file I/O per call. Used in tests and local development where you want predictable, deterministic metadata.

**Construction from dict:**
```python
provider = StaticMetadataProvider(name="test_meta", data={
    "tpch.sf1.lineitem": {
        "description": "Individual line items on customer orders",
        "grain": "one row per line item",
        "columns": {
            "l_returnflag": {
                "description": "Return status",
                "synonyms": ["return", "return status"],
                "value_description": "R=returned, A=accepted, N=neither",
                "semantic_type": "category",
            },
            "l_extendedprice": {
                "description": "Gross price before discounts",
                "semantic_type": "currency",
                "currency_code": "USD",
                "is_high_cardinality": True,
            }
        }
    }
})
```

**Extended `STATIC_SCHEMA_FILE` format (optional):**

The `StaticCatalogProvider` schema file can include a `metadata` key per table. If present, `StaticMetadataProvider` reads it. This allows a single file for both physical schema and metadata in local dev:

```json
{
  "tpch.sf1.lineitem": {
    "row_count": 6000000,
    "columns": [
      {"name": "l_returnflag", "data_type": "STRING"},
      {"name": "l_extendedprice", "data_type": "DECIMAL(15,2)"}
    ],
    "metadata": {
      "description": "Individual line items on customer orders",
      "grain": "one row per line item",
      "columns": {
        "l_returnflag": {
          "description": "Return status",
          "synonyms": ["return", "return status"],
          "semantic_type": "category"
        }
      }
    }
  }
}
```

---

## YAMLMetadataProvider (local)

The `YAMLMetadataProvider` defined in [[metadata]] works identically in local development — it reads a YAML file from disk. No special local variant needed. Use `path` pointing to a local file:

```toml
[[metadata.providers.stack]]
name = "domain_yaml"
type = "yaml"
path = "./metadata/tpch_metadata.yaml"
```

A complete `tpch_metadata.yaml` for the demo rooms is provided in `demo/tpch_metadata.yaml`.

---

## StaticCatalogProvider

Implements `CatalogProvider`. Reads physical table schemas from a local JSON file. No live database connection. Returns physical schema only — no descriptive fields.

**Configuration:**
```
STATIC_SCHEMA_FILE    path to schemas.json
```

**Schema file format:**
```json
{
  "catalog.schema.table_name": {
    "row_count": 10000,
    "columns": [
      {"name": "id",     "data_type": "BIGINT"},
      {"name": "status", "data_type": "STRING"},
      {"name": "amount", "data_type": "DECIMAL(10,2)"}
    ]
  }
}
```

Note: no `comment` field — descriptions come from `StaticMetadataProvider` or `YAMLMetadataProvider`, not from `CatalogProvider`.

---

## DuckDBQueryProvider

Implements `QueryProvider`. Runs SQL against local files using DuckDB.

**Configuration:**
```
DUCKDB_DATA_DIR    directory containing Parquet or CSV files
                   files named {schema}__{table}.parquet are auto-registered
                   as catalog.schema.table
```

**Notes:**
- `execute()`: runs `duckdb.connect().execute(sql).fetchdf()`, converts to `QueryResult`
- `validate()`: uses DuckDB's `EXPLAIN` — same SQL prefix approach as [[databricks_providers]]
- Useful for testing SQL generation without a live warehouse
- DuckDB supports most Spark SQL constructs — good fidelity for local testing

---

## ChromaVectorProvider

Implements `VectorProvider`. Uses ChromaDB running locally (in-memory or persisted).

**Configuration:**
```
CHROMA_PATH    local directory for persistence, or ":memory:" for in-process
```

**Notes:**
- Collection name is derived from `DB_VECTOR_INDEX` config value
- `filter` dict is passed directly to ChromaDB's `where` parameter
- Cosine similarity — scores are in [0, 1], higher = more similar

---

## SQLiteStoreProvider

Implements `StoreProvider`. Uses Python's built-in `sqlite3`.

**Configuration:**
```
SQLITE_PATH    path to .db file, default: ./tiri_store.db
```

**Schema:**
```sql
CREATE TABLE IF NOT EXISTS kv_store (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

**Notes:**
- `put()` uses `INSERT OR REPLACE` (SQLite's equivalent of MERGE)
- Thread-safe: use `check_same_thread=False` with a connection per request
- For tests: `:memory:` database, fresh per test

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | `StaticCatalogProvider` with valid schema file | MUST return `TableMeta` with physical schema — empty descriptive fields |
| 2 | `StaticCatalogProvider.get_table_meta()` for unknown table | MUST raise `TableNotFoundError` |
| 3 | `DuckDBQueryProvider.validate()` with valid SQL | MUST return `(True, None)` without executing |
| 4 | `DuckDBQueryProvider.execute()` with `SELECT 1 AS n` | MUST return `QueryResult` with one row `{"n": 1}` |
| 5 | `ChromaVectorProvider` upsert, query, delete round-trip | MUST behave identically to contract in [[providers]] |
| 6 | `SQLiteStoreProvider` in `:memory:` mode | MUST pass all `StoreProvider` contract tests |
| 7 | `AnthropicLLMProvider.embed()` | MUST raise `LLMProviderError` |
| 8 | `OllamaLLMProvider.complete()` with local model running | MUST return non-empty `content` |
| 9 | `StaticMetadataProvider.enrich()` | MUST set description and extend synonyms |
| 10 | `StaticMetadataProvider.enrich()` with no data for a table | MUST skip silently |
| 11 | `YAMLMetadataProvider` with valid YAML file | MUST populate all declared fields correctly |
| 12 | All local providers | MUST be constructable with no network calls |
| 13 | Full engine integration with all-local providers | MUST complete a `chat()` call without any external I/O |
