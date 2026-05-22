"""OpenAILLMProvider — drop-in for DatabricksLLMProvider using the OpenAI SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI
from openai import APIError, APIStatusError, OpenAIError, RateLimitError

from tiri.data_models import LLMMessage, LLMResponse
from tiri.providers.base import LLMProvider, LLMProviderError


class OpenAILLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        embed_model: str = "text-embedding-3-small",
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if not api_key and client is None:
            raise LLMProviderError("OpenAILLMProvider requires api_key")
        self._model = model
        self._embed_model = embed_model
        self._client = client or AsyncOpenAI(api_key=api_key)

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        chosen_model = model or self._model
        try:
            response = await self._client.chat.completions.create(
                model=chosen_model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except (RateLimitError, APIStatusError, APIError, OpenAIError) as e:
            raise LLMProviderError(f"OpenAI completion failed: {e}") from e

        choice = response.choices[0]
        usage_obj = getattr(response, "usage", None)
        usage = _usage_dict(usage_obj)
        return LLMResponse(
            content=choice.message.content or "",
            usage=usage,
            raw=response,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        chosen_model = model or self._model
        try:
            stream = await self._client.chat.completions.create(
                model=chosen_model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                temperature=temperature,
                stream=True,
            )
        except (RateLimitError, APIStatusError, APIError, OpenAIError) as e:
            raise LLMProviderError(f"OpenAI streaming failed: {e}") from e

        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = await self._client.embeddings.create(
                model=self._embed_model, input=texts
            )
        except (RateLimitError, APIStatusError, APIError, OpenAIError) as e:
            raise LLMProviderError(f"OpenAI embed failed: {e}") from e
        return [item.embedding for item in response.data]


def _usage_dict(usage: Any) -> dict:
    if usage is None:
        return {}
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
    }
