"""Tests for tiri.feedback.* — Collector, Proposer, BenchmarkRunner.

Covers all 10 test cases in docs/feedback.md plus the Proposer's
benchmark-conversation exclusion.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

import pytest

from tiri.data_models import (
    Benchmark,
    ColumnMeta,
    ConversationTurn,
    ExampleSQL,
    LLMMessage,
    LLMResponse,
    QueryResult,
    RoomConfig,
    TableMeta,
    VectorMatch,
)
from tiri.engine.room_engine import RoomEngine
from tiri.feedback.benchmark_runner import BenchmarkRunner
from tiri.feedback.collector import Collector
from tiri.feedback.proposer import Proposer
from tiri.feedback.sql_normalize import normalize_sql
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    QueryProvider,
    StoreProvider,
    StoreProviderError,
    VectorProvider,
)


# ── Stub providers ──────────────────────────────────────────────────────────


class _Store(StoreProvider):
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    async def get(self, key):
        v = self._data.get(key)
        return None if v is None else json.loads(json.dumps(v))

    async def put(self, key, value):
        self._data[key] = json.loads(json.dumps(value))

    async def list_keys(self, prefix):
        return sorted(k for k in self._data if k.startswith(prefix))

    async def delete(self, key):
        self._data.pop(key, None)


_DEFAULT_SYNTHESIS_JSON = json.dumps(
    {
        "answer": "Result shown above.",
        "data_supports": [],
        "data_does_not_support": [],
        "would_need": [],
        "confidence": "high",
        "confidence_rationale": "test default",
    }
)

_DEFAULT_PLANNING_JSON = json.dumps(
    {
        "requires_multiple_queries": False,
        "steps": [
            {"step_id": "step_1", "description": "single-step default", "depends_on": []}
        ],
        "synthesis_instruction": "Report the single result directly.",
    }
)


class _ScriptedLLM(LLMProvider):
    """One canned response per call. .calls records every prompt sent.

    EXT-7/EXT-1: task="synthesis" returns a high-confidence default and
    task="planning" returns a one-step plan OUTSIDE the response queue.
    Benchmark assertions only care about SQL correctness/execution.
    Defaults keep queue indices the same as pre-EXT-1.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls: list[str] = []

    async def complete(self, messages, temperature=0.0, max_tokens=2048, task="sql", model=None):
        prompt = messages[0].content if messages else ""
        self.calls.append(prompt)
        if task == "synthesis":
            return LLMResponse(content=_DEFAULT_SYNTHESIS_JSON, usage={}, raw=None)
        if task == "planning":
            return LLMResponse(content=_DEFAULT_PLANNING_JSON, usage={}, raw=None)
        if self._index >= len(self._responses):
            raise AssertionError("_ScriptedLLM exhausted")
        content = self._responses[self._index]
        self._index += 1
        return LLMResponse(content=content, usage={}, raw=None)

    async def stream(self, messages, temperature=0.0, task="sql", model=None) -> AsyncIterator[str]:
        yield ""

    async def embed(self, texts):
        return [[0.0] for _ in texts]


# ── Helpers ─────────────────────────────────────────────────────────────────


def _store_turn(
    store: _Store,
    *,
    conv_id: str,
    turn_id: str,
    room_id: str = "r1",
    question: str = "q",
    sql: str | None = "SELECT 1",
    error: str | None = None,
    clarification: str | None = None,
    feedback: str | None = None,
) -> None:
    turn = ConversationTurn(
        room_id=room_id,
        conversation_id=conv_id,
        turn_id=turn_id,
        question=question,
        sql=sql,
        error=error,
        clarification_question=clarification,
        feedback_signal=feedback,
    )
    store._data[f"conv:{conv_id}:turn:{turn_id}"] = json.loads(
        json.dumps(asdict(turn))
    )


def _store_conv_index(store: _Store, conv_id: str, turn_ids: list[str]) -> None:
    store._data[f"conv:{conv_id}:index"] = {"turn_ids": turn_ids}


def _store_room_index(store: _Store, room_id: str, conv_ids: list[str]) -> None:
    store._data[f"room:{room_id}:conversations"] = {
        "conversation_ids": conv_ids
    }


# ═══════════════════════════════════════════════════════════════════════════
# SQL normalization — case 9
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_sql_treats_whitespace_and_semicolons_as_equivalent() -> None:
    """Case 9."""
    a = "SELECT id FROM t"
    b = "select  id  from t;"
    assert normalize_sql(a) == normalize_sql(b)


def test_normalize_sql_preserves_string_literal_casing() -> None:
    """Casing INSIDE string literals must not change — that would alter
    the data the query selects on."""
    sql = "SELECT * FROM t WHERE name = 'Acme Co'"
    out = normalize_sql(sql)
    assert "'Acme Co'" in out
    assert "select * from t where name = " in out


def test_normalize_sql_strips_trailing_semicolons() -> None:
    assert normalize_sql("SELECT 1;") == "select 1"
    assert normalize_sql("SELECT 1 ;  ; ") == "select 1"


def test_normalize_sql_collapses_internal_whitespace() -> None:
    assert (
        normalize_sql("SELECT   a,\n   b\nFROM   t")
        == "select a, b from t"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Collector — cases 1, 2
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_collector_record_up_updates_feedback_signal() -> None:
    """Case 1."""
    store = _Store()
    _store_turn(store, conv_id="c1", turn_id="t1")
    collector = Collector(store=store)

    await collector.record("c1", "t1", "up", comment="great")

    turn = await store.get("conv:c1:turn:t1")
    assert turn["feedback_signal"] == "up"
    feedback = await store.get("feedback:c1:t1")
    assert feedback == {"signal": "up", "comment": "great"}


@pytest.mark.asyncio
async def test_collector_record_for_nonexistent_turn_raises() -> None:
    """Case 2."""
    collector = Collector(store=_Store())
    with pytest.raises(StoreProviderError, match="not found"):
        await collector.record("ghost", "missing", "up")


@pytest.mark.asyncio
async def test_collector_rejects_invalid_signal() -> None:
    store = _Store()
    _store_turn(store, conv_id="c1", turn_id="t1")
    collector = Collector(store=store)
    with pytest.raises(ValueError, match="up"):
        await collector.record("c1", "t1", "maybe")


# ═══════════════════════════════════════════════════════════════════════════
# Proposer — cases 3, 4, 5 + benchmark exclusion
# ═══════════════════════════════════════════════════════════════════════════


def _room(examples: list[ExampleSQL] | None = None) -> RoomConfig:
    return RoomConfig(
        room_id="r1",
        title="r",
        tables=["main.x.t"],
        warehouse_id="wh",
        examples=examples or [],
    )


@pytest.mark.asyncio
async def test_proposer_with_no_thumbs_up_returns_empty_list() -> None:
    """Case 3."""
    store = _Store()
    _store_room_index(store, "r1", ["c1"])
    _store_conv_index(store, "c1", ["t1"])
    _store_turn(store, conv_id="c1", turn_id="t1", feedback=None)
    llm = _ScriptedLLM([])  # never called
    proposer = Proposer(store=store, llm=llm)
    out = await proposer.propose("r1", _room())
    assert out == []
    assert llm.calls == []


@pytest.mark.asyncio
async def test_proposer_skips_turn_already_in_examples() -> None:
    """Case 4."""
    store = _Store()
    _store_room_index(store, "r1", ["c1"])
    _store_conv_index(store, "c1", ["t1"])
    _store_turn(
        store,
        conv_id="c1",
        turn_id="t1",
        question="how many?",
        sql="SELECT count(*) FROM t",
        feedback="up",
    )
    # Existing example matches the turn's SQL (normalized).
    existing = ExampleSQL(question="any", sql="select count(*) from t;", id="E")
    llm = _ScriptedLLM([])  # MUST NOT be called for already-known SQL
    proposer = Proposer(store=store, llm=llm)
    out = await proposer.propose("r1", _room(examples=[existing]))
    assert out == []
    assert llm.calls == []  # filtered before LLM judging


@pytest.mark.asyncio
async def test_proposer_does_not_modify_room_config() -> None:
    """Case 5."""
    store = _Store()
    _store_room_index(store, "r1", ["c1"])
    _store_conv_index(store, "c1", ["t1"])
    _store_turn(
        store,
        conv_id="c1",
        turn_id="t1",
        question="q",
        sql="SELECT 1",
        feedback="up",
    )
    config = _room()
    snapshot = asdict(config)
    llm = _ScriptedLLM(["YES it's a good example."])
    proposer = Proposer(store=store, llm=llm)
    proposed = await proposer.propose("r1", config)

    assert len(proposed) == 1
    # Original config unchanged.
    assert asdict(config) == snapshot


@pytest.mark.asyncio
async def test_proposer_excludes_benchmark_conversations() -> None:
    """Benchmark conversations (`benchmark-*`) MUST NOT be scanned for
    proposals — they are synthetic runs, not user feedback."""
    store = _Store()
    _store_room_index(store, "r1", ["c1", "benchmark-q1"])
    _store_conv_index(store, "c1", ["t1"])
    _store_conv_index(store, "benchmark-q1", ["bt1"])
    _store_turn(
        store, conv_id="c1", turn_id="t1",
        question="real q", sql="SELECT 1", feedback="up",
    )
    _store_turn(
        store, conv_id="benchmark-q1", turn_id="bt1",
        question="bench q", sql="SELECT 99", feedback="up",
    )
    llm = _ScriptedLLM(["YES"])  # one YES — should be the c1 turn only
    proposer = Proposer(store=store, llm=llm)
    out = await proposer.propose("r1", _room())
    assert len(out) == 1
    assert out[0].question == "real q"
    # Only the real turn was sent to the LLM (one call).
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_proposer_drops_llm_no_responses() -> None:
    store = _Store()
    _store_room_index(store, "r1", ["c1"])
    _store_conv_index(store, "c1", ["t1", "t2"])
    _store_turn(
        store, conv_id="c1", turn_id="t1", question="yes-q", sql="SELECT 1",
        feedback="up",
    )
    _store_turn(
        store, conv_id="c1", turn_id="t2", question="no-q", sql="SELECT 2",
        feedback="up",
    )
    llm = _ScriptedLLM(["YES — clean example", "NO — too narrow"])
    proposer = Proposer(store=store, llm=llm)
    out = await proposer.propose("r1", _room())
    assert [ex.question for ex in out] == ["yes-q"]


@pytest.mark.asyncio
async def test_proposer_skips_turns_without_sql() -> None:
    """Clarification and error turns can have feedback but no SQL — skip them."""
    store = _Store()
    _store_room_index(store, "r1", ["c1"])
    _store_conv_index(store, "c1", ["t1"])
    _store_turn(
        store, conv_id="c1", turn_id="t1",
        question="q", sql=None, clarification="?", feedback="up",
    )
    llm = _ScriptedLLM([])  # never called
    proposer = Proposer(store=store, llm=llm)
    out = await proposer.propose("r1", _room())
    assert out == []


# ═══════════════════════════════════════════════════════════════════════════
# BenchmarkRunner — cases 6, 7, 8, 10
# ═══════════════════════════════════════════════════════════════════════════


class _Catalog(CatalogProvider):
    async def get_table_meta(self, full_name):
        return TableMeta(
            full_name=full_name,
            columns=[ColumnMeta(name="id", data_type="BIGINT")],
        )

    async def list_tables(self, c, s):
        return []

    async def list_schemas(self, c):
        return []

    async def search_tables(self, q, limit=10):
        return []


class _Vector(VectorProvider):
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    async def upsert(self, id, vector, payload):
        self._data[id] = {"vector": vector, "payload": dict(payload)}

    async def query(self, vector, top_k=5, filter=None):
        return []

    async def delete(self, id):
        self._data.pop(id, None)

    async def list_ids(self, filter=None):
        return list(self._data.keys())


class _Query(QueryProvider):
    def __init__(
        self,
        *,
        validation: tuple[bool, str | None] = (True, None),
        row_count: int = 1,
    ) -> None:
        self._validation = validation
        self._row_count = row_count

    async def execute(self, sql, limit=10_000, user_token=None):
        return QueryResult(
            columns=["n"],
            rows=[{"n": i} for i in range(self._row_count)],
            row_count=self._row_count,
            truncated=False,
            duration_ms=1,
        )

    async def validate(self, sql, user_token=None):
        return self._validation


class _PipelineLLM(LLMProvider):
    """Drives engine.chat — returns scripted intent JSON, sql, and viz_summary."""

    def __init__(self, generated_sql: str) -> None:
        self._intent = json.dumps(
            {
                "intent": "sql_query",
                "relevant_tables": ["main.x.t"],
                "relevant_snippets": [],
                "confidence": 0.95,
                "reasoning": "ok",
            }
        )
        self._sql = generated_sql

    async def complete(self, messages, temperature=0.0, max_tokens=2048, task="sql", model=None):
        if task == "intent":
            return LLMResponse(content=self._intent, usage={}, raw=None)
        if task == "planning":
            return LLMResponse(content=_DEFAULT_PLANNING_JSON, usage={}, raw=None)
        if task == "sql":
            return LLMResponse(content=self._sql, usage={}, raw=None)
        if task == "viz_summary":
            return LLMResponse(content="summary", usage={}, raw=None)
        if task == "synthesis":
            return LLMResponse(content=_DEFAULT_SYNTHESIS_JSON, usage={}, raw=None)
        return LLMResponse(content="", usage={}, raw=None)

    async def stream(self, messages, temperature=0.0, task="sql", model=None) -> AsyncIterator[str]:
        yield ""

    async def embed(self, texts):
        return [[0.0] for _ in texts]


def _seed_room_with_benchmarks(
    store: _Store, benchmarks: list[Benchmark]
) -> None:
    config = RoomConfig(
        room_id="r1",
        title="r",
        tables=["main.x.t"],
        warehouse_id="wh",
        benchmarks=benchmarks,
    )
    store._data["room:r1:config"] = json.loads(
        json.dumps(asdict(config))
    )


def _build_engine(llm: LLMProvider, store: _Store, query: QueryProvider) -> RoomEngine:
    return RoomEngine(
        llm=llm,
        catalog=_Catalog(),
        metadata_providers=[],
        query=query,
        vector=_Vector(),
        store=store,
    )


@pytest.mark.asyncio
async def test_benchmark_matching_sql_passes() -> None:
    """Case 6."""
    store = _Store()
    bench = Benchmark(
        question="how many?",
        expected_sql="SELECT count(*) FROM t",
        id="b1",
    )
    _seed_room_with_benchmarks(store, [bench])
    engine = _build_engine(
        _PipelineLLM(generated_sql="select count(*) from t"),
        store,
        _Query(),
    )
    runner = BenchmarkRunner(engine=engine)
    report = await runner.run("r1")
    assert report.total == 1
    assert report.passed == 1
    assert report.score == 1.0
    assert report.results[0].sql_match is True


@pytest.mark.asyncio
async def test_benchmark_non_matching_sql_fails_does_not_raise() -> None:
    """Case 7."""
    store = _Store()
    bench = Benchmark(
        question="how many?",
        expected_sql="SELECT count(*) FROM t",
        id="b1",
    )
    _seed_room_with_benchmarks(store, [bench])
    engine = _build_engine(
        _PipelineLLM(generated_sql="SELECT DISTINCT id FROM t"),
        store,
        _Query(),
    )
    runner = BenchmarkRunner(engine=engine)
    report = await runner.run("r1")
    assert report.passed == 0
    assert report.failed == 1
    assert report.results[0].sql_match is False
    assert report.results[0].passed is False


@pytest.mark.asyncio
async def test_benchmark_pipeline_error_recorded_and_other_continue() -> None:
    """Case 8."""
    store = _Store()
    benchmarks = [
        Benchmark(
            question="q1",
            expected_sql="SELECT 1",
            id="b1",
        ),
        Benchmark(
            question="q2",
            expected_sql="SELECT 1",
            id="b2",
        ),
    ]
    _seed_room_with_benchmarks(store, benchmarks)
    engine = _build_engine(
        _PipelineLLM(generated_sql="SELECT 1"),
        store,
        # First validate fails; second succeeds — but our _Query is a single
        # fixed validation. Instead, simulate pipeline error by raising in execute.
        _Query(),
    )

    # Patch execute to fail once, succeed thereafter.
    original_execute = engine._query.execute
    call_index = {"n": 0}

    async def maybe_failing_execute(sql, limit=10_000, user_token=None):
        call_index["n"] += 1
        if call_index["n"] == 1:
            raise RuntimeError("boom")
        return await original_execute(sql, limit, user_token)

    engine._query.execute = maybe_failing_execute  # type: ignore[assignment]

    runner = BenchmarkRunner(engine=engine)
    report = await runner.run("r1")
    assert report.total == 2
    # First benchmark errored, second succeeded → 1 passed.
    assert report.results[0].error is not None
    assert report.results[1].passed is True


@pytest.mark.asyncio
async def test_benchmark_report_score_is_passed_over_total() -> None:
    """Case 10."""
    store = _Store()
    benchmarks = [
        Benchmark(question="q1", expected_sql="SELECT 1", id="b1"),
        Benchmark(question="q2", expected_sql="SELECT 2", id="b2"),
        Benchmark(question="q3", expected_sql="SELECT 3", id="b3"),
    ]
    _seed_room_with_benchmarks(store, benchmarks)
    # Generated SQL matches b1 and b2 but not b3 (different number).
    engine = _build_engine(_PipelineLLM(generated_sql="SELECT 1"), store, _Query())
    runner = BenchmarkRunner(engine=engine)
    report = await runner.run("r1")
    # Only b1 matches verbatim; b2 and b3 don't.
    assert report.passed == 1
    assert report.failed == 2
    assert report.score == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_benchmark_row_count_match_passes_when_sql_does_not() -> None:
    """When sql_match is False but row counts match, passed is True."""
    store = _Store()
    bench = Benchmark(
        question="q",
        expected_sql="SELECT count(*) FROM t",
        expected_row_count=5,
        id="b1",
    )
    _seed_room_with_benchmarks(store, [bench])
    engine = _build_engine(
        _PipelineLLM(generated_sql="SELECT n FROM t LIMIT 1"),
        store,
        _Query(row_count=5),  # same row count both queries
    )
    runner = BenchmarkRunner(engine=engine, store_query=engine._query)
    report = await runner.run("r1")
    assert report.results[0].sql_match is False
    assert report.results[0].result_match is True
    assert report.results[0].passed is True
