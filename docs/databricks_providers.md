---
tags: [layer/infrastructure]
status: stable
depends_on: [providers, data_models]
---

# Databricks providers

## In this system

**Linked from:** [[README]], [[providers]]
**Links to:** [[providers]], [[data_models]]
**Layer:** infrastructure

---

## What this is

The default, production implementation of every interface defined in [[providers]], targeting Databricks as the infrastructure. Each class is a drop-in replacement — it conforms to the ABC and adds no public methods beyond those defined in the interface.

Swap any one independently by changing the corresponding entry in `container.py`. See [[README]] key design rules: the engine never imports from this module directly.

---

## DatabricksLLMProvider

Implements `LLMProvider`. Calls Databricks Model Serving, which exposes an OpenAI-compatible chat completions endpoint.

**Configuration (env vars):**
```
DB_LLM_ENDPOINT    e.g. databricks-meta-llama-3-3-70b-instruct
DB_EMBED_ENDPOINT  e.g. databricks-bge-large-en
DATABRICKS_HOST
DATABRICKS_TOKEN
```

**`complete()` implementation:**
- `POST {DATABRICKS_HOST}/serving-endpoints/{DB_LLM_ENDPOINT}/invocations`
- Body: OpenAI-format `{"messages": [...], "temperature": ..., "max_tokens": ...}`
- Parse `response["choices"][0]["message"]["content"]`

**`stream()` implementation:**
- Same endpoint with `"stream": true`
- Parse SSE chunks, yield `delta.content` strings
- Accumulate total content for `usage` approximation (Model Serving may not return usage on streaming responses)

**`embed()` implementation:**
- `POST {DATABRICKS_HOST}/serving-endpoints/{DB_EMBED_ENDPOINT}/invocations`
- Body: `{"input": [text1, text2, ...]}`
- Parse `response["data"][i]["embedding"]` for each input

**Error handling:**
- HTTP 429 → retry with exponential backoff (max 3 attempts)
- HTTP 4xx (not 429) → raise `LLMProviderError` immediately
- HTTP 5xx → retry once, then raise `LLMProviderError`

---

## DatabricksCatalogProvider

Implements `CatalogProvider`. Uses `databricks-sdk` `WorkspaceClient`. Returns physical schema only — column names, data types, row counts. Does not populate descriptive fields.

**Configuration:**
```
DATABRICKS_HOST
DATABRICKS_TOKEN
```

**`get_table_meta()` implementation:**
- `client.tables.get(full_name)` → `TableInfo`
- Map `TableInfo.columns` to `ColumnMeta` list (name + data_type only)
- Set `row_count` from `TableInfo.properties` if available
- Leave all descriptive fields (`description`, `synonyms`, etc.) empty — those are for `UCAnnotationsMetadataProvider`

**`list_tables()` implementation:**
- `client.tables.list(catalog_name=catalog, schema_name=schema)`
- Unity Catalog handles permission filtering automatically

**`search_tables()` implementation:**
- `client.tables.list()` then client-side substring filter on name
- Used by management API and EXT-2 dynamic table selection

---

## UCAnnotationsMetadataProvider

Implements `MetadataProvider`. Reads Unity Catalog table and column comments. Typically the first entry in the metadata stack — provides baseline descriptions that YAML or other sources can override.

**What it provides:**
- `TableMeta.description` from UC table comment
- `ColumnMeta.description` from UC column comment
- `ColumnMeta.sample_values` for eligible columns (runs `SELECT DISTINCT` via `QueryProvider`)

**Sample value eligibility:**
- `data_type` is STRING or VARCHAR
- `row_count` < `sample_values_max_distinct` threshold (default 50 distinct values)
- Column name is not in exclusion list: `id`, `uuid`, `email`, `description`, `notes`, `comment`

**Configuration in `tiri.toml`:**
```toml
[[metadata.providers.stack]]
name                       = "uc_annotations"
type                       = "uc_annotations"
sample_values_enabled      = true
sample_values_max_distinct = 50
```

**`enrich()` implementation:**
1. For each table in `tables`: `client.tables.get(full_name)` to get comments
2. If `TableInfo.comment` is non-empty and `TableMeta.description` is empty: set it
3. If `TableInfo.comment` is non-empty and `TableMeta.description` is already set: record `MetadataConflict`, override
4. For each column: same pattern for `ColumnMeta.description`
5. If `sample_values_enabled`: for eligible columns, run `SELECT DISTINCT {col} FROM {table} LIMIT {max}` via `query_provider`
6. Extend `ColumnMeta.sample_values` with results (deduplicated)
7. Append `"uc_annotations"` to `TableMeta.metadata_sources`

---

## DatabricksQueryProvider

Implements `QueryProvider`. Uses the Databricks SQL Statement Execution API.

**Configuration:**
```
DATABRICKS_HOST
DATABRICKS_TOKEN
DB_WAREHOUSE_ID    (default, overrideable per-room via RoomConfig.warehouse_id)
```

**`execute()` implementation:**
1. `POST /api/2.0/sql/statements` with body:
   ```json
   {
     "statement": "<sql> LIMIT <limit>",
     "warehouse_id": "<id>",
     "wait_timeout": "30s",
     "disposition": "INLINE"
   }
   ```
2. If `status.state == "PENDING"` or `"RUNNING"`: poll `GET /api/2.0/sql/statements/{id}` every 1s until terminal state
3. If `SUCCEEDED`: map `result.data_array` + `manifest.schema.columns` to `QueryResult`
4. Set `truncated = True` if `result.row_count >= limit`
5. If `FAILED`: raise `QueryProviderError` with `status.error.message`

**`validate()` implementation:**
- Prefix the SQL with `EXPLAIN ` and call execute with `wait_timeout=10s`
- If `SUCCEEDED`: return `(True, None)`
- If `FAILED`: return `(False, error_message)`
- MUST use a minimal warehouse compute size to keep validation fast

---

## DatabricksVectorProvider

Implements `VectorProvider`. Uses Databricks Vector Search REST API with a Direct Access index (caller manages the index content, not a Delta sync).

**Configuration:**
```
DATABRICKS_HOST
DATABRICKS_TOKEN
DB_VECTOR_INDEX    e.g. main.tiri.example_index
```

**Index creation** (run once at setup, not at request time):
```
POST /api/2.0/vector-search/indexes
{
  "name": "<DB_VECTOR_INDEX>",
  "endpoint_name": "<vector_search_endpoint>",
  "primary_key": "id",
  "index_type": "DIRECT_ACCESS",
  "embedding_vector_columns": [{"name": "vector", "embedding_dimension": 1024}]
}
```

**`upsert()` implementation:**
- `PUT /api/2.0/vector-search/indexes/{index}/upsert-data`
- Body: `{"inputs_json": json.dumps([{"id": id, "vector": vector, **payload}])}`

**`query()` implementation:**
- `POST /api/2.0/vector-search/indexes/{index}/query`
- Body: `{"query_vector": vector, "num_results": top_k, "filters_json": json.dumps(filter or {})}`
- Map response to `list[VectorMatch]`

**`delete()` implementation:**
- `DELETE /api/2.0/vector-search/indexes/{index}/delete-data`
- Body: `{"primary_keys": [id]}`

---

## DatabricksStoreProvider

Implements `StoreProvider`. Backed by a Delta table in Unity Catalog, accessed via `DatabricksQueryProvider`.

**Configuration:**
```
DB_STORE_TABLE    e.g. main.tiri.kv_store
```

**Table schema** (created once at setup):
```sql
CREATE TABLE IF NOT EXISTS {DB_STORE_TABLE} (
    key       STRING NOT NULL,
    value     STRING NOT NULL,     -- JSON-serialized dict
    updated_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');
```

**`get()` implementation:**
```sql
SELECT value FROM {table} WHERE key = '{key}' LIMIT 1
```
Return `json.loads(row["value"])` or `None` if no row.

**`put()` implementation:**
```sql
MERGE INTO {table} AS target
USING (SELECT '{key}' AS key, '{value}' AS value, current_timestamp() AS updated_at) AS source
ON target.key = source.key
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
```

**`list_keys()` implementation:**
```sql
SELECT key FROM {table} WHERE key LIKE '{prefix}%' ORDER BY key
```

**`delete()` implementation:**
```sql
DELETE FROM {table} WHERE key = '{key}'
```

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | `DatabricksLLMProvider.complete()` with valid endpoint | MUST return non-empty `content` |
| 2 | `DatabricksLLMProvider.complete()` with HTTP 429 | MUST retry up to 3 times before raising |
| 3 | `DatabricksCatalogProvider.get_table_meta()` for known table | MUST return correct column names and types |
| 4 | `DatabricksQueryProvider.validate()` with `SELECT 1` | MUST return `(True, None)` |
| 5 | `DatabricksQueryProvider.validate()` with `SELECT * FROM nonexistent_table` | MUST return `(False, non-empty string)` |
| 6 | `DatabricksQueryProvider.execute()` result | MUST NOT exceed `limit` rows |
| 7 | `DatabricksVectorProvider.upsert()` then `query()` with same vector | MUST return that entry in top result |
| 8 | `DatabricksStoreProvider.put()` then `get()` | MUST return identical dict |
| 9 | `DatabricksStoreProvider.get()` missing key | MUST return `None` |
| 10 | All providers | MUST raise appropriate `ProviderError` subclass on auth failure |
