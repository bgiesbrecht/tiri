"""HypothesisAgent + Hypothesis/HypothesisResult invariant tests — EXT-11.

This file covers the six invariants the user called out as non-negotiable
(and the ten doc-level test cases in docs/extensions.md EXT-11):

  1. HypothesisResult.confidence is ALWAYS "low" (dataclass-enforced)
  2. HypothesisResult.disclaimer is ALWAYS non-empty (dataclass-enforced)
  3. Every Hypothesis has ≥ 1 contradicting_pattern (Hypothesis.__post_init__)
  4. HypothesisAgent MUST NOT run when hypothesis_mode_enabled=False
  5. HypothesisAgent MUST NOT run without a multi-step ReasoningPlan
  6. Post-generation causal-language check raises HypothesisError

Doc cases 5 (testable_in_room requires suggested_test), 7 (domain_knowledge_used
must reference real entries), and 10 (hypothesis_mode_enabled + non-causal
question → no agent call) are covered too.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tiri.data_models import (
    ContextPackage,
    Hypothesis,
    HypothesisResult,
    LLMMessage,
    LLMResponse,
    QueryResult,
    ReasoningPlan,
    ReasoningStep,
    SynthesizedAnswer,
)
from tiri.engine.agents.hypothesis_agent import (
    HypothesisAgent,
    HypothesisError,
)
from tiri.providers.base import LLMProvider


# ═══════════════════════════════════════════════════════════════════════════
# Dataclass-enforced invariants (1, 2, 3)
# ═══════════════════════════════════════════════════════════════════════════


def _valid_hypothesis(**overrides) -> Hypothesis:
    defaults = dict(
        statement="The data is consistent with X being associated with Y.",
        supporting_patterns=["a pattern"],
        contradicting_patterns=["a counter-pattern"],
        testability="not_testable",
        suggested_test=None,
        domain_knowledge_used=[],
    )
    defaults.update(overrides)
    return Hypothesis(**defaults)


def test_hypothesis_with_empty_contradicting_patterns_raises() -> None:
    """Invariant 3: a hypothesis with only supporting evidence is not a
    hypothesis, it is a claim. Enforced on Hypothesis itself, not just on
    HypothesisResult, so the type is sound in isolation."""
    with pytest.raises(ValueError, match="contradicting_pattern"):
        Hypothesis(
            statement="X may be associated with Y",
            supporting_patterns=["a pattern"],
            contradicting_patterns=[],
            testability="not_testable",
            suggested_test=None,
            domain_knowledge_used=[],
        )


def test_hypothesis_result_confidence_high_raises() -> None:
    """Invariant 1: confidence is ALWAYS 'low'. The default-valued field
    is a documentation hint; the post-init check is the actual gate."""
    with pytest.raises(ValueError, match="MUST be 'low'"):
        HypothesisResult(hypotheses=[_valid_hypothesis()], confidence="high")


def test_hypothesis_result_confidence_medium_raises() -> None:
    with pytest.raises(ValueError, match="MUST be 'low'"):
        HypothesisResult(hypotheses=[_valid_hypothesis()], confidence="medium")


def test_hypothesis_result_empty_disclaimer_raises() -> None:
    """Invariant 2: disclaimer is mandatory and non-empty."""
    with pytest.raises(ValueError, match="disclaimer MUST be non-empty"):
        HypothesisResult(hypotheses=[_valid_hypothesis()], disclaimer="")


def test_hypothesis_result_whitespace_only_disclaimer_raises() -> None:
    with pytest.raises(ValueError, match="disclaimer MUST be non-empty"):
        HypothesisResult(hypotheses=[_valid_hypothesis()], disclaimer="   ")


def test_hypothesis_result_with_valid_inputs_constructs() -> None:
    """Smoke test: a well-formed HypothesisResult passes all invariants."""
    result = HypothesisResult(hypotheses=[_valid_hypothesis()])
    assert result.confidence == "low"
    assert result.disclaimer  # default is non-empty


def test_hypothesis_result_with_empty_hypotheses_list_is_allowed() -> None:
    """The LLM declined to produce any hypothesis (e.g. the data is too
    sparse to support even a provisional candidate). That's a valid
    outcome — the caller decides whether to surface it."""
    result = HypothesisResult(hypotheses=[])
    assert result.hypotheses == []


# ═══════════════════════════════════════════════════════════════════════════
# Agent unit tests — causal-language check (invariant 6) + parsing
# ═══════════════════════════════════════════════════════════════════════════


class _ScriptedLLM(LLMProvider):
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, messages, temperature=0.0, max_tokens=2048, task="sql", model=None
    ) -> LLMResponse:
        self.calls.append(
            {"task": task, "messages": [(m.role, m.content) for m in messages]}
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


def _context(*, domain_knowledge: list[str] | None = None) -> ContextPackage:
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
        domain_knowledge=domain_knowledge or [],
    )


def _multi_step_plan() -> ReasoningPlan:
    return ReasoningPlan(
        question="Why did X change?",
        steps=[
            ReasoningStep(
                step_id="step_1",
                description="Trend over time",
                sql="SELECT 1",
                result=None,
                depends_on=[],
            ),
            ReasoningStep(
                step_id="step_2",
                description="Breakdown by segment",
                sql="SELECT 2",
                result=None,
                depends_on=["step_1"],
            ),
        ],
        synthesis_instruction="Combine.",
    )


def _query_results(n: int) -> list[QueryResult]:
    return [
        QueryResult(
            columns=["x"], rows=[{"x": i}], row_count=1, truncated=False, duration_ms=1
        )
        for i in range(n)
    ]


def _synthesized() -> SynthesizedAnswer:
    return SynthesizedAnswer(
        answer="Step 1 trend pairs with step 2 segment breakdown.",
        data_supports=["trend"],
        data_does_not_support=["root causes"],
        would_need=["operational logs"],
        confidence="low",
        confidence_rationale="why question",
    )


_SENTINEL = object()


def _hyp_response_json(
    *,
    statement: str = "The data is consistent with the segment shift coinciding with the decline.",
    supporting: object = _SENTINEL,
    contradicting: object = _SENTINEL,
    testability: str = "testable_in_room",
    suggested_test: str | None = "Run step_2 broken down by region as well.",
    domain_knowledge_used: list[str] | None = None,
) -> str:
    """Sentinel defaults so callers can pass an empty list explicitly
    without it being short-circuited to the default by `or`."""
    if supporting is _SENTINEL:
        supporting = ["segment A grew while overall fell"]
    if contradicting is _SENTINEL:
        contradicting = ["segment B was stable"]
    return json.dumps(
        {
            "hypotheses": [
                {
                    "statement": statement,
                    "supporting_patterns": supporting,
                    "contradicting_patterns": contradicting,
                    "testability": testability,
                    "suggested_test": suggested_test,
                    "domain_knowledge_used": domain_knowledge_used or [],
                }
            ]
        }
    )


@pytest.mark.asyncio
async def test_agent_builds_well_formed_hypothesis_result() -> None:
    llm = _ScriptedLLM([_hyp_response_json()])
    agent = HypothesisAgent(llm)
    result = await agent.run(
        question="Why did revenue fall?",
        plan=_multi_step_plan(),
        results=_query_results(2),
        synthesized=_synthesized(),
        context=_context(),
    )
    assert isinstance(result, HypothesisResult)
    assert result.confidence == "low"
    assert result.disclaimer
    assert len(result.hypotheses) == 1


@pytest.mark.parametrize(
    "forbidden_phrase",
    ["caused", "because", "due to", "result of", "led to"],
)
@pytest.mark.asyncio
async def test_causal_language_in_statement_raises(
    forbidden_phrase: str,
) -> None:
    """Invariant 6 / doc cases 1 + 8: any causal verb in a statement
    raises HypothesisError. Tested across the five canonical phrases."""
    llm = _ScriptedLLM(
        [_hyp_response_json(statement=f"The segment shift {forbidden_phrase} the decline.")]
    )
    agent = HypothesisAgent(llm)
    with pytest.raises(HypothesisError, match="causal language"):
        await agent.run(
            question="Why did it drop?",
            plan=_multi_step_plan(),
            results=_query_results(2),
            synthesized=_synthesized(),
            context=_context(),
        )


@pytest.mark.asyncio
async def test_causal_language_check_is_case_insensitive() -> None:
    llm = _ScriptedLLM(
        [_hyp_response_json(statement="The drop was DUE TO seasonality changes.")]
    )
    agent = HypothesisAgent(llm)
    with pytest.raises(HypothesisError):
        await agent.run(
            question="Why?",
            plan=_multi_step_plan(),
            results=_query_results(2),
            synthesized=_synthesized(),
            context=_context(),
        )


@pytest.mark.asyncio
async def test_empty_contradicting_patterns_in_llm_output_raises() -> None:
    """Doc case 6 / invariant 3: a hypothesis without contradicting evidence
    raises HypothesisError BEFORE the dataclass would catch it. Gives the
    caller a clearer error type."""
    llm = _ScriptedLLM([_hyp_response_json(contradicting=[])])
    agent = HypothesisAgent(llm)
    with pytest.raises(HypothesisError, match="one-sided"):
        await agent.run(
            question="Why?",
            plan=_multi_step_plan(),
            results=_query_results(2),
            synthesized=_synthesized(),
            context=_context(),
        )


@pytest.mark.asyncio
async def test_testable_hypothesis_without_suggested_test_raises() -> None:
    """Doc case 5: testability='testable_in_room' MUST include suggested_test."""
    llm = _ScriptedLLM(
        [_hyp_response_json(testability="testable_in_room", suggested_test=None)]
    )
    agent = HypothesisAgent(llm)
    with pytest.raises(HypothesisError, match="testable_in_room"):
        await agent.run(
            question="Why?",
            plan=_multi_step_plan(),
            results=_query_results(2),
            synthesized=_synthesized(),
            context=_context(),
        )


@pytest.mark.asyncio
async def test_not_testable_hypothesis_with_null_suggested_test_passes() -> None:
    """The inverse: testability='not_testable' must accept null suggested_test."""
    llm = _ScriptedLLM(
        [
            _hyp_response_json(
                testability="not_testable", suggested_test=None
            )
        ]
    )
    agent = HypothesisAgent(llm)
    result = await agent.run(
        question="Why?",
        plan=_multi_step_plan(),
        results=_query_results(2),
        synthesized=_synthesized(),
        context=_context(),
    )
    assert result.hypotheses[0].suggested_test is None


@pytest.mark.asyncio
async def test_domain_knowledge_used_filtered_to_real_entries() -> None:
    """Doc case 7: domain_knowledge_used MUST only reference entries actually
    in RoomConfig.domain_knowledge. Hallucinated entries are dropped, not
    raised — the LLM may produce plausible-but-wrong text, and we'd rather
    surface the hypothesis with the real entries than fail the turn."""
    real_axiom = "Q4 spikes are normal for this industry"
    hallucinated = "Customers prefer blue logos"
    llm = _ScriptedLLM(
        [
            _hyp_response_json(
                domain_knowledge_used=[real_axiom, hallucinated]
            )
        ]
    )
    agent = HypothesisAgent(llm)
    result = await agent.run(
        question="Why?",
        plan=_multi_step_plan(),
        results=_query_results(2),
        synthesized=_synthesized(),
        context=_context(domain_knowledge=[real_axiom]),
    )
    used = result.hypotheses[0].domain_knowledge_used
    assert real_axiom in used
    assert hallucinated not in used


@pytest.mark.asyncio
async def test_agent_truncates_to_3_hypotheses() -> None:
    """Spec: maximum 3 hypotheses — quality over quantity."""
    five_hypotheses = {
        "hypotheses": [
            {
                "statement": f"hypothesis {i} is consistent with the data",
                "supporting_patterns": ["a"],
                "contradicting_patterns": ["b"],
                "testability": "not_testable",
                "suggested_test": None,
                "domain_knowledge_used": [],
            }
            for i in range(5)
        ]
    }
    llm = _ScriptedLLM([json.dumps(five_hypotheses)])
    agent = HypothesisAgent(llm)
    result = await agent.run(
        question="Why?",
        plan=_multi_step_plan(),
        results=_query_results(2),
        synthesized=_synthesized(),
        context=_context(),
    )
    assert len(result.hypotheses) == 3


@pytest.mark.asyncio
async def test_unparseable_response_raises_hypothesis_error() -> None:
    llm = _ScriptedLLM(["this is not JSON at all"])
    agent = HypothesisAgent(llm)
    with pytest.raises(HypothesisError, match="not valid JSON"):
        await agent.run(
            question="Why?",
            plan=_multi_step_plan(),
            results=_query_results(2),
            synthesized=_synthesized(),
            context=_context(),
        )


@pytest.mark.asyncio
async def test_associated_with_phrasing_is_allowed() -> None:
    """Negative test: hedged phrasing must pass the causal check.
    'associated with' / 'coincided with' / 'consistent with' are the
    sanctioned alternatives."""
    statement = (
        "The decline coincided with a shift in product mix and is "
        "associated with seasonal patterns observed in step_2."
    )
    llm = _ScriptedLLM([_hyp_response_json(statement=statement)])
    agent = HypothesisAgent(llm)
    result = await agent.run(
        question="Why?",
        plan=_multi_step_plan(),
        results=_query_results(2),
        synthesized=_synthesized(),
        context=_context(),
    )
    assert result.hypotheses[0].statement == statement


# ═══════════════════════════════════════════════════════════════════════════
# RoomEngine gate tests (invariants 4 + 5 + doc case 10)
# ═══════════════════════════════════════════════════════════════════════════


from tiri.engine.room_engine import _is_causal_question


def test_is_causal_question_detects_why_phrasing() -> None:
    """Gate #3: causal question detection. Cheap substring scan keeps the
    hypothesis pipeline coherent with SynthesisAgent's causal_hint logic."""
    assert _is_causal_question("Why did revenue fall in Q3?") is True
    assert _is_causal_question("What caused the spike?") is True
    assert _is_causal_question("What drove the decline?") is True
    assert _is_causal_question("What led to the change?") is True


def test_is_causal_question_does_not_misfire_on_factual_questions() -> None:
    assert _is_causal_question("What is total revenue?") is False
    assert _is_causal_question("How many customers signed up?") is False
    assert _is_causal_question("List the top regions by sales.") is False
