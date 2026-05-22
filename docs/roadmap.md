---
tags: [roadmap]
status: living
depends_on: []
---

# Roadmap — validated, not yet designed

## In this system

**Linked from:** [[README]]
**Links to:** [[extensions]], [[vision]], [[data_models]], [[metadata]], [[providers]]
**Layer:** reference

---

## What this is

Capabilities that have been validated by real customer scenarios but are not yet
designed to the implementation level required for [[extensions]]. Each entry
records the motivating use case, the gap it closes, and the design direction
agreed on. This prevents the reasoning from being lost when the time comes to
implement.

Nothing here is in scope for the current build. When a capability is ready to
design, it moves to [[extensions]] with a full interface spec and test cases.

---

## R1 — Oracle (and other external catalog) metadata

**Motivated by:** A customer running Databricks with Lakehouse Federation or
JDBC external tables over Oracle. Unity Catalog knows the physical schema of
the federated tables but not the Oracle-side semantics — column descriptions,
table purposes, foreign key relationships, view logic, and business vocabulary
that the Oracle DBA has maintained for years in the Oracle data dictionary.
Genie cannot reach this metadata. Tiri's metadata stack can.

**The gap:** The existing `MetadataProvider` interface and stack design already
support this — there is no architectural gap, only a missing implementation.
`OracleMetadataProvider` would read from Oracle's data dictionary via JDBC:

- `ALL_TAB_COMMENTS` → `TableMeta.description`
- `ALL_COL_COMMENTS` → `ColumnMeta.description`
- `ALL_CONS_COLUMNS` + `ALL_CONSTRAINTS` → `ColumnMeta.is_primary_key`,
  `is_foreign_key`, `foreign_key_table`, `foreign_key_column`
- `ALL_VIEWS.text` → parsed to populate `TableMeta.grain` and
  `TableMeta.default_filter` (the view SQL reveals what the view filters and
  how it joins)

**Design direction:**
- New `MetadataProvider` implementation: `OracleMetadataProvider`
- Configured in `tiri.toml` as `type = "oracle"` with `jdbc_url`, `jdbc_user`,
  `jdbc_pass`, `schemas: list[str]`
- Slots into the metadata stack between UC annotations and room-level YAML
  overrides — Oracle knows more than UC about Oracle objects, but the room
  author's YAML should still win
- Add `OracleMetadataProviderError` to the error hierarchy in [[providers]]
- The same pattern applies to other external catalogs: Hive Metastore, AWS
  Glue, SQL Server, PostgreSQL `information_schema`. Consider a generic
  `JdbcMetadataProvider` that parameterizes the query templates rather than
  separate classes per database.

**Blocking dependency:** None — the `MetadataProvider` interface is in place.
Can be implemented independently of any extension.

---

## R2 — Query performance tracking

**Motivated by:** The same Oracle customer wanting to understand which query
patterns are expensive on their warehouse so the planning agent can make
informed decisions — prefer approximate queries, warn about joins that fan out,
suggest pre-aggregated alternatives.

**The gap:** `ConversationTurn` stores `duration_ms` but nothing more. The
Databricks SQL Statement Execution API returns richer execution statistics
that are currently discarded.

**Design direction:**

Add `QueryPerformance` to [[data_models]]:

```python
@dataclass
class QueryPerformance:
    execution_ms: int
    bytes_scanned: int | None      # from warehouse execution stats
    rows_returned: int
    warehouse_queue_ms: int | None # time spent waiting for warehouse slot
    from_cache: bool               # whether result was served from cache
```

Add `query_performance: QueryPerformance | None = None` to `QueryResult`.
`DatabricksQueryProvider` populates it from the Statement Execution API
response. All other `QueryProvider` implementations return `None` (acceptable
— this is an optional enrichment, not a contract requirement).

**Blocking dependency:** None. Purely additive to `QueryResult` and
`ConversationTurn`. Can be implemented at Step 5 (Databricks providers)
or any time after.

---

## R3 — Supervised reasoning

**Motivated by:** Domain experts who understand the business data (Oracle DBAs,
finance analysts, compliance officers) want to inspect and guide the system's
reasoning plan before it executes — not just after. Particularly valuable when
the data has nuances the LLM may not correctly infer: complex view logic,
fiscal calendar definitions, exception populations that should be excluded.

Genie does not expose a reasoning plan for inspection — the pipeline is managed by the platform. R3 adds a human-in-the-loop checkpoint between planning and execution in Tiri.

**The gap:** EXT-1 adds multi-query reasoning (`PlanningAgent` →
`ReasoningPlan` → `[SQLAgent × N]` → `SynthesisAgent`). R3 adds a
human-in-the-loop checkpoint between planning and execution. The supervisor
sees the proposed plan, can approve it as-is, amend individual steps, add
steps the planner missed, or reject and provide guidance for replanning.

**Design direction:**

New type in [[data_models]]:

```python
@dataclass
class ReasoningCheckpoint:
    checkpoint_id: str
    conversation_id: str
    room_id: str
    question: str
    proposed_plan: ReasoningPlan
    suggested_amendments: list[str]
    # Populated from history: "last time a churn question was asked,
    # the supervisor added a cohort breakdown step"
    status: str  # "pending" | "approved" | "amended" | "rejected"
    supervisor_notes: str = ""
    amended_plan: ReasoningPlan | None = None
    created_at: str = ""
    resolved_at: str = ""
```

New `RoomConfig` field: `supervision_enabled: bool = False`

Pipeline change (EXT-1 must be in place):
```
PlanningAgent → [checkpoint if supervision_enabled] → SQLAgent × N
```

When supervision is enabled, `stream_chat()` yields a `checkpoint` SSE
event instead of proceeding to SQL execution. The conversation is paused.
A new API endpoint resolves it:

```
POST /rooms/{id}/conversations/{cid}/checkpoints/{checkpoint_id}/resolve
body: {"action": "approve" | "amend" | "reject",
       "amended_plan": ReasoningPlan | null,
       "notes": "..."}
```

**Suggested amendments** (the history-awareness part): when the checkpoint
is created, the system scans prior `ReasoningCheckpoint` records for the same
room where `status == "amended"` and the question vector is similar. The
amendments made by supervisors in those cases are surfaced as suggestions —
"in 3 prior similar questions, supervisors added a step to break down by
customer segment." This is the lightweight learning loop that makes supervision
progressively less work as the system builds a history.

**Blocking dependency:** EXT-1 (needs `ReasoningPlan`), SSE streaming
infrastructure (Step 9/10), and R2 is a natural companion (performance data
informs which plans to flag for supervision).

---

## R4 — Performance-guided planning

**Motivated by:** Same customer as R2/R3. As query history accumulates,
the `PlanningAgent` should be able to factor in "this join pattern has
historically taken 45+ seconds on this warehouse" when deciding step order,
whether to suggest approximate alternatives, or whether to warn the user
about expected latency.

**Design direction:**

The `PlanningAgent` (EXT-1) receives a `performance_context: str` field in
its prompt — a brief summary of relevant historical performance patterns for
this room, derived from prior `ConversationTurn` records with `QueryPerformance`
data attached.

The `Proposer` (in [[feedback]]) is extended to consider performance signals
alongside feedback signals when nominating examples: queries that executed
quickly, returned actionable results, and were not amended by supervisors are
stronger candidates than queries that only received a thumbs-up.

No new types needed beyond R2's `QueryPerformance`.

**Blocking dependency:** R2 (needs performance data), EXT-1 (needs
`PlanningAgent`).

---

## R5 — Migration tooling: Genie Space → Tiri room

**Motivated by:** Any customer with existing Genie Spaces who wants to adopt
Tiri without re-authoring their room configurations from scratch.

**The gap:** The translation from Genie wire format to `RoomConfig` is
mechanical and well-understood (documented in [[concept_map]]). It should
be a first-class CLI command rather than a one-off script.

**Design direction:**

New CLI command:

```bash
python -m tiri.cli import-genie --space-id <id> --output <path/to/config.json>
```

Reads the Genie Space via `GET /api/2.0/genie/spaces/{id}?include_serialized_space=true`,
translates to `RoomConfig` JSON:

| Genie field | RoomConfig field | Translation |
|---|---|---|
| `instructions.text_instructions[0].content` | `text_instruction` | Unwrap list |
| `instructions.example_question_sqls` | `examples` | Unwrap list fields |
| `instructions.join_specs` | `joins` | Flatten `left.identifier`→`left_table`, strip `FROM_RELATIONSHIP_TYPE_` prefix |
| `instructions.sql_snippets.filters` | `sql_filters` | Add `kind="filter"`, unwrap list fields |
| `instructions.sql_snippets.expressions` | `sql_expressions` | Add `kind="expression"`, unwrap list fields |
| `config.sample_questions` | `sample_questions` | Unwrap list |
| `data_sources[].table_ref` | `tables` | Extract FQN |

Writes the output JSON to `--output` path. The user reviews it, adds
`warehouse_id`, optionally enriches with metrics and column overrides, then
runs `python -m tiri.cli load-room`.

**Blocking dependency:** None beyond the CLI infrastructure (Step 3 config.py
is already done). Could be implemented immediately.

---

---

## R6 — Per-user inference audit trail

**Motivated by:** EXT-6 ensures SQL executes with the user's own credentials,
so Unity Catalog enforces data access correctly. But LLM calls — intent
classification, SQL generation, synthesis — currently always use the service
credential. Organizations with strict audit requirements may need to know not
just what data was accessed per user, but what LLM calls were made on their
behalf.

**The gap:** `auth.py` extracts the user's Bearer token and passes it to
`QueryProvider`. It does not pass it to `LLMProvider`. The LLM calls carry
no per-user identity signal.

**Pattern observed in the ecosystem:** The `x-forwarded-access-token` header
injected by Databricks Apps — the same header now supported by Tiri's `auth.py`
— can be used to obtain a `WorkspaceClient` authenticated as the calling user,
which can then be used to instantiate a `ChatDatabricks` model endpoint call
carrying that user's identity. This pattern exists in at least one community
implementation and is consistent with how other Databricks services handle
per-request identity in Apps deployments.

**Design direction:**

Add `user_token: str | None = None` to `LLMProvider.complete()` and
`LLMProvider.stream()` — same pattern as `QueryProvider.execute()`. When
provided, `DatabricksLLMProvider` uses it to authenticate the Model Serving
call rather than the service credential. The token is forwarded from
`RoomEngine.chat()` alongside the existing `user_token` for SQL execution.

`RouterLLMProvider` passes `user_token` through to the backend's
`complete()`/`stream()` calls. Single-backend implementations that don't
support per-user inference (OpenAI, Anthropic, Ollama) accept and ignore
the parameter — same pattern as the `task=` parameter.

This is additive. No existing behavior changes when `user_token=None`.

**What it enables:**
- Databricks Model Serving audit logs show the calling user's identity,
  not the service principal
- Consistent identity across data access and inference — the same user
  credential used for SQL is also used for LLM calls
- Required for deployments where compliance teams audit all LLM activity
  per user

**Blocking dependency:** None architecturally. Requires Databricks Model
Serving to support per-request user token auth on the endpoint (available
in Databricks Apps deployments via the forwarded token). Not all Model
Serving configurations support this — verify before implementing.

---

## R11 — SKOS vocabulary support

**Motivated by:** Room authors currently express domain terminology in free-text `text_instruction` and flat `column_overrides` synonym lists. This works but doesn't scale — synonyms are scattered, term hierarchy is implicit, and there is no machine-readable way to express that "top line" means "revenue" which means `SUM(l_extendedprice * (1 - l_discount))`. When an organization already maintains a controlled vocabulary (in Collibra, Purview, a data catalog, or a simple SKOS Turtle file), Tiri should be able to consume it directly rather than requiring the room author to re-express it.

**The gap:** Tiri has no concept of a formal vocabulary. Term resolution happens via LLM — the model infers from `text_instruction` and examples that "top line" might mean revenue. This works until it doesn't. SKOS (W3C Simple Knowledge Organization System) is the standard for controlled vocabularies: preferred labels, alternate labels, scope notes, concept hierarchies, and relationships. It is exactly what `text_instruction` synonym lists are trying to be informally.

**What SKOS provides for Tiri:**

- `skos:prefLabel` — the canonical term the model should use in SQL
- `skos:altLabel` — synonyms: "top line", "sales", "income" → all resolve to "revenue"
- `skos:definition` — precise definition injected into the SQL generation prompt
- `skos:scopeNote` — "don't do this" notes: "Never use o_totalprice — it is pre-discount"
- `skos:narrower` / `skos:broader` — concept hierarchy: "gross revenue" is narrower than "revenue"
- `skos:exactMatch` — cross-vocabulary alignment: your "revenue" is the same as the dbt metric "net_revenue"

**Design direction:**

New `RoomConfig` field:
```python
vocabulary_uri: str = ""
# Path to a SKOS Turtle file or SPARQL endpoint.
# e.g. "./metadata/domain_vocabulary.ttl"
# e.g. "https://sparql.mycompany.com/vocabulary"
```

New component: `SkosTermResolver` in `tiri/knowledge/`:
```python
class SkosTermResolver:
    def __init__(self, graph: rdflib.Graph): ...

    def resolve(self, term: str) -> TermResolution | None:
        """
        SPARQL query against the SKOS graph.
        Matches term against skos:prefLabel and skos:altLabel.
        Returns: canonical label, SQL expression (if defined),
        definition, scope notes, and related concepts.
        Returns None if term not found in vocabulary.
        """

@dataclass
class TermResolution:
    canonical_label: str          # skos:prefLabel
    sql_expression: str | None    # tiri:sqlExpression (Tiri extension property)
    definition: str               # skos:definition
    scope_notes: list[str]        # skos:scopeNote — constraints and "don't do this" notes
    broader: list[str]            # parent concepts
    narrower: list[str]           # child concepts
```

`ContextBuilder` calls `SkosTermResolver.resolve()` for each noun phrase in the question before passing to `IntentAgent`. Resolutions are injected into `ContextPackage.mcp_context` (reusing the existing injection mechanism) as structured term definitions the agents can use.

Sample vocabulary (Turtle format):
```turtle
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix tiri: <https://tiri.databricks.com/vocab#> .

tiri:revenue a skos:Concept ;
    skos:prefLabel "revenue" ;
    skos:altLabel "top line", "sales", "income", "net revenue" ;
    skos:definition "Net revenue after discounts" ;
    skos:scopeNote "Use lineitem table only. Never use orders.o_totalprice — it is pre-discount." ;
    tiri:sqlExpression "SUM(l_extendedprice * (1 - l_discount))" ;
    skos:narrower tiri:grossRevenue ;
    skos:broader tiri:financialMetric .

tiri:supplier a skos:Concept ;
    skos:prefLabel "supplier" ;
    skos:altLabel "vendor", "partner", "source" ;
    skos:definition "An organization that supplies parts to customers" .
```

Room authors who already maintain a SKOS vocabulary (or whose organization uses Collibra, Purview, or a similar catalog with SKOS export) can point `vocabulary_uri` at it and get term resolution for free. Authors who don't have one can build a simple Turtle file alongside their room config.

**Dependency:** `rdflib` — pure Python, no external services required for file-based vocabularies. SPARQL endpoint support requires `rdflib-endpoint` or direct HTTP queries.

**Blocking dependency:** None. Purely additive. `SkosTermResolver` is an optional component — rooms without `vocabulary_uri` are completely unaffected.

---

## R12 — OntologyMetadataProvider

**Motivated by:** Enterprise data catalogs (Collibra, Microsoft Purview, Apache Atlas) and semantic layer tools (dbt semantic layer, UC metric views) maintain rich OWL/RDF graphs describing table semantics, data quality, lineage, and business concept definitions. Tiri should be able to consume these graphs as a `MetadataProvider` — turning existing governance investment directly into room quality without requiring room authors to re-express what the catalog already knows.

**The gap:** The existing `MetadataProvider` implementations read from UC annotations, YAML files, Delta tables, and dbt manifests. None of them speak RDF/OWL — the standard format that formal semantic catalogs use. An `OntologyMetadataProvider` fills this gap.

**What OWL/RDF provides beyond SKOS:**

- Class hierarchies and property restrictions — "a customer is a type of party, a party has exactly one country"
- `owl:equivalentClass` / `owl:sameAs` — formal equivalence between your terms and external ontologies
- Data quality assertions — "this table is complete", "this column is always non-null"
- Provenance — where the data came from, who is responsible for it
- Inferred join paths — `rdfs:domain` and `rdfs:range` on properties describe which tables are related and how

**Design direction:**

New `MetadataProvider` implementation:

```python
class OntologyMetadataProvider(MetadataProvider):
    """
    Reads an OWL/RDFS/RDF graph and produces TableMeta enrichments.
    Supports local Turtle files, remote SPARQL endpoints, and
    Collibra/Purview export formats.
    """

    def __init__(
        self,
        name: str,
        graph: rdflib.Graph | None = None,
        sparql_endpoint: str | None = None,
        namespace: str = "https://tiri.databricks.com/vocab#",
    ): ...

    async def enrich(
        self,
        tables: dict[str, TableMeta],
        room_config: RoomConfig,
    ) -> None:
        """
        SPARQL queries derive:
        - TableMeta.description from rdfs:comment or skos:definition
        - TableMeta.grain from tiri:grain annotation
        - ColumnMeta.description from rdfs:comment on the property
        - ColumnMeta.synonyms from skos:altLabel on the property
        - ColumnMeta.semantic_type from tiri:semanticType annotation
        - TableMeta.recommended_joins from rdfs:domain/rdfs:range relationships
        - TableMeta.data_quality from tiri:dataQuality assertions
          (used by SynthesisAgent for confidence calibration)
        """
```

**Confidence calibration from data quality assertions:**

OWL can express data quality in a way `SynthesisAgent` can use directly:

```turtle
tpch:lineitem tiri:dataQuality tiri:Complete .
tpch:lineitem tiri:dataQuality tiri:CurrentAsOf "daily" .

tpch:forecast tiri:dataQuality tiri:PartiallyStale ;
    tiri:stalenessNote "Updated monthly. May lag reality by up to 4 weeks." .
```

A new field on `TableMeta`:
```python
@dataclass
class TableMeta:
    # ... existing fields ...
    data_quality: str = ""         # "complete", "partially_stale", "external"
    data_quality_note: str = ""    # injected into SynthesisAgent prompt
```

`SynthesisAgent` uses these to calibrate confidence assignment — a result involving a `partially_stale` table is automatically `medium` or `low`, not because of a heuristic, but because the ontology asserts it.

**Join path inference:**

With OWL property domain/range declarations, multi-hop join paths can be inferred rather than enumerated:

```turtle
tiri:suppliedBy rdfs:domain tpch:partsupp ;
               rdfs:range  tpch:supplier .
tiri:locatedIn  rdfs:domain tpch:supplier ;
               rdfs:range  tpch:nation .
tiri:regionOf   rdfs:domain tpch:nation ;
               rdfs:range  tpch:region .
```

A SPARQL property path query (`tiri:suppliedBy/tiri:locatedIn/tiri:regionOf`) finds the route from `partsupp` to `region` without requiring the room author to declare every hop in `RoomConfig.joins`. This is the long-term resolution of the join enumeration burden — the ontology knows the graph, Tiri traverses it.

**Configuration in `tiri.toml`:**
```toml
[[metadata.providers.stack]]
name     = "enterprise_ontology"
type     = "ontology"
source   = "./metadata/enterprise.ttl"    # Turtle file
# or:
source   = "https://sparql.mycompany.com" # SPARQL endpoint
namespace = "https://data.mycompany.com/ontology#"
```

**Interoperability targets:**

| Catalog | Export format | Integration path |
|---|---|---|
| Collibra | RDF/OWL export | Load Turtle file into `OntologyMetadataProvider` |
| Microsoft Purview | Apache Atlas REST API (RDF-compatible) | HTTP fetch → parse |
| Apache Atlas | RDF export | Load Turtle file |
| dbt semantic layer | dbt manifest JSON (not RDF) | Existing `DbtMetadataProvider` |
| UC metric views | SQL / REST API | Planned `UCMetricViewProvider` |

**Blocking dependency:** R11 (SKOS support) is a natural predecessor — the SKOS vocabulary and the OWL ontology often live in the same graph. Both use `rdflib`. Implementing R11 first means `OntologyMetadataProvider` can reuse the SPARQL infrastructure.

**Dependency:** `rdflib` — same as R11.

---

## Design principles for this list

When a roadmap item is ready to graduate to [[extensions]]:

1. It must have a complete interface spec — every new type, every method
   signature, every new field on existing types
2. It must have test cases written before the implementation (the MUST/SHOULD
   table format from other extension docs)
3. Its blocking dependencies must be resolved or explicitly scheduled
4. It must have a clear answer to: "what does a room author configure to enable
   this, and what do they see differently in responses?"

Items that cannot answer question 4 are not ready to graduate.
