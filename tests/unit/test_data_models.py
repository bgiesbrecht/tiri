"""Tests for tiri.data_models.

Every row in `docs/data_models.md`'s `## Test cases` table is asserted here,
plus EXT-11 invariants from the same doc.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from tiri.data_models import (
    Benchmark,
    BenchmarkReport,
    BenchmarkResult,
    ClarifyResult,
    ColumnMeta,
    ColumnOverride,
    ContextPackage,
    ConversationTurn,
    ExampleSQL,
    Hypothesis,
    HypothesisResult,
    IntentResult,
    JoinSpec,
    LLMMessage,
    LLMResponse,
    MetadataConflict,
    Metric,
    QueryResult,
    ReasoningPlan,
    ReasoningStep,
    RoomConfig,
    SQLResult,
    SqlSnippet,
    SynthesizedAnswer,
    TableMeta,
    VectorMatch,
    VizResult,
)


def _sample_config(**overrides) -> RoomConfig:
    base = dict(
        room_id="test-room",
        title="Test Room",
        tables=["catalog.schema.table1"],
        warehouse_id="wh-123",
    )
    base.update(overrides)
    return RoomConfig(**base)


# ── Test case 1: RoomConfig with missing `tables` MUST raise ValueError ─────


def test_room_config_requires_tables() -> None:
    with pytest.raises(ValueError, match="tables"):
        RoomConfig(
            room_id="test-room",
            title="Test Room",
            tables=[],
            warehouse_id="wh-123",
        )


def test_room_config_requires_non_empty_room_id() -> None:
    with pytest.raises(ValueError, match="room_id"):
        RoomConfig(
            room_id="",
            title="Test Room",
            tables=["c.s.t"],
            warehouse_id="wh-123",
        )


@pytest.mark.parametrize(
    "bad_id", ["has space", "has/slash", "has\\backslash", "has\ttab"]
)
def test_room_config_requires_url_safe_room_id(bad_id: str) -> None:
    with pytest.raises(ValueError, match="URL-safe"):
        RoomConfig(
            room_id=bad_id,
            title="Test Room",
            tables=["c.s.t"],
            warehouse_id="wh-123",
        )


def test_room_config_requires_warehouse_id() -> None:
    with pytest.raises(ValueError, match="warehouse_id"):
        RoomConfig(
            room_id="test-room",
            title="Test Room",
            tables=["c.s.t"],
            warehouse_id="",
        )


# ── Test case 2: RoomConfig JSON round-trip MUST be identical to original ──


def test_room_config_json_round_trip_identical() -> None:
    original = RoomConfig(
        room_id="r1",
        title="Demo",
        tables=["cat.sch.t1", "cat.sch.t2"],
        warehouse_id="wh1",
        text_instruction="some instruction",
        examples=[
            ExampleSQL(question="q1", sql="select 1", id="ex1"),
            ExampleSQL(question="q2", sql="select 2", id="ex2"),
        ],
        joins=[
            JoinSpec(
                left_table="cat.sch.t1",
                left_alias="a",
                right_table="cat.sch.t2",
                right_alias="b",
                join_on="a.x = b.y",
                relationship_type="MANY_TO_ONE",
                instruction="join note",
                id="j1",
            )
        ],
        sql_filters=[
            SqlSnippet(
                display_name="f1",
                sql="x = 1",
                kind="filter",
                synonyms=["s1", "s2"],
                id="f-1",
            )
        ],
        sql_expressions=[
            SqlSnippet(
                display_name="rev",
                sql="extendedprice * (1 - discount)",
                kind="expression",
                id="e-1",
            )
        ],
        sql_measures=[
            SqlSnippet(display_name="sum_rev", sql="SUM(x)", kind="measure", id="m-1")
        ],
        sample_questions=["What is X?", "How many Y?"],
        benchmarks=[
            Benchmark(
                question="bench", expected_sql="select 1", expected_row_count=5, id="b-1"
            )
        ],
        column_overrides=[
            ColumnOverride(table="cat.sch.t1", column="c1", description="override d")
        ],
        max_tables_per_query=15,
        hypothesis_mode_enabled=True,
        domain_knowledge=["axiom 1", "axiom 2"],
    )
    serialized = json.dumps(asdict(original))
    reloaded = RoomConfig.from_dict(json.loads(serialized))
    assert asdict(reloaded) == asdict(original)


# ── Test case 3: duplicate ExampleSQL ids MUST raise ValueError ─────────────


def test_room_config_rejects_duplicate_example_ids() -> None:
    with pytest.raises(ValueError, match="unique"):
        _sample_config(
            examples=[
                ExampleSQL(question="q1", sql="s1", id="dup"),
                ExampleSQL(question="q2", sql="s2", id="dup"),
            ]
        )


# ── Test case 4: SqlSnippet with invalid kind MUST raise ValueError ─────────


def test_sql_snippet_rejects_invalid_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        SqlSnippet(display_name="x", sql="y", kind="invalid")


@pytest.mark.parametrize("kind", ["filter", "expression", "measure"])
def test_sql_snippet_accepts_valid_kinds(kind: str) -> None:
    SqlSnippet(display_name="x", sql="y", kind=kind)  # MUST NOT raise


# ── Test case 5: ConversationTurn mutual exclusion ──────────────────────────


def test_conversation_turn_rejects_sql_plus_clarification() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ConversationTurn(
            room_id="r", sql="select 1", clarification_question="huh?"
        )


def test_conversation_turn_rejects_sql_plus_error() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ConversationTurn(room_id="r", sql="select 1", error="boom")


def test_conversation_turn_rejects_clarification_plus_error() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ConversationTurn(
            room_id="r", clarification_question="huh?", error="boom"
        )


def test_conversation_turn_rejects_none_set() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ConversationTurn(room_id="r")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sql": "select 1"},
        {"clarification_question": "did you mean X or Y?"},
        {"error": "out of scope"},
    ],
)
def test_conversation_turn_accepts_exactly_one_set(kwargs: dict) -> None:
    ConversationTurn(room_id="r", **kwargs)  # MUST NOT raise


# ── Test case 6: all dataclasses importable with no side effects ────────────


def test_module_imports_with_no_side_effects() -> None:
    # If module-level work raised, the import at the top of this file would
    # have failed before collection. The presence of this test asserts it.
    from tiri import data_models

    assert data_models is not None


# ── Test case 7: all dataclasses serialize via asdict + json.dumps ──────────


SERIALIZABLE_INSTANCES = [
    ColumnOverride(table="t", column="c"),
    ExampleSQL(question="q", sql="s"),
    JoinSpec(
        left_table="t1",
        left_alias="a",
        right_table="t2",
        right_alias="b",
        join_on="a.x=b.y",
        relationship_type="ONE_TO_ONE",
    ),
    SqlSnippet(display_name="x", sql="x=1", kind="filter"),
    Benchmark(question="q", expected_sql="select 1"),
    MetadataConflict(
        table="t",
        column=None,
        field="description",
        values={"prov_a": "x"},
        resolved_to="prov_a",
    ),
    ColumnMeta(name="c", data_type="INT"),
    TableMeta(full_name="cat.sch.t"),
    QueryResult(
        columns=["a"], rows=[{"a": 1}], row_count=1, truncated=False, duration_ms=10
    ),
    VizResult(chart_type="bar", vega_lite_spec={"$schema": "x"}, summary="s"),
    LLMMessage(role="system", content="hi"),
    LLMResponse(content="hi", usage={"prompt_tokens": 1}, raw=None),
    VectorMatch(id="i", score=0.9, payload={"q": "x"}),
    IntentResult(
        intent="sql_query",
        relevant_tables=[],
        relevant_snippets=[],
        confidence=0.9,
        reasoning="ok",
    ),
    SQLResult(is_valid=True, attempts=1, sql="select 1"),
    ClarifyResult(question="huh?"),
    ConversationTurn(room_id="r", sql="select 1"),
    ContextPackage(
        room_id="r",
        table_schemas={},
        joins=[],
        sql_snippets=[],
        metrics=[],
        text_instruction="",
        default_filters=[],
        retrieved_examples=[],
        conversation_history=[],
    ),
    SynthesizedAnswer(
        answer="x",
        data_supports=[],
        data_does_not_support=[],
        would_need=[],
        confidence="high",
        confidence_rationale="r",
    ),
    ReasoningStep(
        step_id="s1", description="d", sql=None, result=None, depends_on=[]
    ),
    ReasoningPlan(question="q", steps=[], synthesis_instruction=""),
    Hypothesis(
        statement="x may contribute to y",
        supporting_patterns=["p1"],
        contradicting_patterns=["p2"],
        testability="not_testable",
        suggested_test=None,
        domain_knowledge_used=[],
    ),
    HypothesisResult(
        hypotheses=[
            Hypothesis(
                statement="x may contribute to y",
                supporting_patterns=["p1"],
                contradicting_patterns=["p2"],
                testability="not_testable",
                suggested_test=None,
                domain_knowledge_used=[],
            )
        ]
    ),
    BenchmarkResult(
        benchmark_id="b1",
        question="q",
        expected_sql="select 1",
        generated_sql="select 1",
        sql_match=True,
        result_match=True,
        passed=True,
        error=None,
    ),
    BenchmarkReport(
        room_id="r",
        run_at="2026-01-01T00:00:00Z",
        total=1,
        passed=1,
        failed=0,
        score=1.0,
        results=[],
    ),
]


@pytest.mark.parametrize(
    "instance", SERIALIZABLE_INSTANCES, ids=lambda x: type(x).__name__
)
def test_dataclass_serializes_via_asdict_and_json(instance) -> None:
    payload = asdict(instance)
    json_str = json.dumps(payload)
    parsed = json.loads(json_str)
    assert parsed == payload


# ── EXT-11 invariants (data_models.md HypothesisResult.__post_init__) ──────


def _valid_hypothesis(**overrides) -> Hypothesis:
    base = dict(
        statement="x may be associated with y",
        supporting_patterns=["p1"],
        contradicting_patterns=["p2"],
        testability="testable_in_room",
        suggested_test="run query Z",
        domain_knowledge_used=[],
    )
    base.update(overrides)
    return Hypothesis(**base)


def test_hypothesis_result_confidence_must_be_low() -> None:
    with pytest.raises(ValueError, match="confidence"):
        HypothesisResult(hypotheses=[_valid_hypothesis()], confidence="high")


def test_hypothesis_result_disclaimer_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="disclaimer"):
        HypothesisResult(hypotheses=[_valid_hypothesis()], disclaimer="")


def test_hypothesis_result_disclaimer_must_be_non_whitespace() -> None:
    with pytest.raises(ValueError, match="disclaimer"):
        HypothesisResult(hypotheses=[_valid_hypothesis()], disclaimer="   ")


def test_hypothesis_must_have_contradicting_patterns() -> None:
    with pytest.raises(ValueError, match="contradicting"):
        HypothesisResult(
            hypotheses=[_valid_hypothesis(contradicting_patterns=[])]
        )


def test_hypothesis_result_accepts_valid_input() -> None:
    HypothesisResult(hypotheses=[_valid_hypothesis()])  # MUST NOT raise


# ── Change 1: RoomConfig.default_filters round-trip ─────────────────────────


def test_room_config_default_filters_round_trip() -> None:
    config = _sample_config(
        default_filters=["tenant_id = 'acme'", "environment != 'test'"]
    )
    reloaded = RoomConfig.from_dict(json.loads(json.dumps(asdict(config))))
    assert asdict(reloaded) == asdict(config)


@pytest.mark.parametrize(
    "bad_filter",
    [
        "SELECT * FROM secrets",
        "WITH cte AS (SELECT 1) SELECT * FROM cte",
    ],
)
def test_room_config_rejects_full_statements_in_default_filters(
    bad_filter: str,
) -> None:
    with pytest.raises(ValueError, match="SELECT|WITH"):
        _sample_config(default_filters=[bad_filter])


@pytest.mark.parametrize(
    "fragment",
    [
        "tenant_id = 'acme'",
        "environment != 'test'",
        "active = true",
        "status IN ('active', 'trial')",
        "amount > (SELECT AVG(amount) FROM orders)",  # subquery in WHERE is valid
    ],
)
def test_room_config_accepts_valid_default_filter_fragments(
    fragment: str,
) -> None:
    _sample_config(default_filters=[fragment])  # MUST NOT raise


# ── Change 2: Metric type + RoomConfig.metrics ──────────────────────────────


def test_metric_serializes_via_asdict_and_json() -> None:
    m = Metric(
        name="revenue",
        display_name="Net Revenue",
        sql="SUM(l_extendedprice * (1 - l_discount))",
        grain="line item",
        description="Revenue after discounts",
        synonyms=["sales", "net sales"],
        dimensions=["r_name", "c_mktsegment"],
        filters=[],
        unit="USD",
    )
    payload = asdict(m)
    assert json.loads(json.dumps(payload)) == payload


def test_room_config_metrics_round_trip() -> None:
    config = _sample_config(
        metrics=[
            Metric(
                name="revenue",
                display_name="Net Revenue",
                sql="SUM(x * (1 - y))",
                grain="line item",
            )
        ]
    )
    reloaded = RoomConfig.from_dict(json.loads(json.dumps(asdict(config))))
    assert asdict(reloaded) == asdict(config)
