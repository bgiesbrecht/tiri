---
tags: [layer/demo, tpch]
status: stable
depends_on: [data_models, room_engine, knowledge_store, agents, feedback]
---

# Demo rooms — TPC-H

## In this system

**Linked from:** [[README]]
**Links to:** [[data_models]], [[room_engine]], [[knowledge_store]], [[agents]], [[feedback]]
**Layer:** demo

---

## What this is

Two production-quality Tiri rooms built on the TPC-H benchmark dataset. They serve three purposes simultaneously:

- **Development target** — concrete rooms to build and test against during development
- **Benchmark suite** — each room includes benchmark questions with known correct SQL, enabling automated quality measurement against ground truth
- **Capability showcase** — the questions and SQL patterns here cover the full range of Tiri's reasoning requirements: multi-table joins, window functions, conditional aggregation, CTEs, date math, and the critical distinction between gross and net revenue

TPC-H was purpose-built for decision support systems — "the queries and the data have been chosen to have broad industry-wide relevance and illustrate systems that examine large volumes of data to give answers to critical business questions." That is exactly Tiri's domain.

---

## The dataset

TPC-H models a wholesale parts supplier business. Eight tables, clean foreign key relationships, 22 canonical benchmark queries with verified correct answers. Available in Databricks as a built-in catalog — no data loading required.

```
tpch.sf1.*    ← scale factor 1 (~6M rows in lineitem) — use for development
tpch.sf10.*   ← scale factor 10 — use for performance testing
```

The eight tables split naturally across the two rooms:

```
Sales Analysis room          Supply Chain room
──────────────────           ─────────────────
customer                     supplier
orders                       partsupp
lineitem  ◄──────────────►  lineitem  (shared — different JOIN paths)
nation    ◄──────────────►  nation    (shared — different contexts)
region                       part
```

`lineitem` and `nation` appear in both rooms. This is intentional — it tests that Tiri correctly scopes answers to the room context. "Who are our top suppliers?" in the Supply Chain room should never reference the customer table. "Who are our top customers?" in the Sales Analysis room should never reference the supplier table.

---

## Room 1 — Sales Analysis

**Config file:** `tpch_sales_config.json`
**Business domain:** Customer-facing revenue, order patterns, discount analysis, geographic breakdowns
**Tables:** `customer`, `orders`, `lineitem`, `nation`, `region`
**Primary users:** Sales leadership, regional managers, finance

### What this room can answer

- Revenue by region, nation, market segment, order priority
- Top customers by revenue with geographic breakdown
- Monthly and annual revenue trends with growth rates
- Return and acceptance rates (return flag analysis)
- Late delivery rates and fulfillment performance
- Discount analysis — how much is being given away and to whom
- Unshipped order backlog and value

### The critical formula this room teaches Tiri

```sql
-- Revenue (always this, never l_extendedprice alone)
SUM(l_extendedprice * (1 - l_discount))

-- Billed amount (with tax)
SUM(l_extendedprice * (1 - l_discount) * (1 + l_tax))

-- Discount amount
SUM(l_extendedprice * l_discount)
```

This is the single most important thing for the SQL agent to learn from this room. Getting the revenue formula wrong produces plausible-looking but incorrect numbers — exactly the kind of error Tiri must never make silently.

### The JOIN path

```
region ← nation ← customer ← orders ← lineitem
```

Five hops. The full path is required for any question involving geographic breakdown of line item data. Partial joins produce incorrect results — e.g. joining lineitem directly to nation skips the customer-orders path and produces wrong groupings.

### Benchmark questions (5)

All five are in `tpch_sales_config.json` under `benchmarks`. They cover:
1. Pricing summary by return flag and line status (TPC-H Q1)
2. Top customers by revenue with nation (TPC-H Q10 variant)
3. Order volume by year and priority
4. Revenue per nation ranked within region (window function)
5. Late delivery percentage (conditional aggregation)

---

## Room 2 — Supply Chain

**Config file:** `tpch_supply_config.json`
**Business domain:** Supplier performance, part costs, inventory availability, procurement
**Tables:** `supplier`, `partsupp`, `part`, `lineitem`, `nation`, `region`
**Primary users:** Procurement, operations, supply chain managers

### What this room can answer

- Supplier ranking by inventory value, part count, account balance
- Part cost analysis — minimum cost, average cost, cost by type
- Gross margin per part (retail price minus supply cost)
- Inventory availability and utilization (available vs. shipped)
- Suppliers with negative account balances (risk indicators)
- Geographic supplier distribution and cost by nation
- Best supplier per part (lowest cost ranking)

### The critical JOIN this room teaches Tiri

```sql
-- CORRECT — always join partsupp to lineitem on BOTH keys
JOIN tpch.sf1.lineitem l
  ON l.l_partkey = ps.ps_partkey
 AND l.l_suppkey = ps.ps_suppkey

-- WRONG — joining on partkey alone produces a fan-out
JOIN tpch.sf1.lineitem l ON l.l_partkey = ps.ps_partkey
```

This is the most common SQL error in TPC-H supply chain queries. `partsupp` is a junction table with a composite key — joining on one column only multiplies rows incorrectly. The join spec in the config encodes this explicitly, and the SQL agent must respect it.

### Benchmark questions (5)

All five are in `tpch_supply_config.json` under `benchmarks`. They cover:
1. Suppliers in Europe by account balance and parts supplied (TPC-H Q2 variant)
2. Minimum supply cost per part across all suppliers
3. Distinct part count per supplier
4. Total available quantity and average cost per nation
5. Parts with more than 5 suppliers (HAVING clause)

---

## Demo files

```
demo/
├── tpch_sales_config.json    ← Sales Analysis room config
├── tpch_supply_config.json   ← Supply Chain room config
└── tpch_metadata.yaml        ← YAML metadata for all 8 TPC-H tables
```

`tpch_metadata.yaml` provides rich metadata for all eight TPC-H tables — descriptions, grain, synonyms, semantic types, value descriptions, foreign key relationships, and behavioral hints. Load it as a `YAMLMetadataProvider` to get dramatically better SQL generation without changing any room config. Add to `tiri.toml`:

```toml
[[metadata.providers.stack]]
name = "tpch_domain"
type = "yaml"
path = "./demo/tpch_metadata.yaml"
```

---

## How to load a room

```bash
# Load both demo rooms (idempotent — safe to re-run)
python -m tiri.cli load-room demo/tpch_sales_config.json
python -m tiri.cli load-room demo/tpch_supply_config.json
```

Both commands call `RoomManager.create()` or `RoomManager.update()` depending on whether the `room_id` already exists, then re-index all examples into the vector store.

---

## How to run benchmarks

```bash
python -m tiri.cli benchmark --room tpch-sales
python -m tiri.cli benchmark --room tpch-supply
```

A score of 100% means Tiri generated SQL that exactly matches the known-correct TPC-H answers (after normalization). This is the development quality gate — do not consider a feature complete if benchmark scores regress. Both rooms must score 100% before proceeding to extensions.

---

## What to try first

These questions are the best early tests of Tiri's reasoning quality — they expose the most common failure modes:

**From the Sales Analysis room:**

> "What is our total revenue by region?"

Tests: correct revenue formula, 5-table join, geographic grouping.
Failure mode: uses `l_extendedprice` without discount, or joins lineitem directly to nation.

> "How has monthly revenue trended and what is the month-over-month growth rate?"

Tests: date truncation, window function (LAG), CTE, percentage calculation.
Failure mode: missing CTE, wrong date column, LAG without ORDER BY.

> "Which nations rank highest in revenue within their region?"

Tests: RANK() OVER (PARTITION BY), 5-table join, window function.
Failure mode: uses RANK without partition, or collapses the regional grouping.

**From the Supply Chain room:**

> "For each part, which supplier offers the lowest cost?"

Tests: RANK() OVER (PARTITION BY ps_partkey), 4-table join, CTE.
Failure mode: uses MIN() without ranking, can't return the supplier name for the minimum.

> "What is the shipped quantity versus available quantity per part?"

Tests: composite key JOIN (both partkey AND suppkey), LEFT JOIN, utilization calculation.
Failure mode: joins partsupp to lineitem on one key only — produces inflated counts.

> "Which suppliers have a negative account balance?"

Tests: simple filter on s_acctbal, correct understanding of what account balance means.
Failure mode: none expected — this is a confidence baseline.

---

## Extending the demo

TPC-H has 22 canonical benchmark queries. The rooms above cover approximately 10 of them through examples and benchmarks. When Tiri's core pipeline is stable, extend coverage by adding the remaining TPC-H queries as examples and benchmarks. The full query set is available at `https://www.tpc.org/tpc_documents_current_versions/pdf/tpc-h_v2.17.1.pdf`.

The two-room structure also serves as the foundation for testing [[room_engine]]'s cross-room behavior. A future demo can add a third "federated" room that routes questions to either the Sales or Supply Chain room based on intent — the first test of Tiri's multi-room reasoning capability described in [[extensions]].

---

## Why not the solar panels room?

The `solar_panels_config.json` (one table: `bg.solar.panels`) was built to test the API surface, not to exercise Tiri's reasoning capability. A single table with no joins, no business vocabulary ambiguity, and no multi-step questions cannot reveal whether the SQL agent, context builder, or join resolution are working correctly. It remains useful for API smoke testing but should not be used as a quality benchmark.
