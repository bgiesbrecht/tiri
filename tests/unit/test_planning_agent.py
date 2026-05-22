"""PlanningAgent tests — EXT-1.

Cases mirror docs/extensions.md EXT-1:
  1. Simple aggregation question → one-step plan
  2. "Why" question → multi-step plan with ≥ 2 steps
  6. Any plan → MUST have ≤ 5 steps (truncated with WARNING if model returns more)

Plus the safety tests around malformed responses — the agent never lets
a broken plan stop the pipeline; it logs and falls back to a one-step plan
with the original question as the description.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tiri.data_models import (
    ContextPackage,
    LLMMessage,
    LLMResponse,
    ReasoningPlan,
)
from tiri.engine.agents.planning_agent import (
    PlanningAgent,
    PlanningError,
)
from tiri.providers.base import LLMProvider


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


def _plan_json(
    *,
    requires_multiple: bool,
    steps: list[dict],
    synthesis_instruction: str = "test",
) -> str:
    return json.dumps(
        {
            "requires_multiple_queries": requires_multiple,
            "steps": steps,
            "synthesis_instruction": synthesis_instruction,
        }
    )


# ── Case 1: simple aggregation → one-step plan ────────────────────────────


@pytest.mark.asyncio
async def test_simple_aggregation_returns_one_step_plan() -> None:
    llm = _ScriptedLLM(
        [
            _plan_json(
                requires_multiple=False,
                steps=[
                    {
                        "step_id": "step_1",
                        "description": "Total revenue across all sales rows.",
                        "depends_on": [],
                    }
                ],
                synthesis_instruction="Report the single result directly.",
            )
        ]
    )
    agent = PlanningAgent(llm)
    plan = await agent.plan("What is total revenue?", _context())
    assert len(plan.steps) == 1
    assert plan.steps[0].step_id == "step_1"
    assert plan.steps[0].depends_on == []
    assert llm.calls[0]["task"] == "planning"


# ── Case 2: "why" question → multi-step plan ──────────────────────────────


@pytest.mark.asyncio
async def test_why_question_returns_multi_step_plan() -> None:
    llm = _ScriptedLLM(
        [
            _plan_json(
                requires_multiple=True,
                steps=[
                    {
                        "step_id": "step_1",
                        "description": "Monthly churn rate trend.",
                        "depends_on": [],
                    },
                    {
                        "step_id": "step_2",
                        "description": "Churn rate by customer segment.",
                        "depends_on": ["step_1"],
                    },
                    {
                        "step_id": "step_3",
                        "description": "Renewal patterns for the affected segment.",
                        "depends_on": ["step_2"],
                    },
                ],
                synthesis_instruction=(
                    "Compare segment-level churn against the overall trend to "
                    "show which segments coincide with the change."
                ),
            )
        ]
    )
    agent = PlanningAgent(llm)
    plan = await agent.plan("Why did churn increase last quarter?", _context())
    assert len(plan.steps) >= 2
    assert plan.steps[0].step_id == "step_1"
    # Depends-on resolution: step_2 depends on step_1 (earlier), step_3 on step_2
    assert plan.steps[1].depends_on == ["step_1"]
    assert plan.steps[2].depends_on == ["step_2"]


# ── Case 6: max 5 steps — truncate with warning ───────────────────────────


@pytest.mark.asyncio
async def test_plan_with_more_than_5_steps_is_truncated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    steps_8 = [
        {
            "step_id": f"step_{i + 1}",
            "description": f"step {i + 1} description",
            "depends_on": [],
        }
        for i in range(8)
    ]
    llm = _ScriptedLLM(
        [_plan_json(requires_multiple=True, steps=steps_8)]
    )
    agent = PlanningAgent(llm)
    with caplog.at_level(logging.WARNING, logger="tiri.engine.agents.planning"):
        plan = await agent.plan("complex question", _context())
    assert len(plan.steps) == 5
    assert any("truncating to 5" in r.message for r in caplog.records)


# ── Defensive: malformed response ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_steps_falls_back_to_one_step_plan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = _ScriptedLLM(
        [_plan_json(requires_multiple=False, steps=[])]
    )
    agent = PlanningAgent(llm)
    with caplog.at_level(logging.WARNING, logger="tiri.engine.agents.planning"):
        plan = await agent.plan("What is X?", _context())
    assert len(plan.steps) == 1
    assert plan.steps[0].description == "What is X?"
    assert any("no steps" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_unparseable_response_raises_planning_error() -> None:
    llm = _ScriptedLLM(["this is not JSON"])
    agent = PlanningAgent(llm)
    with pytest.raises(PlanningError, match="not valid JSON"):
        await agent.plan("Q", _context())


@pytest.mark.asyncio
async def test_step_missing_description_falls_back_to_question(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = _ScriptedLLM(
        [
            _plan_json(
                requires_multiple=False,
                steps=[{"step_id": "step_1", "depends_on": []}],
            )
        ]
    )
    agent = PlanningAgent(llm)
    with caplog.at_level(logging.WARNING, logger="tiri.engine.agents.planning"):
        plan = await agent.plan("What is X?", _context())
    assert plan.steps[0].description == "What is X?"
    assert any("missing description" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_forward_reference_in_depends_on_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """depends_on may only reference earlier step_ids. Forward references
    (step_1 depending on step_2) are dropped with a warning — sequential
    execution honors declared order, so a forward edge would be incoherent."""
    llm = _ScriptedLLM(
        [
            _plan_json(
                requires_multiple=True,
                steps=[
                    {
                        "step_id": "step_1",
                        "description": "first",
                        "depends_on": ["step_2"],  # forward — invalid
                    },
                    {
                        "step_id": "step_2",
                        "description": "second",
                        "depends_on": [],
                    },
                ],
            )
        ]
    )
    agent = PlanningAgent(llm)
    with caplog.at_level(logging.WARNING, logger="tiri.engine.agents.planning"):
        plan = await agent.plan("q", _context())
    assert plan.steps[0].depends_on == []
    assert any(
        "not an earlier step" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_json_with_markdown_fences_is_parsed() -> None:
    inner = _plan_json(
        requires_multiple=False,
        steps=[{"step_id": "step_1", "description": "x", "depends_on": []}],
    )
    fenced = "```json\n" + inner + "\n```"
    agent = PlanningAgent(_ScriptedLLM([fenced]))
    plan = await agent.plan("q", _context())
    assert len(plan.steps) == 1
