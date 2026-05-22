"""ClarifyAgent — generates a focused follow-up when intent is ambiguous."""

from __future__ import annotations

from tiri.data_models import (
    ClarifyResult,
    ContextPackage,
    IntentResult,
    LLMMessage,
)
from tiri.engine.agents.base import (
    format_table_list,
    load_template,
    render,
)
from tiri.providers.base import LLMProvider


_TEMPLATE = load_template("clarification.txt")


class ClarifyAgent:
    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def run(
        self,
        question: str,
        context: ContextPackage,
        intent: IntentResult,
    ) -> ClarifyResult:
        prompt = render(
            _TEMPLATE,
            table_list=format_table_list(context.table_schemas),
            question=question,
            reasoning=intent.reasoning or "(none provided)",
        )
        response = await self._llm.complete(
            [LLMMessage(role="system", content=prompt)],
            task="clarify",
        )
        return ClarifyResult(question=response.content.strip())
