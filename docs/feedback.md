---
tags: [layer/surface]
status: stable
depends_on: [providers, data_models, room_engine]
---

# Feedback

## In this system

**Linked from:** [[README]], [[api]]
**Links to:** [[providers]], [[data_models]], [[room_engine]]
**Layer:** surface

---

## What this is

The improvement loop that makes rooms better over time. Three components:

- **Collector** — stores thumbs-up/down signals against conversation turns
- **Proposer** — analyzes thumbs-up turns and proposes new example SQLs for admin review
- **BenchmarkRunner** — evaluates room quality by running stored question/SQL pairs

Together these close the feedback loop: users signal quality → proposer extracts knowledge → admin approves → room improves. Without active maintenance, room quality degrades as data models change.

---

## Collector

### Responsibility

Attach a feedback signal to a persisted `ConversationTurn`. Called by the feedback route in [[api]].

### Interface

```python
class Collector:
    def __init__(self, store: StoreProvider): ...

    async def record(
        self,
        conversation_id: str,
        turn_id: str,
        signal: str,         # "up" | "down"
        comment: str = "",
    ) -> None:
        """
        1. Load the existing turn from store
        2. Set turn.feedback_signal = signal
        3. store.put("conv:{conv_id}:turn:{turn_id}", updated_turn)
        4. store.put("feedback:{conv_id}:{turn_id}", {"signal": signal, "comment": comment})
        """
```

**API route** (called from [[api]] feedback router):
```
POST /rooms/{room_id}/conversations/{conv_id}/messages/{turn_id}/feedback
body: {"signal": "up" | "down", "comment": ""}
→ 200
```

---

## Proposer

### Responsibility

Scan all thumbs-up turns for a room and propose new `ExampleSQL` entries for the knowledge store. Proposed examples require admin approval before being added to the room config — the proposer never modifies `RoomConfig` directly.

### Interface

```python
class Proposer:
    def __init__(self, store: StoreProvider, llm: LLMProvider): ...

    async def propose(self, room_id: str, config: RoomConfig) -> list[ExampleSQL]:
        """
        1. Collect all turns for room_id where feedback_signal == "up"
        2. Filter out turns already in config.examples (matched by SQL similarity)
        3. For each remaining turn, make one LLM call:
              "Given this question and SQL that a user rated helpful,
               should it be added as a worked example?
               Question: {question}
               SQL: {sql}
               Reply YES or NO with a one-sentence reason."
        4. Return list of proposed ExampleSQL for admin review
        """
```

### Admin review flow

Proposals are returned to the caller (management API or a notebook). The admin reviews them and calls `PATCH /rooms/{room_id}` to add approved examples. This is intentionally a human-in-the-loop step — the proposer never auto-approves.

**API route:**
```
POST /rooms/{room_id}/feedback/propose
→ 200 {"proposed_examples": [ExampleSQL, ...]}
```

---

## BenchmarkRunner

### Responsibility

Evaluate room quality by running every benchmark question through the full pipeline and comparing the generated SQL to the expected SQL. Produces a `BenchmarkReport` with pass/fail and a summary score.

### Supporting types

`BenchmarkResult` and `BenchmarkReport` are defined in [[data_models]]. Reference them there for field-level documentation.

### Interface

```python
class BenchmarkRunner:
    def __init__(self, engine: RoomEngine): ...

    async def run(self, room_id: str) -> BenchmarkReport:
        """
        For each benchmark in RoomConfig.benchmarks:
          1. engine.chat(room_id, conv_id=f"benchmark-{benchmark.id}", question=benchmark.question)
          2. Extract generated SQL from the returned ConversationTurn
          3. Compare normalized(generated_sql) == normalized(expected_sql)
             Normalization: lowercase, collapse whitespace, strip trailing semicolons
          4. If benchmark.expected_row_count is set:
               run both SQLs via query.execute() and compare row counts
          5. Assemble BenchmarkResult
        Assemble and return BenchmarkReport.
        """
```

**Normalization rules for SQL comparison:**
- Lowercase all keywords
- Collapse all whitespace (spaces, tabs, newlines) to single spaces
- Strip leading/trailing whitespace
- Remove trailing semicolons
- Do NOT normalize column aliases, string literals, or quoted identifiers

**API route:**
```
POST /rooms/{room_id}/benchmarks/run
→ 200 BenchmarkReport

POST /rooms/{room_id}/benchmarks
body: Benchmark
→ 201 {"benchmark_id": "..."}

DELETE /rooms/{room_id}/benchmarks/{benchmark_id}
→ 204
```

---

## Feedback store key layout

All feedback data uses the `StoreProvider` from [[providers]]:

```
conv:{conv_id}:turn:{turn_id}     ← ConversationTurn (feedback_signal set here)
feedback:{conv_id}:{turn_id}      ← raw signal + comment, for bulk scanning
```

To retrieve all thumbs-up turns for a room, `Proposer` uses the room→conversation index:

1. `store.get("room:{room_id}:conversations")` → list of `conversation_id` values for this room
2. For each `conversation_id`: `store.list_keys("conv:{conv_id}:turn:")` → turn keys
3. Filter turns where `feedback_signal == "up"`

This avoids the full store scan and correctly scopes to the room. The `room:{room_id}:conversations` index is maintained by `RoomEngine` — see [[data_models]] store key layout.

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | `Collector.record()` with `signal="up"` | MUST update `turn.feedback_signal` in store |
| 2 | `Collector.record()` for nonexistent turn | MUST raise `StoreProviderError` or equivalent |
| 3 | `Proposer.propose()` with no thumbs-up turns | MUST return empty list |
| 4 | `Proposer.propose()` with a thumbs-up turn already in examples | MUST NOT propose it again |
| 5 | `Proposer.propose()` | MUST NOT modify `RoomConfig` |
| 6 | `BenchmarkRunner.run()` with matching SQL | MUST return `passed=True` for that benchmark |
| 7 | `BenchmarkRunner.run()` with non-matching SQL | MUST return `passed=False`, not raise |
| 8 | `BenchmarkRunner.run()` when pipeline errors | MUST record the error in `BenchmarkResult.error`, continue remaining benchmarks |
| 9 | SQL normalization | `"SELECT id FROM t"` and `"select  id  from t;"` MUST be considered equal |
| 10 | `BenchmarkReport.score` | MUST equal `passed / total` exactly |
