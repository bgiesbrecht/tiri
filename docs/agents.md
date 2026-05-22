---
tags: [layer/intelligence]
status: stable
depends_on: [providers, data_models, knowledge_store, metadata]
---

# Agents

## In this system

**Linked from:** [[README]], [[room_engine]]
**Links to:** [[providers]], [[data_models]], [[knowledge_store]], [[metadata]]
**Layer:** intelligence

---

## What this is

Seven agents that form the reasoning core of the system. Each takes a `ContextPackage` (assembled by [[knowledge_store]]) and produces a typed result. They are called in sequence by [[room_engine]].

The full pipeline (when all extensions are active):

```
question + ContextPackage
        │
        ▼
  IntentAgent (task="intent")
  ├── intent: sql_query
  │     ↓
  │   PlanningAgent (task="planning")         ← EXT-1
  │     ↓ ReasoningPlan (1–5 steps)
  │   SQLAgent × N steps (task="sql")
  │     ↓ QueryResult × N
  │   SynthesisAgent (task="synthesis")       ← EXT-1 / EXT-7
  │     ↓ SynthesizedAnswer
  │   HypothesisAgent (task="synthesis")      ← EXT-11 (hypothesis_mode_enabled only)
  │     ↓ HypothesisResult (optional)
  │   VizAgent (task="viz_summary")
  │
  ├── intent: clarify_needed  → ClarifyAgent (task="clarify")
  └── intent: out_of_scope    → error response
```

**Key rule (from [[README]]):** agents import only from [[providers]] and [[data_models]]. No SDK imports, no HTTP calls, no file I/O. All I/O goes through provider interfaces.

**Prompts are files.** All prompt templates live in `engine/prompt_templates/`. Agents load them at startup. Never construct prompts with f-strings inline in agent code.

---

## IntentAgent

### Responsibility

Classify the user's question and identify which tables and snippets are relevant. Routes the pipeline to `SQLAgent` or `ClarifyAgent`. Acts as a filter — catches out-of-scope questions before wasting a SQL generation call.

### Result type

`IntentResult` is defined in [[data_models]]. Fields: `intent`, `relevant_tables`, `relevant_snippets`, `confidence`, `reasoning`, `table_selection_method`.

### Interface

```python
class IntentAgent:
    def __init__(self, llm: LLMProvider, confidence_threshold: float = 0.7): ...

    async def run(
        self,
        question: str,
        context: ContextPackage,
    ) -> IntentResult:
```

### Prompt template: `intent_classification.txt`

```
You are classifying a user question for a data analytics assistant.

## Available tables
{table_list}
-- Format per table:
-- catalog.schema.table — {description}
--   Domain: {domain}
--   Synonyms: {synonyms}  (user may refer to this table by any of these names)

## Available SQL expressions
{snippet_list}
-- Format: {display_name} ({kind}): {instruction}
--   Synonyms: {synonyms}

## Named business metrics
{metric_list}
-- Format: {display_name} ({name}) — {description}
--   Synonyms: {synonyms}
-- (compact form for intent routing — full definitions are in the SQL generation prompt)

## Instructions
{text_instruction}

## Question
{question}

Respond with a JSON object only. No markdown fences.
{
  "intent": "sql_query" | "clarify_needed" | "out_of_scope",
  "relevant_tables": ["catalog.schema.table", ...],
  "relevant_snippets": ["display_name", ...],
  "confidence": 0.0–1.0,
  "reasoning": "one sentence"
}

intent is "out_of_scope" if the question cannot be answered using the available tables.
intent is "clarify_needed" if the question is ambiguous and cannot be resolved from context.
intent is "sql_query" for all other questions about the data.

When matching user language to tables, consider table descriptions, domain labels, and synonyms.
A user who says "purchases" may mean the orders table. A user who says "vendors" may mean the supplier table.
```

### Routing rules

- `confidence >= threshold` AND `intent == "sql_query"` → route to `SQLAgent`
- `confidence < threshold` OR `intent == "clarify_needed"` → route to `ClarifyAgent`
- `intent == "out_of_scope"` → return error turn immediately, no further agents called

### Snippet resolution

The LLM returns `relevant_snippets` as a list of `display_name` strings. The agent resolves these to `SqlSnippet` objects before populating `IntentResult`:

```python
snippet_map = {s.display_name: s for s in context.sql_snippets}
relevant_snippets = [
    snippet_map[name]
    for name in raw_response.get("relevant_snippets", [])
    if name in snippet_map
]
# Log a WARNING for any name not found in snippet_map — do not raise
```

Unknown display names are silently dropped with a warning. This handles LLM hallucination of snippet names gracefully.

---

## SQLAgent

### Responsibility

Generate valid, correct SQL for the user's question. Self-corrects using `QueryProvider.validate()` before returning. The most critical agent — quality here directly determines answer quality.

### Result type

`SQLResult` is defined in [[data_models]]. Fields: `sql`, `explanation`, `is_valid`, `error`, `attempts`.

### Interface

```python
class SQLAgent:
    def __init__(self, llm: LLMProvider, query: QueryProvider, max_retries: int = 3): ...

    async def run(
        self,
        question: str,
        context: ContextPackage,
        intent: IntentResult,
    ) -> SQLResult:
```

### Self-correction loop

The `load_template()`, `format_schemas()`, `format_joins()`, `format_snippets()`, `format_examples()`, and `format_history()` calls below are internal helpers defined in `sql_agent.py` — not part of any public interface.

```
# Build initial messages list from the prompt template + context
messages = [
    {"role": "system", "content": load_template("sql_generation.txt").format(
        text_instruction=context.text_instruction,
        table_schemas=format_schemas(intent.relevant_tables, context),
        join_specs=format_joins(context.joins),
        sql_snippets=format_snippets(intent.relevant_snippets),
        examples=format_examples(context.retrieved_examples),
        history=format_history(context.conversation_history),
        question=question,
    )}
]

attempt = 1
while attempt <= max_retries:
    response = await llm.complete(messages, task="sql")
    sql = response.content.strip()
    if sql.startswith("CANNOT_ANSWER:"):
        return SQLResult(is_valid=False, error=sql, attempts=attempt)
    is_valid, error = await query.validate(sql)
    if is_valid:
        return SQLResult(sql=sql, explanation="", is_valid=True, attempts=attempt)
    # Append failed attempt + error to message history and retry
    messages.append({"role": "assistant", "content": sql})
    messages.append({"role": "user", "content": f"That SQL has an error: {error}\nPlease fix it and return only the corrected SQL."})
    attempt += 1
return SQLResult(is_valid=False, error=f"Failed after {max_retries} attempts", attempts=max_retries)
```

### Prompt template: `sql_generation.txt`

```
You are a SQL expert. Generate a single SQL query that answers the user's question.
Return ONLY the SQL. No markdown fences. No explanation.
If the question cannot be answered, respond with: CANNOT_ANSWER: <one-sentence reason>

## Instructions
{text_instruction}

## Mandatory filters (MUST appear in every query — not optional)
{default_filters}
-- These filters apply to every query in this room regardless of tables used.
-- If this section is empty, no mandatory filters apply.

## Available tables (use only these, fully-qualified names)
{table_schemas}
-- Format per table:
-- catalog.schema.table — {description}
--   Grain: {grain}
--   Default date column: {default_date_column}
--   Default filter: {default_filter}  (apply unless user says otherwise)
--   Columns:
--     column_name  TYPE  [{semantic_type}]  — {description}
--       Synonyms: {synonyms}
--       Values: {value_description}
--       Sample values: {sample_values}
--       HIGH CARDINALITY — avoid in GROUP BY without filters
--       (high_cardinality flag suppresses sample values and adds the warning above)

## Join relationships
{join_specs}
-- Format: {left_alias}.{left_table} JOIN {right_alias}.{right_table} ON {join_on}
--   Relationship: {relationship_type}
--   Note: {instruction}

## Reusable SQL expressions (use when relevant)
{sql_snippets}
-- Format: {display_name} ({kind}): {sql}  -- {instruction}
--   Synonyms: {synonyms}

## Named business metrics (use these definitions — do not redefine)
{metrics}
-- Format: {display_name} ({name})
--   Definition: {sql}
--   Grain: {grain}
--   Description: {description}
--   Synonyms: {synonyms}
--   Valid dimensions: {dimensions}
--   Always-apply filters: {filters}
--   Unit: {unit}
-- When the user asks for a metric by name or synonym, use the definition
-- above exactly. Do not substitute your own SQL for a named metric.

## Worked examples (most similar to this question)
{examples}
-- Format:
-- Q: {question}
-- SQL: {sql}

## Conversation history
{history}

## Question
{question}
```

**Prompt construction rules:**
- Include `grain` for every table — it is the most important context for correct aggregation
- Include `default_filter` for every table that has one — apply it unless the user explicitly asks for all rows
- Include `RoomConfig.default_filters` as mandatory constraints — these apply to every query regardless of table. Inject them as a dedicated "## Mandatory filters" section after the table schemas. The SQLAgent MUST include all of them in every generated query; they are not optional
- Include all `Metric` objects from `context.metrics` — inject as the "## Named business metrics" section. When the user references a metric by name or synonym, the agent MUST use the declared `sql` exactly, not substitute its own definition
- Include `value_description` for categorical columns — prevents the agent from guessing category spellings
- Include `synonyms` for columns where they exist — helps match user language to column names
- Suppress `sample_values` for `is_high_cardinality=True` columns and add the HIGH CARDINALITY warning instead
- Include `default_date_column` — used when user says "this year" or "last month" without specifying a column
- Omit fields that are empty — do not inject blank lines for missing optional fields

### Context filtering

Before building the prompt, filter to `intent.relevant_tables` and `intent.relevant_snippets`. Do not inject all tables and snippets — only what IntentAgent identified as relevant. This keeps prompts focused and reduces token usage.

---

## ClarifyAgent

### Responsibility

Generate a focused follow-up question when the intent is ambiguous. Should resolve the ambiguity with a single yes/no or multiple-choice question where possible.

### Result type

`ClarifyResult` is defined in [[data_models]]. Fields: `question` (the follow-up question to present to the user).

### Interface

```python
class ClarifyAgent:
    def __init__(self, llm: LLMProvider): ...

    async def run(
        self,
        question: str,
        context: ContextPackage,
        intent: IntentResult,
    ) -> ClarifyResult:
```

### Prompt template: `clarification.txt`

```
A user asked a question that needs clarification before you can write SQL.

## Available tables
{table_list}

## Original question
{question}

## Why it needs clarification
{intent.reasoning}

Generate a single, focused follow-up question to resolve the ambiguity.
Prefer yes/no or multiple-choice questions. Be brief.
Respond with the question text only. No preamble.
```

---

## VizAgent

### Responsibility

Given a `QueryResult`, choose the right chart type and produce a complete Vega-Lite spec. The [[README]] key rule applies: **no LLM call for Vega-Lite**. Chart type selection and spec construction are rule-based.

### Result type

See `VizResult` in [[data_models]].

### Interface

```python
class VizAgent:
    def __init__(self, llm: LLMProvider): ...
    # llm is injected but only used for summary generation, not spec generation

    async def run(
        self,
        question: str,
        query_result: QueryResult,
        context: ContextPackage,    # required — used for semantic_type lookup
    ) -> VizResult:
```

### Column type detection

`VizAgent.run()` receives both `query_result: QueryResult` and `context: ContextPackage`. Use `ColumnMeta.semantic_type` from `context.table_schemas` as the primary type signal, falling back to value inspection when the column is not found in the context (e.g. derived columns with aliases).

```python
def classify_column(
    col_name: str,
    query_result: QueryResult,
    context: ContextPackage,
) -> str:
    """Returns: "date" | "numeric" | "string" | "unknown" """

    # 1. Check semantic_type from resolved metadata
    for table_meta in context.table_schemas.values():
        for col_meta in table_meta.columns:
            if col_meta.name == col_name:
                if col_meta.semantic_type in ("date",):
                    return "date"
                if col_meta.semantic_type in ("currency", "measure"):
                    return "numeric"
                if col_meta.semantic_type in ("category", "identifier"):
                    return "string"

    # 2. Fallback: inspect first non-null value in the result
    for row in query_result.rows:
        val = row.get(col_name)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            return "numeric"
        if isinstance(val, str):
            # Try to parse as date
            import re
            if re.match(r"\d{4}-\d{2}-\d{2}", str(val)):
                return "date"
            return "string"
    return "unknown"
```

Update the chart selection rules to use this:

| Condition | Chart type |
|---|---|
| 1 `numeric` column, 0 other columns | `counter` |
| 1 `date` column + 1 `numeric` column | `line` |
| 1 `date` column + 2+ `numeric` columns | `line` (multi-series) |
| 1 `string` column + 1 `numeric` column, ≤ 20 rows | `bar` |
| 1 `string` column + 1 `numeric` column, > 20 rows | `table` |
| 2 `numeric` columns | `scatter` |
| anything else | `table` |

### Vega-Lite spec construction

Build the spec in Python using a `build_{chart_type}_spec(result: QueryResult) -> dict` function per chart type. Do not ask the LLM to generate JSON.

Example for `bar`:
```python
def build_bar_spec(result: QueryResult) -> dict:
    str_col = next(c for c in result.columns if is_string(result, c))
    num_col = next(c for c in result.columns if is_numeric(result, c))
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": result.rows},
        "mark": "bar",
        "encoding": {
            "x": {"field": str_col, "type": "nominal", "sort": "-y"},
            "y": {"field": num_col, "type": "quantitative"},
        }
    }
```

### Summary generation

After building the spec, make one `llm.complete()` call to generate `VizResult.summary`:

```
Prompt: "In one sentence, summarize this query result for a business user.
         Question: {question}
         Columns: {column_names}
         Row count: {row_count}
         Top rows: {first_3_rows}"
```

---

## PlanningAgent

### Responsibility

Determine how many SQL queries are needed to answer a question and produce an ordered execution plan. Single-step plans are returned for simple questions — no overhead. Multi-step plans decompose complex questions into dependent steps. Added by EXT-1 (multi-query reasoning).

### Result type

`ReasoningPlan` is defined in [[data_models]]. Fields: `question`, `steps` (list of `ReasoningStep`), `synthesis_instruction`.

### Interface

```python
class PlanningAgent:
    def __init__(self, llm: LLMProvider, max_steps: int = 5): ...

    async def plan(
        self,
        question: str,
        context: ContextPackage,
    ) -> ReasoningPlan:
```

**Hard cap:** plans are truncated to `max_steps` (default 5) with a logged WARNING if the LLM returns more. This prevents unbounded query loops regardless of prompt engineering failures.

**Defensive fallbacks:** empty `steps[]` response → one-step plan using the original question; forward references in `depends_on` → dropped with WARNING.

### Prompt template: `planning.txt`

Instructs the model to return a JSON reasoning plan. Single-step for direct aggregations; multi-step only for "why" / comparison / multi-dimension questions. `synthesis_instruction` explains how to combine the results.

---

## SynthesisAgent

### Responsibility

Produce a coherent prose answer from one or more query results. Quantifies confidence, names what the data does and does not support, and explicitly forbids causal language. Added by EXT-1 (multi-query) and EXT-7 (explicit uncertainty).

### Result type

`SynthesizedAnswer` is defined in [[data_models]]. Fields: `answer`, `data_supports`, `data_does_not_support`, `would_need`, `confidence` ("high" | "medium" | "low"), `confidence_rationale`.

### Interface

```python
class SynthesisAgent:
    def __init__(self, llm: LLMProvider): ...

    async def synthesize(
        self,
        question: str,
        plan: ReasoningPlan,
        results: list[QueryResult],
        context: ContextPackage,
    ) -> SynthesizedAnswer:
```

### Causal language enforcement (structural invariant)

After generating the response, `_enforce_no_causal_language()` scans `answer` for the following phrases (case-insensitive, word-boundary):

- `caused by` / `caused`
- `because of` / `because`
- `due to`
- `result of`
- `led to`

If any phrase is found, `SynthesisError` is raised — not returned. The pipeline catches this as a hard error. This is structural enforcement, not prompt-level guidance.

**Allowed alternatives:** "associated with", "coincided with", "occurred alongside", "is consistent with", "correlates with".

### Confidence assignment rules

| Confidence | When to use |
|---|---|
| `high` | Single unambiguous query, clean data, no assumptions required |
| `medium` | Join across multiple tables, or requires assumptions about business definitions |
| `low` | Inference across incomplete data, time gaps, ambiguous terms, or causal phrasing in the question |

### When SynthesizedAnswer is attached to a turn

- **Multi-step plans:** always attached
- **Single-step plans:** attached only when confidence is `medium` or `low`

High-confidence single-step answers don't require synthesis prose — the SQL + result + viz events already convey the answer.

### Prompt template: `synthesis.txt`

Forbids causal verbs, names allowed alternatives, encodes the confidence rules. For multi-step plans, enumerates each step's SQL, columns, and row preview. For single-step plans, references the single result.

---

## HypothesisAgent

### Responsibility

Generate provisional candidate explanations for observed data patterns. Runs only when `RoomConfig.hypothesis_mode_enabled=True`, the question is causal ("why" / "what caused"), and a multi-step `ReasoningPlan` with results is available. Added by EXT-11 (hypothesis mode). **Requires EXT-1.**

### Result type

`HypothesisResult` is defined in [[data_models]]. Contains `hypotheses: list[Hypothesis]`, `confidence` (always `"low"` — invariant), and `disclaimer` (always non-empty — invariant).

### Interface

```python
class HypothesisAgent:
    def __init__(self, llm: LLMProvider): ...

    async def run(
        self,
        question: str,
        plan: ReasoningPlan,
        results: list[QueryResult],
        synthesized: SynthesizedAnswer,
        context: ContextPackage,       # for domain_knowledge and table metadata
    ) -> HypothesisResult:
```

### Three gates (checked by RoomEngine before calling the agent)

1. `RoomConfig.hypothesis_mode_enabled == True` — room author explicit opt-in
2. `len(plan.steps) > 1` — multi-step evidence required
3. `_is_causal_question(question)` — substring scan for "why" / "what caused" / "what led to" / "what drove"

All three must pass. The agent itself does not re-check these.

### Causal language enforcement

Same `_enforce_no_causal_language()` post-generation scan as `SynthesisAgent`, applied to every `Hypothesis.statement`. Raises `HypothesisError` on violation.

### Structural invariants (enforced by `__post_init__`)

- `HypothesisResult.confidence` MUST be `"low"` — raises `ValueError` if any other value is set
- `HypothesisResult.disclaimer` MUST be non-empty — raises `ValueError` if empty or whitespace
- Every `Hypothesis` MUST have at least one `contradicting_pattern` — raises `ValueError` if empty

A hypothesis with only supporting evidence is not a hypothesis — it is a claim. These invariants cannot be overridden at runtime.

### Domain knowledge grounding

`context.domain_knowledge` entries (from `RoomConfig.domain_knowledge`) are injected into the prompt. The agent is instructed to use them for more targeted hypotheses and to record which entries it used in `Hypothesis.domain_knowledge_used`. Hallucinated entries (not present in `domain_knowledge`) are dropped with a WARNING — not raised.

### Prompt template: `hypothesis_generation.txt`

Forbids causal verbs (`caused`, `because`, `due to`, `result of`, `led to`, `explains`, `drove`, `produced`). Mandates hedged language. Caps output at 3 hypotheses. Requires `contradicting_patterns` for every hypothesis. Specifies that `domain_knowledge_used` must reference room axioms verbatim.

---

## Test cases

| # | Agent | Scenario | MUST |
|---|---|---|---|
| 1 | IntentAgent | Question clearly about an in-scope table | MUST return `intent="sql_query"` with `confidence >= 0.7` |
| 2 | IntentAgent | Question about something with no relevant table | MUST return `intent="out_of_scope"` |
| 3 | IntentAgent | Ambiguous question (e.g. "show me the data") | MUST return `intent="clarify_needed"` or `confidence < 0.7` |
| 4 | IntentAgent | Response | MUST be parseable as valid JSON matching `IntentResult` shape |
| 5 | IntentAgent | LLM returns a `relevant_snippets` display name not in context | MUST drop silently, log WARNING, not raise |
| 6 | SQLAgent | Valid question | MUST call `query.validate()` before returning |
| 7 | SQLAgent | First SQL attempt has syntax error | MUST retry and return corrected SQL |
| 8 | SQLAgent | All retries fail | MUST return `SQLResult(is_valid=False)`, not raise |
| 9 | SQLAgent | SQL references table not in `intent.relevant_tables` | SHOULD be flagged — indicates context filtering bug |
| 10 | ClarifyAgent | Ambiguous question | MUST return a non-empty question string |
| 11 | ClarifyAgent | Response | MUST NOT contain SQL |
| 12 | VizAgent | Single numeric result | MUST return `chart_type="counter"` |
| 13 | VizAgent | Column with `semantic_type="date"` + numeric column | MUST return `chart_type="line"` using semantic_type, not value inspection |
| 14 | VizAgent | Derived column with alias not in context (fallback path) | MUST fall back to value inspection for type detection |
| 15 | VizAgent | `vega_lite_spec` | MUST be valid Vega-Lite v5 JSON |
| 16 | VizAgent | Any input | MUST NOT call `llm.complete()` for spec construction |
| 17 | All agents | Any input | MUST NOT import or call any SDK directly |
| 18 | PlanningAgent | Simple aggregation question | MUST return a one-step plan |
| 19 | PlanningAgent | "Why did X change?" question | MUST return a multi-step plan with ≥ 2 steps |
| 20 | PlanningAgent | LLM returns > 5 steps | MUST truncate to 5 with a logged WARNING |
| 21 | PlanningAgent | Multi-step plan | MUST execute steps in dependency order |
| 22 | SynthesisAgent | Any answer | MUST NOT contain "caused by", "because of", "due to", "result of", "led to" in `answer` field |
| 23 | SynthesisAgent | Direct aggregation question | MUST return `confidence="high"` |
| 24 | SynthesisAgent | "Why did X change?" question | MUST return `confidence="low"` and non-empty `data_does_not_support` |
| 25 | HypothesisAgent | Output | MUST NOT contain causal language in any `Hypothesis.statement` |
| 26 | HypothesisAgent | `HypothesisResult.confidence` | MUST always be `"low"` — not configurable |
| 27 | HypothesisAgent | `HypothesisResult.disclaimer` | MUST always be present and non-empty |
| 28 | HypothesisAgent | Room with `hypothesis_mode_enabled=False` | MUST NOT be called even for why-questions |
| 29 | HypothesisAgent | Hypothesis with `testability="testable_in_room"` | MUST include a `suggested_test` |
| 30 | HypothesisAgent | Any hypothesis | MUST include at least one `contradicting_pattern` |
