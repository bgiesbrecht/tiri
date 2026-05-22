"""PlanningAgent — decomposes a question into a ReasoningPlan (EXT-1).

Most questions return a one-step plan, in which case the rest of the
pipeline behaves identically to the pre-EXT-1 single-query path. The
optimization matters because PlanningAgent runs on every question.

For multi-step plans, the LLM produces an ordered list of step
descriptions with `depends_on` hints. SQL generation is independent
per step (depends_on is a synthesis-ordering hint, not data flow).

Hard cap: 5 steps. The LLM can produce more in theory; the agent
truncates with a logged WARNING. Without this cap, prompt-engineering
failures could produce unbounded query loops.
"""

from __future__ import annotations

import json
import logging
import re

from tiri.data_models import (
    ContextPackage,
    LLMMessage,
    ReasoningPlan,
    ReasoningStep,
)
from tiri.engine.agents.base import (
    format_table_list,
    load_template,
    render,
)
from tiri.providers.base import LLMProvider, LLMProviderError


_TEMPLATE = load_template("planning.txt")
_log = logging.getLogger("tiri.engine.agents.planning")

_MAX_STEPS = 5


class PlanningError(Exception):
    """Raised when the planner's JSON cannot be coerced into a ReasoningPlan."""


class PlanningAgent:
    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def plan(
        self, question: str, context: ContextPackage
    ) -> ReasoningPlan:
        prompt = render(
            _TEMPLATE,
            question=question,
            table_summary=format_table_list(context.table_schemas),
            text_instruction=context.text_instruction or "(none)",
        )
        response = await self._llm.complete(
            [LLMMessage(role="system", content=prompt)],
            task="planning",
        )
        raw = _parse_json_response(response.content)
        return _build_plan(question, raw)


def _parse_json_response(content: str) -> dict:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise PlanningError(
                f"PlanningAgent response is not valid JSON: {content!r}"
            )
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise PlanningError(
                f"PlanningAgent response is not valid JSON: {content!r}"
            ) from e


def _build_plan(question: str, raw: dict) -> ReasoningPlan:
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        # Defensive fallback: degrade to a one-step plan using the original
        # question. Better to run the question through SQLAgent than to fail
        # the whole turn over a malformed plan.
        _log.warning(
            "PlanningAgent returned no steps; falling back to one-step plan"
        )
        return _fallback_single_step(question)

    if len(raw_steps) > _MAX_STEPS:
        _log.warning(
            "PlanningAgent returned %d steps; truncating to %d",
            len(raw_steps),
            _MAX_STEPS,
        )
        raw_steps = raw_steps[:_MAX_STEPS]

    steps: list[ReasoningStep] = []
    seen_ids: set[str] = set()
    for i, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            continue
        step_id = str(raw_step.get("step_id") or f"step_{i + 1}").strip()
        if not step_id or step_id in seen_ids:
            step_id = f"step_{i + 1}"
        seen_ids.add(step_id)
        description = str(raw_step.get("description") or "").strip()
        if not description:
            # A step without a description is useless to SQLAgent — fall back
            # to the original question for this step. Logging makes the
            # degraded behavior visible during prompt iteration.
            _log.warning(
                "PlanningAgent step %s missing description; using question verbatim",
                step_id,
            )
            description = question

        depends_on_raw = raw_step.get("depends_on") or []
        depends_on: list[str] = []
        if isinstance(depends_on_raw, list):
            for d in depends_on_raw:
                if isinstance(d, str) and d in seen_ids and d != step_id:
                    depends_on.append(d)
                elif isinstance(d, str):
                    # Forward references or unknown step_ids are dropped.
                    # Sequential execution honors declared order — depends_on
                    # is advisory at the MVP level.
                    _log.warning(
                        "step %s depends_on %r which is not an earlier step; dropping",
                        step_id,
                        d,
                    )

        steps.append(
            ReasoningStep(
                step_id=step_id,
                description=description,
                sql=None,
                result=None,
                depends_on=depends_on,
            )
        )

    if not steps:
        return _fallback_single_step(question)

    synthesis_instruction = str(
        raw.get("synthesis_instruction") or "Report the single result directly."
    ).strip()

    return ReasoningPlan(
        question=question,
        steps=steps,
        synthesis_instruction=synthesis_instruction,
    )


def _fallback_single_step(question: str) -> ReasoningPlan:
    return ReasoningPlan(
        question=question,
        steps=[
            ReasoningStep(
                step_id="step_1",
                description=question,
                sql=None,
                result=None,
                depends_on=[],
            )
        ],
        synthesis_instruction="Report the single result directly.",
    )
