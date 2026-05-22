"""SQLAgent — generates SQL with a self-correction loop.

Validates every candidate via `QueryProvider.validate()` before returning.
The most-critical agent in the pipeline; correctness here directly determines
answer correctness.
"""

from __future__ import annotations

import logging
import re

from tiri.data_models import (
    ContextPackage,
    IntentResult,
    LLMMessage,
    SQLResult,
)
from tiri.engine.agents.base import (
    format_default_filters,
    format_examples,
    format_history,
    format_joins,
    format_mcp_context,
    format_metrics_for_sql,
    format_schemas_for_sql,
    format_snippets_for_sql,
    load_template,
    render,
)
from tiri.providers.base import LLMProvider, QueryProvider


_TEMPLATE = load_template("sql_generation.txt")
_log = logging.getLogger("tiri.engine.agents.sql")
_CANNOT_ANSWER_PREFIX = "CANNOT_ANSWER:"


class SQLAgent:
    def __init__(
        self,
        llm: LLMProvider,
        query: QueryProvider,
        max_retries: int = 3,
    ) -> None:
        self._llm = llm
        self._query = query
        self._max_retries = max_retries

    async def run(
        self,
        question: str,
        context: ContextPackage,
        intent: IntentResult,
        user_token: str | None = None,
    ) -> SQLResult:
        # Filter context to what IntentAgent identified as relevant.
        relevant_table_names = intent.relevant_tables or list(
            context.table_schemas
        )
        relevant_snippets = intent.relevant_snippets or context.sql_snippets

        system_prompt = render(
            _TEMPLATE,
            text_instruction=context.text_instruction or "(none)",
            default_filters=format_default_filters(context.default_filters),
            mcp_context=format_mcp_context(context.mcp_context),
            table_schemas=format_schemas_for_sql(
                relevant_table_names, context.table_schemas
            ),
            join_specs=format_joins(context.joins),
            sql_snippets=format_snippets_for_sql(relevant_snippets),
            metrics=format_metrics_for_sql(context.metrics),
            examples=format_examples(context.retrieved_examples),
            history=format_history(context.conversation_history),
            question=question,
        )

        messages: list[LLMMessage] = [
            LLMMessage(role="system", content=system_prompt)
        ]

        attempt = 0
        last_error: str | None = None
        while attempt < self._max_retries:
            attempt += 1
            response = await self._llm.complete(messages, task="sql")
            candidate = _strip_markdown_fences(response.content.strip())

            if candidate.startswith(_CANNOT_ANSWER_PREFIX):
                reason = candidate[len(_CANNOT_ANSWER_PREFIX):].strip()
                return SQLResult(
                    is_valid=False,
                    attempts=attempt,
                    sql="",
                    explanation="",
                    error=f"CANNOT_ANSWER: {reason}",
                )

            # Pass user_token so EXPLAIN runs with the user's own permissions,
            # not the service credential. Without this, a user without SELECT
            # on a table would pass validation (service has access) and fail
            # at execute time with a permission error — bypassing UC
            # enforcement at the validate boundary.
            is_valid, error = await self._query.validate(
                candidate, user_token=user_token
            )
            if is_valid:
                return SQLResult(
                    is_valid=True,
                    attempts=attempt,
                    sql=candidate,
                    explanation="",
                    error=None,
                )

            last_error = error or "validation failed without a message"
            _log.info(
                "SQLAgent attempt %d failed validation: %s", attempt, last_error
            )
            # Feed the failure back into the conversation for the next attempt.
            messages.append(LLMMessage(role="assistant", content=candidate))
            messages.append(
                LLMMessage(
                    role="user",
                    content=(
                        f"That SQL has an error: {last_error}\n"
                        "Please fix it and return only the corrected SQL."
                    ),
                )
            )

        return SQLResult(
            is_valid=False,
            attempts=self._max_retries,
            sql="",
            explanation="",
            error=f"Failed after {self._max_retries} attempts: {last_error}",
        )


_MARKDOWN_FENCE_OPEN = re.compile(r"^```(?:sql|SQL)?\s*\n?", re.IGNORECASE)
_MARKDOWN_FENCE_CLOSE = re.compile(r"\n?\s*```\s*$")


def _strip_markdown_fences(candidate: str) -> str:
    """Open-source models (qwen2.5-coder, codellama, deepseek) routinely
    wrap SQL in triple-backtick fences despite the 'no markdown fences'
    instruction in the prompt. Without this strip, the warehouse receives
    `EXPLAIN \\`\\`\\`sql ... \\`\\`\\`` and returns PARSE_SYNTAX_ERROR.

    IntentAgent and SynthesisAgent already do the equivalent strip on
    their JSON responses (see `_parse_json_response`). SQLAgent now does
    the same so the agent is robust to model-side prompt-following gaps,
    not just dependent on every backend obeying the format instruction.
    """
    stripped = candidate.strip()
    if stripped.startswith("```"):
        stripped = _MARKDOWN_FENCE_OPEN.sub("", stripped, count=1)
        stripped = _MARKDOWN_FENCE_CLOSE.sub("", stripped, count=1)
    return stripped.strip()
