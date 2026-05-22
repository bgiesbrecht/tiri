"""IntentAgent — classifies the user's question and selects relevant tables/snippets.

Routes the pipeline to SQLAgent (sql_query, high confidence), ClarifyAgent
(clarify_needed or low confidence), or to an error turn (out_of_scope).
"""

from __future__ import annotations

import json
import logging
import re

from tiri.data_models import (
    ContextPackage,
    IntentResult,
    LLMMessage,
    SqlSnippet,
)
from tiri.engine.agents.base import (
    format_mcp_context,
    format_metric_list,
    format_snippet_list,
    format_table_list,
    load_template,
    render,
)
from tiri.providers.base import LLMProvider, LLMProviderError


_TEMPLATE = load_template("intent_classification.txt")
_log = logging.getLogger("tiri.engine.agents.intent")
_VALID_INTENTS = frozenset({"sql_query", "clarify_needed", "out_of_scope"})


class IntentAgent:
    def __init__(
        self, llm: LLMProvider, confidence_threshold: float = 0.7
    ) -> None:
        self._llm = llm
        self._threshold = confidence_threshold

    async def run(
        self, question: str, context: ContextPackage
    ) -> IntentResult:
        prompt = render(
            _TEMPLATE,
            table_list=format_table_list(context.table_schemas),
            snippet_list=format_snippet_list(context.sql_snippets),
            metric_list=format_metric_list(context.metrics),
            text_instruction=context.text_instruction or "(none)",
            mcp_context=format_mcp_context(context.mcp_context),
            question=question,
        )
        response = await self._llm.complete(
            [LLMMessage(role="system", content=prompt)],
            task="intent",
        )
        raw = _parse_json_response(response.content)
        return _build_intent_result(raw, context)


def _parse_json_response(content: str) -> dict:
    """Tolerate models that wrap JSON in fences or trailing prose."""
    stripped = content.strip()
    if stripped.startswith("```"):
        # Strip ```json / ``` fences.
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # Last resort: find the first {...} block.
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise LLMProviderError(
                f"IntentAgent response is not valid JSON: {content!r}"
            )
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise LLMProviderError(
                f"IntentAgent response is not valid JSON: {content!r}"
            ) from e


def _build_intent_result(
    raw: dict, context: ContextPackage
) -> IntentResult:
    intent = raw.get("intent")
    if intent not in _VALID_INTENTS:
        # The model went off the rails — treat as out_of_scope, lowest confidence.
        _log.warning(
            "IntentAgent got unknown intent %r; treating as out_of_scope", intent
        )
        intent = "out_of_scope"

    # `relevant_tables` is returned as a list of full_name strings.
    relevant_tables_raw = raw.get("relevant_tables") or []
    valid_tables = set(context.table_schemas)
    relevant_tables: list[str] = []
    for t in relevant_tables_raw:
        if isinstance(t, str) and t in valid_tables:
            relevant_tables.append(t)
        elif isinstance(t, str):
            _log.warning(
                "IntentAgent referenced unknown table %r; dropping", t
            )

    # `relevant_snippets` is returned as a list of display_name strings.
    snippet_map = {s.display_name: s for s in context.sql_snippets}
    relevant_snippets: list[SqlSnippet] = []
    for name in raw.get("relevant_snippets") or []:
        if not isinstance(name, str):
            continue
        snippet = snippet_map.get(name)
        if snippet is None:
            _log.warning(
                "IntentAgent referenced unknown snippet %r; dropping", name
            )
            continue
        relevant_snippets.append(snippet)

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reasoning = str(raw.get("reasoning") or "")

    return IntentResult(
        intent=intent,
        relevant_tables=relevant_tables,
        relevant_snippets=relevant_snippets,
        confidence=confidence,
        reasoning=reasoning,
        # EXT-2: propagate the selection method set by ContextBuilder.
        table_selection_method=context.table_selection_method,
    )
