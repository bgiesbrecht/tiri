---
tags: [layer/intelligence]
status: stable
depends_on: [providers, data_models]
---

# Metadata

## In this system

**Linked from:** [[README]], [[knowledge_store]], [[providers]], [[configuration]]
**Links to:** [[providers]], [[data_models]], [[knowledge_store]], [[configuration]]
**Layer:** intelligence

---

## What this is

The metadata system is how Tiri understands what your data *means*, not just what it *is*. A column named `l_extendedprice` means nothing to an LLM or a business user. A column described as "the list price for this line item before discounts, in USD" means something precise.

Tiri treats metadata as a curated, layered artifact — not a read-only output from a single source. Multiple metadata providers stack in priority order, each contributing or overriding specific fields. The fully-resolved result is what agents receive in a `ContextPackage`.

This is a deliberate departure from Genie, which reads metadata only from Unity Catalog. Tiri is a platform: your metadata can come from UC, YAML files, Delta tables, dbt manifests, OpenMetadata, or any combination.

---

## The metadata stack

Four conceptual layers, applied in order:

```
Layer 1 — Physical schema (CatalogProvider)
           What physically exists: table names, column names, data types, row counts.
           Always the base. Cannot be overridden.

Layer 2 — Catalog annotations (UCAnnotationsMetadataProvider, etc.)
           What the catalog knows about meaning: comments, tags, properties.
           Often incomplete or stale. Overrides nothing — just fills gaps.

Layer 3 — External metadata sources (YAML, Delta table, dbt, OpenMetadata, ...)
           Human-curated or tool-generated descriptions.
           Richer than catalog annotations. Override layer 2 for any field they define.
           Multiple external sources can be stacked — applied in configured order.

Layer 4 — Room-level overrides (RoomConfigMetadataProvider)
           Room-specific context that doesn't belong in the global catalog.
           Always last. Always wins.
```

The stack is configured in `tiri.toml` as an ordered list. See [[configuration]].

---

## Merge rules

Two rules govern how providers combine their contributions:

**Rule 1 — Scalar fields: last writer wins.**
For any scalar field (`description`, `grain`, `semantic_type`, `default_date_column`, etc.), the last provider in the stack that supplies a non-empty value for that field wins. Earlier providers are overridden.

**Rule 2 — List fields: all providers accumulate.**
For list fields (`synonyms`, `sample_values`, `recommended_joins`, `metadata_sources`), every provider contributes its values. The resolved field is the deduplicated union across all providers.

The rationale: you want the most specific description (last wins), but you want all known synonyms and all sample values (accumulate). These are the two fundamentally different things metadata sources do.

**Conflict recording:**
When two providers supply different non-empty scalar values for the same field, a `MetadataConflict` entry is recorded on `TableMeta.conflicts`. The resolved value is still the last-writer's value — conflicts don't block resolution. But they are visible to room admins for metadata quality review.

```python
# Example: UC says "Orders table", YAML says "Customer purchase orders"
# YAML wins (last in stack), conflict recorded:
conflict = MetadataConflict(
    table="tpch.sf1.orders",
    field="description",
    values={"uc_annotations": "Orders table", "domain_yaml": "Customer purchase orders"},
    resolved_to="domain_yaml",
)
```

---

## MetadataProvider interface

Defined in [[providers]]. Each implementation enriches a dict of `TableMeta` objects in place. Only set fields you have data for — leave others at their current value.

```python
class MetadataProvider(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for provenance tracking. Used in conflict records."""
        ...

    @abstractmethod
    async def enrich(
        self,
        tables: dict[str, TableMeta],   # keyed by full table name; mutate in place
        room_config: RoomConfig,
    ) -> None:
        """
        Enrich TableMeta objects with metadata from this source.

        Rules:
        - Only set fields where you have data. Leave others unchanged.
        - Scalar fields: assign directly (last writer wins — enforced by stack order).
        - List fields: EXTEND, do not replace. Use list.extend() or +=.
        - Append your name to table.metadata_sources for every table you touch.
        - Record MetadataConflict when you override a non-empty scalar field.
        """
```

---

## Implementations

### UCAnnotationsMetadataProvider

Reads table and column comments from Unity Catalog using `databricks-sdk`. Typically Layer 2.

**What it provides:** `description` from table comment, `ColumnMeta.description` from column comments, `sample_values` for low-cardinality string columns (via a DISTINCT query).

**What it does not provide:** synonyms, semantic types, grain, behavioral hints. UC doesn't have these concepts.

**Configuration in `tiri.toml`:**
```toml
[[metadata.providers.stack]]
name = "uc_annotations"
type = "uc_annotations"
# No additional config — uses the same Databricks credentials as CatalogProvider
sample_values_enabled = true        # default: true
sample_values_max_distinct = 50     # only populate if DISTINCT count <= this
```

---

### YAMLMetadataProvider

Reads a YAML file containing human-authored table and column metadata. Typically Layer 3. Version-controllable alongside code. Use for domain knowledge that doesn't belong in UC.

**YAML format:**

```yaml
# tiri_metadata.yaml
# Human-maintained metadata. All fields are optional.
# This file is room-independent — it describes tables, not rooms.

tables:
  tpch.sf1.lineitem:
    description: "Individual line items on customer orders. One row per product per order."
    grain: "one row per line item — a unique combination of order, part, and supplier"
    domain: sales
    freshness: daily
    default_date_column: l_shipdate
    synonyms:
      - line items
      - order lines
    recommended_joins:
      - tpch.sf1.orders
      - tpch.sf1.part
      - tpch.sf1.supplier
    columns:
      l_returnflag:
        description: "Whether this line item was returned, accepted, or is still open"
        value_description: "R = returned by customer, A = accepted, N = not yet determined"
        synonyms: [return status, return flag, return]
        semantic_type: category
      l_extendedprice:
        description: "The gross price for this line item before any discounts are applied"
        semantic_type: currency
        currency_code: USD
        is_high_cardinality: true
      l_discount:
        description: "Fractional discount applied to this line item"
        value_description: "Decimal 0.00 to 0.10 — multiply by 100 for percentage"
      l_shipdate:
        description: "Date the line item was shipped to the customer"
        semantic_type: date
      l_commitdate:
        description: "Promised delivery date for this line item"
        semantic_type: date
      l_receiptdate:
        description: "Date the customer actually received this line item"
        semantic_type: date

  tpch.sf1.orders:
    description: "Customer purchase orders. One row per order."
    grain: "one row per order — identified by o_orderkey"
    domain: sales
    default_date_column: o_orderdate
    default_filter: ""              # no default filter — all orders included
    columns:
      o_orderpriority:
        description: "The urgency level of this order"
        value_description: "1-URGENT, 2-HIGH, 3-MEDIUM, 4-NOT SPECIFIED, 5-LOW"
        semantic_type: category
      o_orderstatus:
        description: "Current fulfillment status of the order"
        value_description: "O = open (has unshipped items), F = fully fulfilled, P = partial"
        semantic_type: category
```

**Configuration in `tiri.toml`:**
```toml
[[metadata.providers.stack]]
name = "domain_yaml"
type = "yaml"
path = "./metadata/domain_metadata.yaml"   # relative to tiri.toml location
# Can also be an absolute path or ${VAR} reference
```

---

### DeltaTableMetadataProvider

Reads metadata from a Delta table. Use when your organization manages metadata as data — stored, versioned, and governed in the lakehouse itself. Compatible with any tooling that can write to Delta.

**Table schema (create once):**

```sql
CREATE TABLE IF NOT EXISTS main.tiri_meta.metadata (
    table_name    STRING NOT NULL,   -- fully-qualified: catalog.schema.table
    column_name   STRING,            -- NULL for table-level fields
    field_name    STRING NOT NULL,   -- e.g. "description", "grain", "semantic_type"
    field_value   STRING NOT NULL,   -- always stored as string; parsed per field type
    source        STRING,            -- optional: who/what wrote this row
    updated_at    TIMESTAMP
) USING DELTA;
```

**Example rows:**
```
tpch.sf1.lineitem | NULL          | description   | "Individual line items..." | data_team | 2025-01-01
tpch.sf1.lineitem | l_returnflag  | description   | "Whether this line item..." | analyst_a | 2025-01-01
tpch.sf1.lineitem | l_returnflag  | semantic_type | "category"                 | auto       | 2025-01-01
tpch.sf1.lineitem | l_extendedprice | synonyms    | "price,gross price"        | analyst_a | 2025-01-01
```

For list fields (`synonyms`, `sample_values`, `recommended_joins`), `field_value` is a comma-separated string. The provider splits on comma and extends the list.

**Configuration in `tiri.toml`:**
```toml
[[metadata.providers.stack]]
name   = "metrics_table"
type   = "delta_table"
table  = "main.tiri_meta.metadata"
# Uses the same warehouse_id as the room's query provider
```

---

### DbtMetadataProvider

Reads a dbt `manifest.json` and imports descriptions, column descriptions, tests (for deriving `is_primary_key`, value constraints), and relationships (for `recommended_joins`).

**What it provides:** `description` from `node.description`, `ColumnMeta.description` from `column.description`, `is_primary_key` inferred from `unique` + `not_null` tests, `recommended_joins` from relationships tests.

**Configuration in `tiri.toml`:**
```toml
[[metadata.providers.stack]]
name          = "dbt_manifest"
type          = "dbt"
manifest_path = "./dbt/target/manifest.json"
catalog_path  = "./dbt/target/catalog.json"   # optional; provides row counts
```

---

### RoomConfigMetadataProvider

Applies room-level metadata overrides from `RoomConfig`. Always last in the stack. Cannot be repositioned. Ensures room-specific context always wins.

**What it provides:** Everything in `RoomConfig` that has a metadata analog — the `text_instruction` contributes context but not per-column fields. The room config's join specs contribute `recommended_joins`. Column-level synonyms and descriptions defined in room config override global metadata.

**RoomConfig additions for column-level metadata:**

```python
@dataclass
class ColumnOverride:
    """Room-specific metadata for one column. Overrides global metadata."""
    table: str         # fully-qualified table name
    column: str        # column name
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    value_description: str = ""
    default_filter: str = ""   # e.g. "status = 'active'" — apply for this room only

@dataclass
class RoomConfig:
    # ... existing fields ...
    column_overrides: list[ColumnOverride] = field(default_factory=list)
```

**Configuration:** The `RoomConfigMetadataProvider` is always implicitly last. You do not declare it in `tiri.toml` — `MetadataFetcher` adds it automatically.

---

### StaticMetadataProvider (local dev / testing)

In-memory provider constructed from a Python dict. No file I/O. Used in tests and local development where you want predictable metadata without files.

```python
provider = StaticMetadataProvider(name="test_meta", data={
    "tpch.sf1.lineitem": {
        "description": "Line items",
        "columns": {
            "l_returnflag": {"description": "Return status", "synonyms": ["return"]}
        }
    }
})
```

---

## ContextPackage metadata fields

After the full stack runs, `ContextPackage.table_schemas` contains the resolved `TableMeta` objects. Agents consume these directly. The SQL generation prompt uses:

- `TableMeta.description` → table description line in the prompt
- `TableMeta.grain` → "one row per X" — critical context for correct aggregation
- `TableMeta.default_date_column` → used when user says "this year" without specifying a column
- `TableMeta.default_filter` → applied automatically unless the user says otherwise
- `ColumnMeta.description` → column annotation in the prompt
- `ColumnMeta.synonyms` → used by IntentAgent for term matching
- `ColumnMeta.value_description` → injected for categorical columns
- `ColumnMeta.sample_values` → injected for low-cardinality columns
- `ColumnMeta.semantic_type` → used by VizAgent for chart type selection and by SQLAgent for date/currency handling
- `ColumnMeta.is_high_cardinality` → suppresses sample values; warns SQLAgent to use with care in GROUP BY
- `TableMeta.conflicts` → not injected into prompts; exposed via management API for admin review

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | Stack with UC → YAML | YAML description MUST override UC description |
| 2 | Stack with UC → YAML | Synonyms from both MUST be present in resolved `ColumnMeta.synonyms` |
| 3 | Stack with UC → YAML → RoomConfig | RoomConfig column override MUST win over YAML |
| 4 | Two providers with different descriptions | MUST record a `MetadataConflict` on `TableMeta.conflicts` |
| 5 | Provider that sets no value for a field | MUST leave earlier provider's value unchanged |
| 6 | `YAMLMetadataProvider` with missing table | MUST skip that table silently, not raise |
| 7 | `DeltaTableMetadataProvider` with synonyms field | MUST split on comma and extend the list |
| 8 | `RoomConfigMetadataProvider` | MUST always run last regardless of stack configuration |
| 9 | `MetadataFetcher` | MUST append provider name to `TableMeta.metadata_sources` for each table touched |
| 10 | `MetadataFetcher` with empty stack | MUST return TableMeta objects with only physical schema fields populated |
| 11 | `StaticMetadataProvider` | MUST be constructable with no I/O |
| 12 | Any provider | MUST use `list.extend()` for list fields, never `list =` (assignment would replace) |
