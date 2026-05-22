"""SynthesisAgent tests — EXT-7 (explicit uncertainty).

Cases mirror the table in docs/extensions.md EXT-7:
  1. Direct aggregation → confidence="high"
  2. "Why" question → confidence="low" + non-empty data_does_not_support
  3. Any answer → MUST NOT contain causal phrases in `answer`
  4. data_does_not_support non-empty for any causal-inference question
  5. would_need MUST suggest concrete additional data sources

Plus the structural invariant: causal language in `answer` raises
SynthesisError, regardless of what the LLM "intended". This is the
non-negotiable correctness rule from vision.md and CLAUDE.md rule #6.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tiri.data_models import (
    ContextPackage,
    LLMMessage,
    LLMResponse,
    QueryResult,
    ReasoningPlan,
    ReasoningStep,
    SqlSnippet,
    TableMeta,
)
from tiri.engine.agents.synthesis_agent import (
    SynthesisAgent,
    SynthesisError,
)
from tiri.providers.base import LLMProvider


def _single_step_plan(question: str, sql: str) -> ReasoningPlan:
    """Wrap a (question, sql) pair as a 1-step ReasoningPlan for tests that
    pre-date EXT-1 — synthesize() expects a plan + matching results list."""
    return ReasoningPlan(
        question=question,
        steps=[
            ReasoningStep(
                step_id="step_1",
                description=question,
                sql=sql,
                result=None,
                depends_on=[],
            )
        ],
        synthesis_instruction="Report the single result directly.",
    )


async def _run(agent, *, question, sql, query_result, context):
    """Compatibility wrapper around the pre-EXT-1 SynthesisAgent.run() API.
    Tests in this file pre-date EXT-1; they pass a single (sql, result)
    pair. Wrap it as a one-step plan and dispatch to synthesize()."""
    plan = _single_step_plan(question, sql)
    return await agent.synthesize(question, plan, [query_result], context)


class _ScriptedLLM(LLMProvider):
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "task": task,
                "messages": [(m.role, m.content) for m in messages],
            }
        )
        content = self._responses[self._index]
        self._index += 1
        return LLMResponse(content=content, usage={}, raw=None)

    async def stream(
        self, messages, temperature=0.0, task="sql", model=None
    ) -> AsyncIterator[str]:
        raise AssertionError("stream() should not be called")
        yield ""

    async def embed(self, texts):
        raise AssertionError("embed() should not be called")


def _context() -> ContextPackage:
    return ContextPackage(
        room_id="r1",
        table_schemas={},
        joins=[],
        sql_snippets=[],
        metrics=[],
        text_instruction="",
        default_filters=[],
        retrieved_examples=[],
        conversation_history=[],
    )


def _result(columns: list[str], rows: list[dict]) -> QueryResult:
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=False,
        duration_ms=10,
    )


def _synthesis_json(
    *,
    answer: str,
    confidence: str = "medium",
    data_supports: list[str] | None = None,
    data_does_not_support: list[str] | None = None,
    would_need: list[str] | None = None,
    confidence_rationale: str = "rationale",
) -> str:
    return json.dumps(
        {
            "answer": answer,
            "data_supports": data_supports or [],
            "data_does_not_support": data_does_not_support or [],
            "would_need": would_need or [],
            "confidence": confidence,
            "confidence_rationale": confidence_rationale,
        }
    )


# ── Test case 1: Direct aggregation → confidence="high" ───────────────────


@pytest.mark.asyncio
async def test_direct_aggregation_returns_high_confidence() -> None:
    llm = _ScriptedLLM(
        [
            _synthesis_json(
                answer="Total revenue across all regions is $1,234,567.",
                confidence="high",
                data_supports=["Sum of revenue column across the result"],
                data_does_not_support=[],
                would_need=[],
                confidence_rationale=(
                    "Single unambiguous aggregation against clean data."
                ),
            )
        ]
    )
    agent = SynthesisAgent(llm)
    result = await _run(agent, 
        question="What is total revenue?",
        sql="SELECT SUM(revenue) FROM main.x.sales",
        query_result=_result(["revenue"], [{"revenue": 1234567}]),
        context=_context(),
    )
    assert result.confidence == "high"
    assert llm.calls[0]["task"] == "synthesis"


# ── Test case 2: "Why" question → confidence="low" + data_does_not_support ─


@pytest.mark.asyncio
async def test_why_question_returns_low_confidence_with_uncertainty() -> None:
    llm = _ScriptedLLM(
        [
            _synthesis_json(
                answer=(
                    "Revenue in Q3 was $500k, lower than Q2's $700k. "
                    "The data shows the decline alongside changes in the "
                    "product mix column."
                ),
                confidence="low",
                data_supports=[
                    "Q2 revenue: $700k",
                    "Q3 revenue: $500k",
                ],
                data_does_not_support=[
                    "Root causes for the revenue decline — this data "
                    "contains no causal signal.",
                ],
                would_need=[
                    "Operational incidents log for Q3",
                    "Marketing campaign records",
                    "Customer churn survey responses",
                ],
                confidence_rationale=(
                    "Question implies causation; data shows correlation only."
                ),
            )
        ]
    )
    agent = SynthesisAgent(llm)
    result = await _run(agent, 
        question="Why did revenue drop in Q3?",
        sql="SELECT quarter, SUM(revenue) FROM main.x.sales GROUP BY quarter",
        query_result=_result(
            ["quarter", "revenue"],
            [{"quarter": "Q2", "revenue": 700000}, {"quarter": "Q3", "revenue": 500000}],
        ),
        context=_context(),
    )
    assert result.confidence == "low"
    assert result.data_does_not_support, (
        "causal-inference question must have non-empty data_does_not_support"
    )


# ── Test case 3: causal language in answer MUST raise ─────────────────────


@pytest.mark.parametrize(
    "forbidden_phrase",
    [
        "caused by",
        "because of",
        "due to",
        "result of",
        "led to",
    ],
)
@pytest.mark.asyncio
async def test_causal_language_in_answer_raises(forbidden_phrase: str) -> None:
    llm = _ScriptedLLM(
        [
            _synthesis_json(
                answer=f"Revenue fell {forbidden_phrase} the new product launch.",
                confidence="low",
            )
        ]
    )
    agent = SynthesisAgent(llm)
    with pytest.raises(SynthesisError, match="causal language"):
        await _run(agent, 
            question="Why did revenue fall?",
            sql="SELECT 1",
            query_result=_result(["x"], [{"x": 1}]),
            context=_context(),
        )


@pytest.mark.asyncio
async def test_causal_language_check_is_case_insensitive() -> None:
    llm = _ScriptedLLM(
        [
            _synthesis_json(
                answer="The drop was DUE TO seasonality.",
                confidence="low",
            )
        ]
    )
    agent = SynthesisAgent(llm)
    with pytest.raises(SynthesisError):
        await _run(agent, 
            question="Why?",
            sql="SELECT 1",
            query_result=_result(["x"], [{"x": 1}]),
            context=_context(),
        )


# ── Test case 4: data_does_not_support non-empty for causal-inference ─────
# Covered by the "why" test above (test_why_question_returns_low_confidence_with_uncertainty);
# the LLM was scripted to emit a non-empty list, and the agent passes it through.
# That structural pass-through is what we're verifying.


# ── Test case 5: would_need MUST be concrete ──────────────────────────────


@pytest.mark.asyncio
async def test_would_need_passes_through_concrete_suggestions() -> None:
    """The agent preserves whatever `would_need` items the LLM produces.
    Whether the items are actually concrete is enforced by the PROMPT
    (synthesis.txt) and the bench harness — the agent's structural job is
    to pass them through as a list of strings, not validate prose quality."""
    concrete = [
        "Salesforce opportunity records for the same period",
        "Marketing email send logs (campaign_id, sent_at, recipient_segment)",
    ]
    llm = _ScriptedLLM(
        [
            _synthesis_json(
                answer="Conversions fell from 4.2% to 2.9%.",
                confidence="medium",
                would_need=concrete,
            )
        ]
    )
    agent = SynthesisAgent(llm)
    result = await _run(agent, 
        question="How did conversion change?",
        sql="SELECT 1",
        query_result=_result(["rate"], [{"rate": 0.029}]),
        context=_context(),
    )
    assert result.would_need == concrete


# ── Edge cases ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_confidence_value_defaults_to_medium() -> None:
    llm = _ScriptedLLM(
        [
            _synthesis_json(
                answer="Revenue total is shown.",
                confidence="extremely-certain",  # not in {high, medium, low}
            )
        ]
    )
    agent = SynthesisAgent(llm)
    result = await _run(agent, 
        question="What is revenue?",
        sql="SELECT SUM(revenue) FROM t",
        query_result=_result(["r"], [{"r": 1}]),
        context=_context(),
    )
    assert result.confidence == "medium"


@pytest.mark.asyncio
async def test_unparseable_response_raises_synthesis_error() -> None:
    llm = _ScriptedLLM(["this is not JSON at all, just prose"])
    agent = SynthesisAgent(llm)
    with pytest.raises(SynthesisError, match="not valid JSON"):
        await _run(agent, 
            question="q",
            sql="SELECT 1",
            query_result=_result(["x"], [{"x": 1}]),
            context=_context(),
        )


@pytest.mark.asyncio
async def test_empty_answer_field_raises() -> None:
    llm = _ScriptedLLM(
        [
            json.dumps(
                {
                    "answer": "",
                    "data_supports": [],
                    "data_does_not_support": [],
                    "would_need": [],
                    "confidence": "low",
                    "confidence_rationale": "x",
                }
            )
        ]
    )
    agent = SynthesisAgent(llm)
    with pytest.raises(SynthesisError, match="missing `answer`"):
        await _run(agent, 
            question="q",
            sql="SELECT 1",
            query_result=_result(["x"], [{"x": 1}]),
            context=_context(),
        )


@pytest.mark.asyncio
async def test_json_with_markdown_fences_is_parsed() -> None:
    """LLMs often wrap JSON in ```json … ``` fences. Tolerate it."""
    inner = _synthesis_json(answer="A direct count.", confidence="high")
    fenced = "```json\n" + inner + "\n```"
    llm = _ScriptedLLM([fenced])
    agent = SynthesisAgent(llm)
    result = await _run(agent, 
        question="How many?",
        sql="SELECT COUNT(*) FROM t",
        query_result=_result(["c"], [{"c": 42}]),
        context=_context(),
    )
    assert result.confidence == "high"
    assert result.answer == "A direct count."


@pytest.mark.asyncio
async def test_why_question_hint_is_injected_into_prompt() -> None:
    """The causal_hint helper surfaces a NOTE for "why" phrasing so the LLM
    is pushed toward low confidence. This is advisory — the structural
    causal-language ban is enforced post-generation."""
    llm = _ScriptedLLM(
        [_synthesis_json(answer="The data shows a decline.", confidence="low")]
    )
    agent = SynthesisAgent(llm)
    await _run(agent, 
        question="Why did revenue fall in Q3?",
        sql="SELECT 1",
        query_result=_result(["x"], [{"x": 1}]),
        context=_context(),
    )
    system_msg = llm.calls[0]["messages"][0][1]
    assert "causation" in system_msg.lower()
    assert "confidence MUST be 'low'" in system_msg


@pytest.mark.asyncio
async def test_associated_with_phrasing_is_allowed() -> None:
    """Negative test: phrases that name co-occurrence (NOT causation) must NOT
    trigger the causal-language check. This protects the agent's ability to
    describe correlations honestly."""
    answer = (
        "Revenue declines coincided with a spike in support tickets and "
        "were associated with a new product launch in the same period."
    )
    llm = _ScriptedLLM([_synthesis_json(answer=answer, confidence="low")])
    agent = SynthesisAgent(llm)
    result = await _run(agent, 
        question="What happened to revenue?",
        sql="SELECT 1",
        query_result=_result(["x"], [{"x": 1}]),
        context=_context(),
    )
    assert result.answer == answer
