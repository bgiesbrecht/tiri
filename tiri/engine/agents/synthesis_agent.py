"""SynthesisAgent — produces an explicit-uncertainty answer (EXT-7 + EXT-1).

Every SynthesizedAnswer states what the data supports, what it does NOT
support, what additional data would be needed, and a confidence level with
a one-sentence rationale.

Causal language is forbidden in the `answer` field. The prompt asks for this
but does not guarantee it — a post-generation scan enforces the invariant
structurally and raises `SynthesisError` on violation. This is the most
load-bearing correctness rule in Tiri (see vision.md, CLAUDE.md rule #6).

EXT-1: synthesize() accepts a ReasoningPlan + list of QueryResults (one per
step). For one-step plans the prompt collapses to a single (sql, result)
pair so the behavior matches the pre-EXT-1 single-query path. For
multi-step plans the prompt enumerates each step's sql/result and asks the
LLM to combine them per `plan.synthesis_instruction`.
"""

from __future__ import annotations

import json
import logging
import re

from tiri.data_models import (
    ContextPackage,
    LLMMessage,
    QueryResult,
    ReasoningPlan,
    ReasoningStep,
    SynthesizedAnswer,
)
from tiri.engine.agents.base import format_mcp_context, load_template, render
from tiri.providers.base import LLMProvider, LLMProviderError


_TEMPLATE = load_template("synthesis.txt")
_log = logging.getLogger("tiri.engine.agents.synthesis")

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})

_CAUSAL_PHRASES: tuple[str, ...] = (
    "caused by",
    "because of",
    "due to",
    "result of",
    "led to",
)

_CAUSAL_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _CAUSAL_PHRASES) + r")\b",
    re.IGNORECASE,
)

_WHY_HINT_WORDS = ("why", "what caused", "what led to", "what drove")


class SynthesisError(Exception):
    """Raised when SynthesisAgent output violates the causal-language ban,
    or when the LLM response cannot be parsed into a SynthesizedAnswer."""


class SynthesisAgent:
    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def synthesize(
        self,
        question: str,
        plan: ReasoningPlan,
        results: list[QueryResult],
        context: ContextPackage,
    ) -> SynthesizedAnswer:
        """Combine the results of every step in `plan` into a single answer.

        `results[i]` corresponds to `plan.steps[i]`. The caller MUST have
        populated `plan.steps[i].sql` and `plan.steps[i].result` for each
        step before calling — those fields drive the prompt rendering.
        """
        if len(results) != len(plan.steps):
            raise SynthesisError(
                f"synthesize() got {len(results)} results for "
                f"{len(plan.steps)} steps"
            )
        prompt = render(
            _TEMPLATE,
            question=question,
            plan_summary=_format_plan_summary(plan),
            results_summary=_format_results_summary(plan.steps, results),
            synthesis_instruction=plan.synthesis_instruction or "(none)",
            mcp_context=format_mcp_context(context.mcp_context),
            causal_hint=_causal_hint(question),
        )
        response = await self._llm.complete(
            [LLMMessage(role="system", content=prompt)],
            task="synthesis",
        )
        raw = _parse_json_response(response.content)
        answer = _build_synthesized_answer(raw)
        _enforce_no_causal_language(answer.answer)
        return answer


# ── prompt formatting helpers ─────────────────────────────────────────────


def _format_rows_preview(result: QueryResult) -> str:
    rows = result.rows[:10]
    if not rows:
        return "(empty result)"
    return "\n".join(json.dumps(r, default=str) for r in rows)


def _format_plan_summary(plan: ReasoningPlan) -> str:
    if len(plan.steps) == 1:
        step = plan.steps[0]
        return f"Single-step plan: {step.description}"
    lines = [f"{len(plan.steps)}-step plan:"]
    for step in plan.steps:
        deps = (
            f"  (depends on: {', '.join(step.depends_on)})"
            if step.depends_on
            else ""
        )
        lines.append(f"  - {step.step_id}: {step.description}{deps}")
    return "\n".join(lines)


def _format_results_summary(
    steps: list[ReasoningStep], results: list[QueryResult]
) -> str:
    blocks: list[str] = []
    for step, result in zip(steps, results):
        block = [
            f"### {step.step_id} — {step.description}",
            f"SQL: {step.sql or '(missing)'}",
            f"Columns: {', '.join(result.columns)}",
            f"Row count: {result.row_count}",
            "Top rows:",
            _format_rows_preview(result),
        ]
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def _causal_hint(question: str) -> str:
    """If the question phrasing implies causation, push the LLM toward `low`
    confidence by surfacing that hint explicitly."""
    q = question.lower()
    if any(q.startswith(w) or w in q for w in _WHY_HINT_WORDS):
        return (
            "NOTE: The question phrasing implies causation — confidence MUST "
            "be 'low' and `data_does_not_support` MUST cite that root causes "
            "are not determinable from this data alone."
        )
    return ""


# ── parsing / validation ──────────────────────────────────────────────────


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
            raise SynthesisError(
                f"SynthesisAgent response is not valid JSON: {content!r}"
            )
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise SynthesisError(
                f"SynthesisAgent response is not valid JSON: {content!r}"
            ) from e


def _build_synthesized_answer(raw: dict) -> SynthesizedAnswer:
    answer = str(raw.get("answer") or "").strip()
    if not answer:
        raise SynthesisError("SynthesisAgent response missing `answer`")

    data_supports = _as_str_list(raw.get("data_supports"))
    data_does_not_support = _as_str_list(raw.get("data_does_not_support"))
    would_need = _as_str_list(raw.get("would_need"))

    confidence = str(raw.get("confidence") or "").strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        _log.warning(
            "SynthesisAgent returned unknown confidence %r; defaulting to 'medium'",
            confidence,
        )
        confidence = "medium"

    rationale = str(raw.get("confidence_rationale") or "").strip()

    return SynthesizedAnswer(
        answer=answer,
        data_supports=data_supports,
        data_does_not_support=data_does_not_support,
        would_need=would_need,
        confidence=confidence,
        confidence_rationale=rationale,
    )


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _enforce_no_causal_language(answer: str) -> None:
    """CLAUDE.md rule #6, vision.md core invariant. MUST raise if any forbidden
    causal phrase appears in the synthesized answer prose."""
    match = _CAUSAL_PATTERN.search(answer)
    if match:
        raise SynthesisError(
            f"SynthesisAgent produced forbidden causal language "
            f"({match.group(0)!r}) in answer: {answer!r}"
        )
