"""Tests for tiri.engine.agents.* — covers all 17 cases in docs/agents.md."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tiri.data_models import (
    ColumnMeta,
    ContextPackage,
    IntentResult,
    LLMMessage,
    LLMResponse,
    QueryResult,
    SqlSnippet,
    TableMeta,
)
from tiri.engine.agents.clarify_agent import ClarifyAgent
from tiri.engine.agents.intent_agent import IntentAgent
from tiri.engine.agents.sql_agent import SQLAgent
from tiri.engine.agents.viz_agent import VizAgent, classify_column, select_chart_type
from tiri.providers.base import LLMProvider, QueryProvider


# ── Test doubles ────────────────────────────────────────────────────────────


class _ScriptedLLM(LLMProvider):
    """Returns a queue of canned responses; one per complete() call.

    Also records every call's task and the system-message text for assertions.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "task": task,
                "messages": [(m.role, m.content) for m in messages],
                "model": model,
            }
        )
        if self._index >= len(self._responses):
            raise AssertionError(
                f"_ScriptedLLM: out of canned responses (call #{self._index + 1})"
            )
        content = self._responses[self._index]
        self._index += 1
        return LLMResponse(content=content, usage={}, raw=None)

    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        raise AssertionError("stream() should not be called by tested agents")
        yield ""  # unreachable

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("embed() should not be called by tested agents")


class _RecordingQuery(QueryProvider):
    """Returns scripted (is_valid, error) pairs for validate(); fails execute."""

    def __init__(self, validations: list[tuple[bool, str | None]]) -> None:
        self._validations = list(validations)
        self._index = 0
        self.validate_calls: list[str] = []

    async def execute(self, sql, limit=10_000, user_token=None):
        raise AssertionError("Agent must not call execute()")

    async def validate(self, sql, user_token=None) -> tuple[bool, str | None]:
        self.validate_calls.append(sql)
        if self._index >= len(self._validations):
            raise AssertionError(
                f"_RecordingQuery: out of canned validations "
                f"(call #{self._index + 1})"
            )
        result = self._validations[self._index]
        self._index += 1
        return result


# ── Context fixtures ───────────────────────────────────────────────────────


def _table(name: str, columns: list[tuple[str, str, str]]) -> TableMeta:
    """Quick TableMeta builder. Columns: (name, data_type, semantic_type)."""
    return TableMeta(
        full_name=name,
        description=f"{name} table",
        columns=[
            ColumnMeta(name=n, data_type=dt, semantic_type=st)
            for n, dt, st in columns
        ],
    )


def _context(
    *,
    tables: dict[str, TableMeta] | None = None,
    snippets: list[SqlSnippet] | None = None,
    default_filters: list[str] | None = None,
) -> ContextPackage:
    return ContextPackage(
        room_id="r1",
        table_schemas=tables or {},
        joins=[],
        sql_snippets=snippets or [],
        metrics=[],
        text_instruction="",
        default_filters=default_filters or [],
        retrieved_examples=[],
        conversation_history=[],
    )


# ═══════════════════════════════════════════════════════════════════════════
# IntentAgent — cases 1, 2, 3, 4, 5
# ═══════════════════════════════════════════════════════════════════════════


def _intent_json(
    intent: str,
    *,
    relevant_tables: list[str] | None = None,
    relevant_snippets: list[str] | None = None,
    confidence: float = 0.9,
    reasoning: str = "ok",
) -> str:
    return json.dumps(
        {
            "intent": intent,
            "relevant_tables": relevant_tables or [],
            "relevant_snippets": relevant_snippets or [],
            "confidence": confidence,
            "reasoning": reasoning,
        }
    )


@pytest.mark.asyncio
async def test_intent_in_scope_question_returns_sql_query_high_confidence() -> None:
    """Case 1."""
    ctx = _context(
        tables={
            "main.x.orders": _table(
                "main.x.orders", [("id", "BIGINT", "identifier")]
            )
        }
    )
    llm = _ScriptedLLM(
        [_intent_json("sql_query", relevant_tables=["main.x.orders"], confidence=0.92)]
    )
    agent = IntentAgent(llm)
    result = await agent.run("How many orders?", ctx)
    assert result.intent == "sql_query"
    assert result.confidence >= 0.7
    assert result.relevant_tables == ["main.x.orders"]
    assert llm.calls[0]["task"] == "intent"


@pytest.mark.asyncio
async def test_intent_no_relevant_table_returns_out_of_scope() -> None:
    """Case 2."""
    ctx = _context(
        tables={"main.x.orders": _table("main.x.orders", [("id", "BIGINT", "")])}
    )
    llm = _ScriptedLLM([_intent_json("out_of_scope", confidence=0.95)])
    agent = IntentAgent(llm)
    result = await agent.run("What's the weather?", ctx)
    assert result.intent == "out_of_scope"


@pytest.mark.asyncio
async def test_intent_ambiguous_returns_clarify_or_low_confidence() -> None:
    """Case 3."""
    ctx = _context(tables={"main.x.t": _table("main.x.t", [("c", "STRING", "")])})
    llm = _ScriptedLLM([_intent_json("clarify_needed", confidence=0.5)])
    agent = IntentAgent(llm)
    result = await agent.run("show me the data", ctx)
    assert (
        result.intent == "clarify_needed" or result.confidence < 0.7
    ), f"got intent={result.intent} conf={result.confidence}"


@pytest.mark.asyncio
async def test_intent_response_parseable_as_intent_result() -> None:
    """Case 4 — JSON-shaped response cleanly maps to IntentResult fields."""
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("c", "STRING", "")])},
        snippets=[SqlSnippet(display_name="active", sql="active=true", kind="filter")],
    )
    llm = _ScriptedLLM(
        [
            _intent_json(
                "sql_query",
                relevant_tables=["main.x.t"],
                relevant_snippets=["active"],
                confidence=0.8,
                reasoning="matches table",
            )
        ]
    )
    agent = IntentAgent(llm)
    result = await agent.run("count active rows", ctx)
    assert isinstance(result, IntentResult)
    assert [s.display_name for s in result.relevant_snippets] == ["active"]
    assert result.reasoning == "matches table"


@pytest.mark.asyncio
async def test_intent_unknown_snippet_dropped_with_warning(caplog) -> None:
    """Case 5."""
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("c", "STRING", "")])},
        snippets=[SqlSnippet(display_name="known", sql="x=1", kind="filter")],
    )
    llm = _ScriptedLLM(
        [
            _intent_json(
                "sql_query",
                relevant_tables=["main.x.t"],
                relevant_snippets=["known", "ghost"],
                confidence=0.9,
            )
        ]
    )
    agent = IntentAgent(llm)
    with caplog.at_level(logging.WARNING, logger="tiri.engine.agents.intent"):
        result = await agent.run("q", ctx)
    # Only known snippet survives.
    assert [s.display_name for s in result.relevant_snippets] == ["known"]
    # The unknown name produced a warning.
    assert any("ghost" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_intent_handles_fenced_json_response() -> None:
    """Tolerance: models sometimes wrap JSON in ```json fences."""
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("c", "STRING", "")])}
    )
    fenced = "```json\n" + _intent_json("sql_query", confidence=0.9) + "\n```"
    llm = _ScriptedLLM([fenced])
    agent = IntentAgent(llm)
    result = await agent.run("q", ctx)
    assert result.intent == "sql_query"


# ═══════════════════════════════════════════════════════════════════════════
# SQLAgent — cases 6, 7, 8, 9
# ═══════════════════════════════════════════════════════════════════════════


def _intent(tables: list[str], snippets: list[SqlSnippet] | None = None) -> IntentResult:
    return IntentResult(
        intent="sql_query",
        relevant_tables=tables,
        relevant_snippets=snippets or [],
        confidence=0.9,
        reasoning="ok",
    )


@pytest.mark.asyncio
async def test_sql_calls_validate_before_returning() -> None:
    """Case 6."""
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("id", "BIGINT", "")])}
    )
    llm = _ScriptedLLM(["SELECT id FROM main.x.t"])
    query = _RecordingQuery([(True, None)])
    agent = SQLAgent(llm, query)
    result = await agent.run("get ids", ctx, _intent(["main.x.t"]))
    assert result.is_valid is True
    assert result.attempts == 1
    assert query.validate_calls == ["SELECT id FROM main.x.t"]
    assert llm.calls[0]["task"] == "sql"


@pytest.mark.asyncio
async def test_sql_retries_after_validation_error_and_returns_corrected() -> None:
    """Case 7."""
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("id", "BIGINT", "")])}
    )
    llm = _ScriptedLLM(
        [
            "SELECT idd FROM main.x.t",      # typo
            "SELECT id FROM main.x.t",        # corrected
        ]
    )
    query = _RecordingQuery(
        [(False, "Unknown column 'idd'"), (True, None)]
    )
    agent = SQLAgent(llm, query)
    result = await agent.run("get ids", ctx, _intent(["main.x.t"]))
    assert result.is_valid is True
    assert result.sql == "SELECT id FROM main.x.t"
    assert result.attempts == 2
    # The second prompt to the LLM included the validation error as feedback.
    second_call_messages = llm.calls[1]["messages"]
    assert any(
        "Unknown column 'idd'" in content for _role, content in second_call_messages
    )


@pytest.mark.asyncio
async def test_sql_all_retries_fail_returns_is_valid_false_not_raise() -> None:
    """Case 8."""
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("id", "BIGINT", "")])}
    )
    llm = _ScriptedLLM(
        [
            "SELECT broken 1",
            "SELECT still broken 2",
            "SELECT yet broken 3",
        ]
    )
    query = _RecordingQuery(
        [
            (False, "syntax error 1"),
            (False, "syntax error 2"),
            (False, "syntax error 3"),
        ]
    )
    agent = SQLAgent(llm, query, max_retries=3)
    result = await agent.run("q", ctx, _intent(["main.x.t"]))
    assert result.is_valid is False
    assert result.attempts == 3
    assert result.error and "Failed after 3 attempts" in result.error


@pytest.mark.parametrize(
    "fenced,expected",
    [
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("```SQL\nSELECT 1\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        # Multi-line SQL with leading/trailing whitespace inside the fence.
        ("```sql\n  SELECT a, b\n  FROM t\n  ```", "SELECT a, b\n  FROM t"),
        # Already-unfenced SQL passes through unchanged.
        ("SELECT 1", "SELECT 1"),
    ],
)
@pytest.mark.asyncio
async def test_sql_strips_markdown_fences_from_model_output(
    fenced: str, expected: str
) -> None:
    """Open-source models (qwen2.5-coder, codellama, deepseek) routinely
    wrap SQL in ```sql...``` fences despite the 'no markdown fences'
    instruction. Caught during TPC-H benchmark validation against Ollama:
    qwen2.5-coder:14b sent fenced SQL that hit the warehouse as
    `EXPLAIN \\`\\`\\`sql...\\`\\`\\`` and produced PARSE_SYNTAX_ERROR. The
    agent now strips fences before calling validate()."""
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("id", "BIGINT", "")])}
    )
    llm = _ScriptedLLM([fenced])
    query = _RecordingQuery([(True, None)])
    agent = SQLAgent(llm, query)
    result = await agent.run("q", ctx, _intent(["main.x.t"]))
    assert result.is_valid is True
    assert result.sql == expected
    # The validator must have been called with the un-fenced SQL.
    assert query.validate_calls == [expected]


@pytest.mark.asyncio
async def test_sql_cannot_answer_prefix_short_circuits() -> None:
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("id", "BIGINT", "")])}
    )
    llm = _ScriptedLLM(["CANNOT_ANSWER: missing column"])
    query = _RecordingQuery([])  # never called
    agent = SQLAgent(llm, query)
    result = await agent.run("q", ctx, _intent(["main.x.t"]))
    assert result.is_valid is False
    assert result.attempts == 1
    assert query.validate_calls == []
    assert "CANNOT_ANSWER" in (result.error or "")


@pytest.mark.asyncio
async def test_sql_prompt_filters_to_relevant_tables_only() -> None:
    """Case 9 (SHOULD): SQL prompt only includes tables IntentAgent selected."""
    ctx = _context(
        tables={
            "main.x.in_scope": _table("main.x.in_scope", [("a", "STRING", "")]),
            "main.x.out_of_scope": _table(
                "main.x.out_of_scope", [("b", "STRING", "")]
            ),
        }
    )
    llm = _ScriptedLLM(["SELECT a FROM main.x.in_scope"])
    query = _RecordingQuery([(True, None)])
    agent = SQLAgent(llm, query)
    await agent.run("q", ctx, _intent(["main.x.in_scope"]))
    system_msg = llm.calls[0]["messages"][0][1]
    assert "main.x.in_scope" in system_msg
    assert "main.x.out_of_scope" not in system_msg


# ═══════════════════════════════════════════════════════════════════════════
# ClarifyAgent — cases 10, 11
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clarify_returns_non_empty_question() -> None:
    """Case 10."""
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("c", "STRING", "")])}
    )
    llm = _ScriptedLLM(["Did you mean orders or invoices?"])
    agent = ClarifyAgent(llm)
    intent = IntentResult(
        intent="clarify_needed",
        relevant_tables=[],
        relevant_snippets=[],
        confidence=0.5,
        reasoning="ambiguous wording",
    )
    result = await agent.run("show the totals", ctx, intent)
    assert result.question == "Did you mean orders or invoices?"
    assert llm.calls[0]["task"] == "clarify"


@pytest.mark.asyncio
async def test_clarify_response_must_not_contain_sql() -> None:
    """Case 11 — defensive check that the agent doesn't inject SQL."""
    ctx = _context(
        tables={"main.x.t": _table("main.x.t", [("c", "STRING", "")])}
    )
    llm = _ScriptedLLM(["Are you asking about orders or invoices?"])
    agent = ClarifyAgent(llm)
    intent = IntentResult(
        intent="clarify_needed",
        relevant_tables=[],
        relevant_snippets=[],
        confidence=0.5,
        reasoning="ambiguous",
    )
    result = await agent.run("show totals", ctx, intent)
    upper = result.question.upper()
    for keyword in ("SELECT ", "FROM ", "JOIN ", "INSERT ", "UPDATE ", "DELETE "):
        assert keyword not in upper, f"clarification contained SQL keyword {keyword!r}"


# ═══════════════════════════════════════════════════════════════════════════
# VizAgent — cases 12, 13, 14, 15, 16
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_viz_single_numeric_result_returns_counter() -> None:
    """Case 12."""
    ctx = _context()
    result = QueryResult(
        columns=["total"], rows=[{"total": 42}], row_count=1,
        truncated=False, duration_ms=1,
    )
    llm = _ScriptedLLM(["The total is 42."])
    viz = VizAgent(llm)
    out = await viz.run("how many?", result, ctx)
    assert out.chart_type == "counter"
    assert out.vega_lite_spec["$schema"].endswith("v5.json")
    assert out.summary == "The total is 42."


@pytest.mark.asyncio
async def test_viz_date_plus_numeric_returns_line_using_semantic_type() -> None:
    """Case 13."""
    ctx = _context(
        tables={
            "main.x.t": TableMeta(
                full_name="main.x.t",
                columns=[
                    ColumnMeta(name="day", data_type="DATE", semantic_type="date"),
                    ColumnMeta(name="amount", data_type="DECIMAL", semantic_type="currency"),
                ],
            )
        }
    )
    result = QueryResult(
        columns=["day", "amount"],
        rows=[
            {"day": "2026-01-01", "amount": 100},
            {"day": "2026-01-02", "amount": 110},
        ],
        row_count=2, truncated=False, duration_ms=1,
    )
    llm = _ScriptedLLM(["Amounts trended up day-over-day."])
    viz = VizAgent(llm)
    out = await viz.run("trend?", result, ctx)
    assert out.chart_type == "line"
    assert out.vega_lite_spec["encoding"]["x"]["type"] == "temporal"


@pytest.mark.asyncio
async def test_viz_date_classification_uses_semantic_type_not_data() -> None:
    """Case 13 (strong form): even when the data value LOOKS like a number,
    semantic_type='date' wins. Verifies the metadata path is the primary signal."""
    ctx = _context(
        tables={
            "main.x.t": TableMeta(
                full_name="main.x.t",
                columns=[
                    # Data is a numeric epoch but metadata says it's a date.
                    ColumnMeta(name="ts", data_type="BIGINT", semantic_type="date"),
                    ColumnMeta(name="v", data_type="INT", semantic_type="measure"),
                ],
            )
        }
    )
    result = QueryResult(
        columns=["ts", "v"],
        rows=[{"ts": 1700000000, "v": 1}, {"ts": 1700001000, "v": 2}],
        row_count=2, truncated=False, duration_ms=1,
    )
    types = [classify_column("ts", result, ctx), classify_column("v", result, ctx)]
    assert types == ["date", "numeric"]


def test_viz_value_inspection_fallback_for_derived_column() -> None:
    """Case 14: derived column with alias not in context falls back to data."""
    ctx = _context(tables={"main.x.t": _table("main.x.t", [("id", "BIGINT", "")])})
    result = QueryResult(
        columns=["computed_alias"],
        rows=[{"computed_alias": 3.14}, {"computed_alias": 2.71}],
        row_count=2, truncated=False, duration_ms=1,
    )
    # The alias doesn't appear in any table_schemas — fallback inspects rows.
    assert classify_column("computed_alias", result, ctx) == "numeric"


@pytest.mark.asyncio
async def test_viz_vega_lite_spec_is_valid_v5_shape() -> None:
    """Case 15 — minimal shape check: $schema points at v5; mark and encoding present."""
    ctx = _context()
    result = QueryResult(
        columns=["region", "revenue"],
        rows=[{"region": "AMERICA", "revenue": 100}, {"region": "EUROPE", "revenue": 200}],
        row_count=2, truncated=False, duration_ms=1,
    )
    llm = _ScriptedLLM(["Two regions, EUROPE leads."])
    viz = VizAgent(llm)
    out = await viz.run("by region?", result, ctx)
    spec = out.vega_lite_spec
    assert spec["$schema"] == "https://vega.github.io/schema/vega-lite/v5.json"
    assert "data" in spec
    assert "values" in spec["data"]
    assert "mark" in spec
    assert "encoding" in spec
    # Resolve to JSON cleanly.
    json.dumps(spec)


@pytest.mark.asyncio
async def test_viz_never_calls_llm_for_spec_construction() -> None:
    """Case 16 — exactly one LLM call (for the summary), zero for the spec."""
    ctx = _context()
    result = QueryResult(
        columns=["region", "revenue"],
        rows=[{"region": "AMERICA", "revenue": 100}],
        row_count=1, truncated=False, duration_ms=1,
    )
    llm = _ScriptedLLM(["summary"])
    viz = VizAgent(llm)
    await viz.run("q", result, ctx)
    assert len(llm.calls) == 1
    assert llm.calls[0]["task"] == "viz_summary"


@pytest.mark.asyncio
async def test_viz_summary_llm_failure_degrades_to_empty_string() -> None:
    """The viz_summary is decorative — a refusal, guardrail miss, quota
    error, or transient timeout from the LLM MUST NOT crash the whole
    turn. Caught during the TPC-H benchmark validation: Databricks'
    output guardrail on llama-3.1-8b false-flagged a benign supply-chain
    summary prompt as 'indiscriminate-weapons', taking down the whole
    chat() invocation when it should have only dropped the summary."""
    class _RaisingLLM(LLMProvider):
        async def complete(self, *args, **kwargs):
            from tiri.providers.base import LLMProviderError
            raise LLMProviderError("HTTP 400: guardrail triggered")

        async def stream(self, *args, **kwargs):
            yield ""

        async def embed(self, texts):
            return [[0.0] for _ in texts]

    ctx = _context()
    result = QueryResult(
        columns=["region", "revenue"],
        rows=[{"region": "AMERICA", "revenue": 100}],
        row_count=1, truncated=False, duration_ms=1,
    )
    viz = VizAgent(_RaisingLLM())
    out = await viz.run("by region?", result, ctx)
    # Turn-level result still ships — just with an empty summary.
    assert out.summary == ""
    assert out.chart_type  # spec generation is unaffected
    assert out.vega_lite_spec["$schema"].endswith("/v5.json")


def test_viz_chart_selection_rules() -> None:
    """Direct tests of select_chart_type() rule table."""
    assert select_chart_type(["numeric"], row_count=1) == "counter"
    assert select_chart_type(["date", "numeric"], row_count=10) == "line"
    assert (
        select_chart_type(["date", "numeric", "numeric"], row_count=10) == "line"
    )
    assert select_chart_type(["string", "numeric"], row_count=10) == "bar"
    assert select_chart_type(["string", "numeric"], row_count=100) == "table"
    assert select_chart_type(["numeric", "numeric"], row_count=10) == "scatter"
    assert (
        select_chart_type(["string", "string", "numeric"], row_count=10) == "table"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Case 17: agents MUST NOT import SDKs (architectural — covered by the
# config.py-level scan, but assert directly that the agent modules import
# only from data_models and providers.base).
# ═══════════════════════════════════════════════════════════════════════════


def test_agents_only_import_data_models_and_providers_base() -> None:
    """Walk every agent module's source and confirm its imports stay inside
    the engine layer + tiri.data_models + tiri.providers.base. SDK imports
    are caught by the static scan in test_config.py — this test asserts the
    positive boundary."""
    import pathlib
    import re

    agents_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "tiri" / "engine" / "agents"
    allowed_prefixes = (
        "tiri.data_models",
        "tiri.engine.agents",
        "tiri.providers.base",
    )
    import_re = re.compile(
        r"^\s*(?:from\s+(\S+)|import\s+(\S+))", re.MULTILINE
    )
    violations: list[str] = []
    for py in agents_dir.rglob("*.py"):
        if py.name == "__init__.py":
            continue
        for match in import_re.finditer(py.read_text()):
            module = match.group(1) or match.group(2)
            if not module.startswith("tiri"):
                continue  # stdlib / third-party-prefix violations are caught elsewhere
            if not module.startswith(allowed_prefixes):
                violations.append(f"{py.name}: imports {module}")
    assert not violations, (
        "Agents may only import from tiri.data_models, tiri.providers.base, "
        "and tiri.engine.agents; violations:\n  " + "\n  ".join(violations)
    )


def test_agents_do_not_import_each_other() -> None:
    """Agents must share types via tiri.data_models, not by importing each
    other's modules. Cross-agent imports would create coupling that makes
    individual agents hard to test in isolation. `base.py` is the explicit
    exception — it holds shared formatting helpers and template loading."""
    import pathlib
    import re

    agents_dir = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "tiri"
        / "engine"
        / "agents"
    )
    agent_module_names = (
        "intent_agent",
        "sql_agent",
        "clarify_agent",
        "viz_agent",
    )
    violations: list[str] = []
    for py in agents_dir.glob("*.py"):
        if py.name in ("__init__.py", "base.py"):
            continue
        my_stem = py.stem
        text = py.read_text()
        for other in agent_module_names:
            if other == my_stem:
                continue
            pattern = re.compile(
                rf"(?:from\s+tiri\.engine\.agents\.{other}\b|"
                rf"import\s+tiri\.engine\.agents\.{other}\b)"
            )
            if pattern.search(text):
                violations.append(
                    f"{py.name} imports tiri.engine.agents.{other}"
                )
    assert not violations, (
        "Agents must not import each other; share types via tiri.data_models:\n  "
        + "\n  ".join(violations)
    )
