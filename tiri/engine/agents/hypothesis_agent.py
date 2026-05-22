"""HypothesisAgent — generates candidate explanations for observed patterns.

EXT-11. The most carefully bounded agent in the system.

The six invariants (from vision.md / CLAUDE.md rule #6):
  1. HypothesisResult.confidence is ALWAYS "low" — enforced by the dataclass.
  2. HypothesisResult.disclaimer is ALWAYS non-empty — enforced by the dataclass.
  3. Every Hypothesis MUST have ≥ 1 contradicting_pattern — enforced by
     Hypothesis.__post_init__.
  4. The agent MUST NOT run when RoomConfig.hypothesis_mode_enabled=False.
     Gate enforced by RoomEngine, not the agent itself.
  5. The agent MUST NOT run without a multi-step ReasoningPlan from EXT-1.
     Single-query turns have nothing to reason over. Gate enforced by
     RoomEngine.
  6. Every Hypothesis.statement is scanned post-generation for causal
     verbs ("caused", "because", "due to", "result of", "led to"). A
     violation raises HypothesisError — enforced HERE.

Invariants 1, 2, 3 are dataclass-level: even if the agent emits a bad
JSON payload, attempting to construct the typed result will raise. The
agent's post-validation focuses on what dataclasses cannot enforce — the
causal-language ban and domain_knowledge_used grounding.
"""

from __future__ import annotations

import json
import logging
import re

from tiri.data_models import (
    ContextPackage,
    Hypothesis,
    HypothesisResult,
    LLMMessage,
    QueryResult,
    ReasoningPlan,
    SynthesizedAnswer,
)
from tiri.engine.agents.base import load_template, render
from tiri.providers.base import LLMProvider


_TEMPLATE = load_template("hypothesis_generation.txt")
_log = logging.getLogger("tiri.engine.agents.hypothesis")

_MAX_HYPOTHESES = 3
_VALID_TESTABILITY = frozenset(
    {"testable_in_room", "requires_external_data", "not_testable"}
)

_CAUSAL_PHRASES: tuple[str, ...] = (
    "caused",
    "because",
    "due to",
    "result of",
    "led to",
    # "explains" / "drove" / "produced" are in the prompt's prohibition list
    # too, but the doc-level "test failure on any of these five" is what we
    # enforce structurally here.
)

_CAUSAL_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _CAUSAL_PHRASES) + r")\b",
    re.IGNORECASE,
)


class HypothesisError(Exception):
    """Raised when HypothesisAgent output violates the causal-language ban
    or cannot be coerced into a HypothesisResult."""


class HypothesisAgent:
    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def run(
        self,
        question: str,
        plan: ReasoningPlan,
        results: list[QueryResult],
        synthesized: SynthesizedAnswer,
        context: ContextPackage,
    ) -> HypothesisResult:
        """Generate hypotheses from the completed multi-query reasoning.

        Preconditions enforced by the caller (RoomEngine), NOT here:
          - RoomConfig.hypothesis_mode_enabled is True
          - len(plan.steps) > 1  (must have multi-query evidence)
          - The question is a causal/why question

        The agent assumes these gates have passed.
        """
        prompt = render(
            _TEMPLATE,
            question=question,
            synthesized_answer=synthesized.answer,
            pattern_summary=_format_pattern_summary(plan, results),
            domain_knowledge=_format_domain_knowledge(context.domain_knowledge),
        )
        response = await self._llm.complete(
            [LLMMessage(role="system", content=prompt)],
            task="synthesis",  # routes via the same backend as SynthesisAgent;
                               # there's no dedicated "hypothesis" task in RoutingConfig
                               # — both are deep-reasoning calls and share the model.
        )
        raw = _parse_json_response(response.content)
        hypotheses = _build_hypotheses(raw, context.domain_knowledge)
        for h in hypotheses:
            _enforce_no_causal_language(h.statement)
        # HypothesisResult.__post_init__ enforces invariants 1 and 2 here.
        return HypothesisResult(hypotheses=hypotheses)


# ── prompt formatting ──────────────────────────────────────────────────────


def _format_pattern_summary(
    plan: ReasoningPlan, results: list[QueryResult]
) -> str:
    """Compact per-step description + result shape. The synthesized answer
    already carries the prose interpretation — this block gives the
    hypothesis-generation step direct access to the raw evidence."""
    blocks: list[str] = []
    for step, result in zip(plan.steps, results):
        preview = json.dumps(result.rows[:5], default=str)
        blocks.append(
            f"- {step.step_id}: {step.description}\n"
            f"    columns={result.columns}; row_count={result.row_count}\n"
            f"    top rows: {preview}"
        )
    return "\n".join(blocks) if blocks else "(no step results)"


def _format_domain_knowledge(entries: list[str]) -> str:
    if not entries:
        return "(no domain knowledge configured for this room)"
    return "\n".join(f"- {e}" for e in entries)


# ── parsing / validation ───────────────────────────────────────────────────


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
            raise HypothesisError(
                f"HypothesisAgent response is not valid JSON: {content!r}"
            )
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise HypothesisError(
                f"HypothesisAgent response is not valid JSON: {content!r}"
            ) from e


def _build_hypotheses(
    raw: dict, room_domain_knowledge: list[str]
) -> list[Hypothesis]:
    raw_list = raw.get("hypotheses") or []
    if not isinstance(raw_list, list):
        raise HypothesisError(
            "HypothesisAgent response missing `hypotheses` list"
        )
    if not raw_list:
        # The LLM declined to produce hypotheses. That's a valid outcome —
        # but HypothesisResult.__post_init__ will accept an empty list, so
        # let it through. The caller can decide whether to surface it.
        return []
    # Cap at MAX_HYPOTHESES — quality over quantity (per spec).
    raw_list = raw_list[:_MAX_HYPOTHESES]
    room_dk_set = set(room_domain_knowledge)
    hypotheses: list[Hypothesis] = []
    for raw_h in raw_list:
        if not isinstance(raw_h, dict):
            continue
        statement = str(raw_h.get("statement") or "").strip()
        if not statement:
            raise HypothesisError("hypothesis missing `statement` field")

        supporting = _as_str_list(raw_h.get("supporting_patterns"))
        contradicting = _as_str_list(raw_h.get("contradicting_patterns"))
        # Hypothesis.__post_init__ enforces this too, but raising HypothesisError
        # here gives the caller a clearer error type for "the LLM produced an
        # invalid hypothesis" vs "we tried to construct one and the type
        # system caught it".
        if not contradicting:
            raise HypothesisError(
                f"hypothesis {statement!r} has empty contradicting_patterns "
                "— a one-sided hypothesis is a claim, not a hypothesis"
            )

        testability = str(raw_h.get("testability") or "not_testable").strip()
        if testability not in _VALID_TESTABILITY:
            _log.warning(
                "HypothesisAgent: unknown testability %r; defaulting to "
                "not_testable",
                testability,
            )
            testability = "not_testable"

        suggested_test_raw = raw_h.get("suggested_test")
        if suggested_test_raw in ("", None, "null"):
            suggested_test: str | None = None
        else:
            suggested_test = str(suggested_test_raw).strip()
        if testability == "testable_in_room" and not suggested_test:
            # Test case 5: testable hypotheses MUST include a suggested_test.
            raise HypothesisError(
                f"hypothesis {statement!r} is testable_in_room but has no "
                "suggested_test"
            )

        # Test case 7: domain_knowledge_used MUST only reference entries
        # actually in RoomConfig.domain_knowledge. Filter, don't raise —
        # the LLM may invent text that looks plausible.
        dk_used_raw = _as_str_list(raw_h.get("domain_knowledge_used"))
        dk_used = [e for e in dk_used_raw if e in room_dk_set]
        dropped = [e for e in dk_used_raw if e not in room_dk_set]
        if dropped:
            _log.warning(
                "HypothesisAgent: dropped %d domain_knowledge_used entries "
                "not present in RoomConfig.domain_knowledge: %r",
                len(dropped),
                dropped,
            )

        hypotheses.append(
            Hypothesis(
                statement=statement,
                supporting_patterns=supporting,
                contradicting_patterns=contradicting,
                testability=testability,
                suggested_test=suggested_test,
                domain_knowledge_used=dk_used,
            )
        )
    return hypotheses


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _enforce_no_causal_language(statement: str) -> None:
    """EXT-11 invariant #6 (test case 1 + 8). The prompt asks for hedged
    language; this is the structural enforcement. Raises HypothesisError
    on violation — the turn becomes an error rather than shipping a
    causal claim under the "hypothesis" label."""
    match = _CAUSAL_PATTERN.search(statement)
    if match:
        raise HypothesisError(
            f"HypothesisAgent produced forbidden causal language "
            f"({match.group(0)!r}) in hypothesis statement: {statement!r}"
        )
