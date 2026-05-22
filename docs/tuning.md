---
tags: [layer/reference]
status: stable
depends_on: [room_engine, agents, feedback, configuration, data_models]
---

# Room tuning guide

## In this system

**Linked from:** [[README]], [[demo]], [[feedback]]
**Links to:** [[agents]], [[data_models]], [[configuration]], [[feedback]], [[extensions]]
**Layer:** reference

---

## What this is

How to improve a room's benchmark score and answer quality after initial configuration. Most rooms start at 60–80% on their benchmarks. Getting to 90–100% is an iteration process — diagnose which agent is failing, apply the right fix, re-benchmark, repeat.

The loop is fast. A room with 5 benchmarks takes under a minute to re-evaluate:

```bash
# Edit the room config
vim demo/my_room_config.json

# Reload (idempotent — safe to run repeatedly)
python -m tiri.cli load-room demo/my_room_config.json

# Re-benchmark
python -m tiri.cli benchmark --room my-room
```

---

## Step 1 — Diagnose where the failure is

Run the benchmark with full output and categorize each failing question by which agent produced the failure. The `ConversationTurn` shape tells you exactly where it broke:

| Field state | What failed | Where to look |
|---|---|---|
| `turn.sql` is empty | `IntentAgent` routed to `ClarifyAgent` | The model thought the question was ambiguous |
| `turn.error` is set | `SQLAgent` exhausted retries | The model couldn't produce valid SQL |
| `turn.query_result` row count wrong | SQL ran but returned wrong results | The model misunderstood the question semantics |
| `turn.synthesized_answer.confidence` unexpectedly low | `SynthesisAgent` is uncertain about a clear question | Context is missing or ambiguous |

Each failure mode has a different fix. Don't guess — read the field.

---

## Fix A — IntentAgent routes to ClarifyAgent instead of producing SQL

The model interpreted the question as ambiguous when it's not. This is the most common failure after switching LLM models, because different models have different sensitivity to ambiguity. See [[configuration]] — *Room calibration and model switching*.

**Fix A1 — Add a worked example (most targeted)**

Add the failing question with its correct SQL to `RoomConfig.examples`. `ExampleIndexer` will retrieve it when similar questions are asked. Seeing a similar question answered with SQL tells `IntentAgent` "this type of question is answerable — don't clarify."

```json
{
  "question": "Which suppliers in Europe have the highest account balance?",
  "sql": "SELECT s_name, s_acctbal FROM supplier JOIN nation ON s_nationkey = n_nationkey JOIN region ON n_regionkey = r_regionkey WHERE r_name = 'EUROPE' ORDER BY s_acctbal DESC LIMIT 10",
  "notes": "Europe region filter via nation → region join"
}
```

**Fix A2 — Tighten `text_instruction`**

Add a sentence that tells the model which terms are unambiguous in this domain:

```json
{
  "text_instruction": "When a question references a known metric or standard business term for this domain (account balance, supply cost, order priority), treat it as answerable. Do not ask for clarification on standard terminology."
}
```

**Fix A3 — Adjust `TIRI_INTENT_THRESHOLD` (last resort)**

Lower from 0.7 to e.g. 0.6 to make the model more willing to attempt SQL on borderline questions. Do this last — it affects all rooms and all questions globally. A threshold change that fixes one room may introduce regressions in another.

---

## Fix B — SQLAgent produces wrong SQL

The model understood the question (IntentAgent routed correctly) but the generated SQL is wrong.

**Fix B1 — Define ambiguous metrics as SQL snippets**

If a question contains a term the model interprets differently each time, define it explicitly as a `sql_measure` in `RoomConfig`:

```json
{
  "sql_measures": [
    {
      "display_name": "parts supplied",
      "sql": "COUNT(ps_partkey)",
      "description": "Number of distinct parts a supplier stocks"
    },
    {
      "display_name": "minimum supply cost",
      "sql": "MIN(ps_supplycost)",
      "description": "Lowest supply cost across all suppliers for a given part"
    }
  ]
}
```

`SQLAgent` sees these snippets as named, defined expressions. *"Suppliers who supply the most parts"* becomes unambiguous — it means `COUNT(ps_partkey)`, not `SUM(ps_availqty)` or `MAX(ps_supplycost)`.

**Fix B2 — Add or improve join specs**

If the SQL agent is guessing how tables relate, define the relationship explicitly:

```json
{
  "joins": [
    {
      "left_table": "supplier",
      "right_table": "nation",
      "join_sql": "supplier.s_nationkey = nation.n_nationkey",
      "relationship": "MANY_TO_ONE"
    },
    {
      "left_table": "nation",
      "right_table": "region",
      "join_sql": "nation.n_regionkey = region.r_regionkey",
      "relationship": "MANY_TO_ONE"
    }
  ]
}
```

The agent follows explicit join specs rather than inferring from column names. Multi-hop joins (supplier → nation → region) are especially important to declare.

**Fix B3 — Add a scope filter**

If questions about a specific domain should always filter to a subset, add a `sql_filter`:

```json
{
  "sql_filters": [
    {
      "display_name": "active parts only",
      "sql": "p_type NOT LIKE '%DISCONTINUED%'",
      "description": "Exclude discontinued parts from all queries"
    }
  ]
}
```

Or add to `RoomConfig.default_filters` for filters that apply to every query unconditionally.

**Fix B4 — Add a worked example**

The most direct fix for a specific question pattern. Add the question with its correct SQL. `SQLAgent` uses the retrieved examples as few-shot demonstrations:

```json
{
  "examples": [
    {
      "question": "For each part, what is the minimum supply cost?",
      "sql": "SELECT p_partkey, p_name, MIN(ps_supplycost) AS min_supply_cost FROM part JOIN partsupp ON part.p_partkey = partsupp.ps_partkey GROUP BY p_partkey, p_name ORDER BY min_supply_cost LIMIT 20",
      "notes": "Limit 20 — large result sets should be capped unless all rows explicitly requested"
    }
  ]
}
```

---

## Fix C — Correct SQL, wrong row count

The SQL is structurally valid and runs without error, but the row count doesn't match the benchmark expectation. This usually means the question has an implicit constraint the model isn't applying.

**Fix C1 — Describe implicit conventions in `text_instruction`**

Make implicit rules explicit:

```json
{
  "text_instruction": "When a question asks for a ranking or comparison without specifying a row limit, return the top 20 results. When a question asks for multi-criterion ranking (e.g. 'highest X and most Y'), rank by the first criterion and break ties with the second."
}
```

**Fix C2 — Add a worked example showing the convention**

A description in `text_instruction` sets the rule. An example in `examples` demonstrates it. Both together are more reliable than either alone.

---

## Fix D — SynthesisAgent confidence unexpectedly low

`SynthesizedAnswer.confidence` is `"low"` or `"medium"` for a question that should have a clean, high-confidence answer.

**Fix D1 — Add column semantic types via metadata**

If columns lack semantic type annotations, `SynthesisAgent` can't distinguish dates from IDs from measures. Add a `metadata_yaml` provider with semantic types:

```yaml
tables:
  - name: orders
    columns:
      - name: o_orderdate
        semantic_type: date
      - name: o_totalprice
        semantic_type: currency
      - name: o_orderstatus
        semantic_type: category
```

**Fix D2 — Define metrics explicitly**

If the question asks about a named business concept, define it in `RoomConfig.metrics`:

```json
{
  "metrics": [
    {
      "name": "revenue",
      "sql": "SUM(l_extendedprice * (1 - l_discount))",
      "description": "Net revenue after discounts",
      "dimensions": ["l_shipdate", "o_orderpriority"]
    }
  ]
}
```

`SynthesisAgent` receives metric definitions as context. A question about "revenue" resolves to a precisely defined expression rather than an inferred column.

---

## The compound ranking problem

One failure pattern deserves special attention because it appears across multiple models and room types: **compound ranking questions** — *"Which X has the highest A and the most B?"*

This is genuinely ambiguous. It could mean:
- Rank by A, break ties with B
- Find the Pareto frontier (highest on both simultaneously)
- Score = A × B (composite metric)
- Rank by A, filter to top 10, then sort by B

No model resolves this correctly without guidance. The fix is always a combination of `text_instruction` (state the convention) and `examples` (demonstrate it):

```json
{
  "text_instruction": "When a question ranks by two criteria simultaneously (e.g. 'highest X and most Y'), rank by the first criterion. Use the second criterion as a tiebreaker.",
  "examples": [
    {
      "question": "Which suppliers have the highest account balance and supply the most parts?",
      "sql": "SELECT s_name, s_acctbal, COUNT(ps_partkey) AS parts_supplied FROM supplier JOIN partsupp ON supplier.s_suppkey = partsupp.ps_suppkey GROUP BY s_name, s_acctbal ORDER BY s_acctbal DESC, parts_supplied DESC LIMIT 10",
      "notes": "Compound ranking: primary sort acctbal DESC, secondary sort parts_supplied DESC"
    }
  ]
}
```

---

## Model switching and recalibration

A room's `text_instruction` and examples are calibrated against the model configured when the room was authored. Switching models is valid but may require re-tuning. See [[configuration]] — *Room calibration and model switching* for the full discussion.

**Practical rule:** after switching models, re-run benchmarks before deploying. If the score drops:
1. Check IntentAgent failures first — is the new model routing more questions to ClarifyAgent?
2. Check SQLAgent failures second — is the new model wrapping SQL in markdown fences or using different function syntax?
3. Add examples targeting the failure pattern before adjusting `TIRI_INTENT_THRESHOLD` globally.

---

## Using the feedback loop

The [[feedback]] system surfaces suggested improvements automatically. `FeedbackProposer` analyzes low-confidence turns and failed benchmarks and proposes additions to `text_instruction`, `examples`, and `sql_measures`.

Run the proposer after a benchmark session:

```bash
python -m tiri.cli feedback propose --room my-room
```

Review the proposals — they are suggestions, not automatic updates. Apply the ones that make sense, reload the room, re-benchmark.

---

## Benchmark score vs answer quality

A benchmark score and answer quality are not the same thing. This distinction matters for how you interpret results and decide what to fix.

**A pipeline failure** — `turn.error` is set or `turn.sql` is empty. The user received nothing. The pipeline broke. This is always worth fixing.

**A benchmark mismatch** — SQL ran, results came back, the user received a valid answer. The benchmark scored it as a failure because the row count didn't match `expected_row_count`. The user's experience was fine; the benchmark's expectation was specific.

These require completely different responses:

| Failure type | `turn.error` / `turn.sql` | User experience | Fix |
|---|---|---|---|
| Pipeline failure | Error set or SQL empty | User got nothing | Fix the agent or room config |
| Benchmark mismatch | Result returned | User got a valid answer | Fix the benchmark question or expectation |

The two irreducible failures in the TPC-H supply room are both benchmark mismatches, not pipeline failures:

- *"Which suppliers in Europe have the highest account balance and supply the most parts?"* — every tested model (Databricks llama-3-3-70b, Ollama qwen2.5-coder, Anthropic Sonnet 4.6, Claude Opus 4.7) returned a ranked list of European suppliers with account balances and parts counts. The user received a useful, valid answer. The benchmark failed because the sort order differed from the expected SQL.

- *"For each part, what is the minimum supply cost across all suppliers?"* — every tested model returned minimum supply cost per part. The user received a correct answer. The benchmark failed because the model returned all ~200,000 parts while the expected SQL caps at 20 — a constraint that does not appear in the question.

In both cases the pipeline is working. The benchmark questions are underspecified. A 3/5 where two questions produce mismatched-but-valid results is fundamentally different from a 3/5 where two questions return errors.

**How to tell the difference when reading benchmark output:**

Look at the ConversationTurn for each failing benchmark:
- `turn.error` set → pipeline failure → fix the room or engine
- `turn.sql` empty → IntentAgent failure → fix A (see above)
- `turn.query_result` present but row count wrong → benchmark mismatch → evaluate whether the answer is actually wrong, or whether the benchmark expectation is too specific

**When a benchmark mismatch is still worth fixing:**

If the model's answer is a valid interpretation but not the one your users need, fix the question to remove the ambiguity — then the model will reliably produce the interpretation you want. Don't lower `TIRI_INTENT_THRESHOLD` or add workarounds for what is really a question authoring issue.

---

## Benchmark design notes

The benchmark is only as good as its `expected_sql`. Two common benchmark authoring mistakes:

**Implicit constraints in the expected SQL that aren't in the question.** If `expected_sql` has `LIMIT 20` but the question says nothing about a limit, the benchmark will fail on any answer that returns the correct rows without the limit. Fix: either add the constraint to the question text, or add it to `text_instruction`, or accept that this is a benchmark design issue and not a model failure.

**Expected SQL that reflects one valid interpretation of an ambiguous question.** If the question is genuinely ambiguous, the benchmark tests whether the model picked the same interpretation as the benchmark author — not whether it answered correctly. Fix: rewrite the question to remove the ambiguity, or add a worked example showing the expected interpretation.

The two irreducible failures in the TPC-H supply room (`5e757fe8` and `ff04dceff`) are both of this type. The questions are ambiguous; the benchmark reflects one valid interpretation; every tested model (Databricks llama, Ollama qwen, Anthropic Sonnet 4.6, Claude Opus 4.7) picks a different but defensible interpretation. The fix is room configuration, not engine changes.

---

## Quick reference — which fix for which failure

| Failure | First fix to try |
|---|---|
| IntentAgent routes to ClarifyAgent | Add a worked example for the failing question |
| SQLAgent produces wrong metric | Add a `sql_measure` snippet defining it precisely |
| SQLAgent misses a join | Add an explicit `JoinSpec` |
| SQLAgent ignores a scope constraint | Add a `sql_filter` or `default_filters` entry |
| Wrong row count (implicit limit) | Add the convention to `text_instruction` + worked example |
| Low synthesis confidence | Add column `semantic_type` annotations via metadata YAML |
| Compound ranking wrong | Add convention to `text_instruction` + worked example showing both sort keys |
| After model switch: more ClarifyAgent routing | Add worked examples; adjust `TIRI_INTENT_THRESHOLD` last |
