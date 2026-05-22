"""AnthropicLLMProvider — completion + streaming. No embed (raises)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic
from anthropic import APIError, APIStatusError, AnthropicError, RateLimitError

from tiri.data_models import LLMMessage, LLMResponse
from tiri.providers.base import LLMProvider, LLMProviderError


class AnthropicLLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        *,
        client: AsyncAnthropic | None = None,
    ) -> None:
        if not api_key and client is None:
            raise LLMProviderError("AnthropicLLMProvider requires api_key")
        self._model = model
        self._client = client or AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        chosen_model = model or self._model
        system_text, anth_messages = _split_messages(messages)
        try:
            response = await self._client.messages.create(
                model=chosen_model,
                system=system_text or None,
                messages=anth_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except (RateLimitError, APIStatusError, APIError, AnthropicError) as e:
            raise LLMProviderError(f"Anthropic completion failed: {e}") from e

        content = _extract_text(response.content)
        usage = {
            "prompt_tokens": getattr(response.usage, "input_tokens", 0),
            "completion_tokens": getattr(response.usage, "output_tokens", 0),
        }
        return LLMResponse(content=content, usage=usage, raw=response)

    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        chosen_model = model or self._model
        system_text, anth_messages = _split_messages(messages)
        try:
            async with self._client.messages.stream(
                model=chosen_model,
                system=system_text or None,
                messages=anth_messages,
                temperature=temperature,
                max_tokens=2048,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except (RateLimitError, APIStatusError, APIError, AnthropicError) as e:
            raise LLMProviderError(f"Anthropic streaming failed: {e}") from e

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise LLMProviderError(
            "Anthropic does not support embeddings — wire a different backend "
            "to the `embed` route in tiri.toml"
        )


def _split_messages(
    messages: list[LLMMessage],
) -> tuple[str, list[dict]]:
    """Anthropic takes system text separately; user/assistant in messages list."""
    system_parts: list[str] = []
    anth_messages: list[dict] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        else:
            anth_messages.append({"role": m.role, "content": m.content})
    return ("\n\n".join(system_parts).strip(), anth_messages)


def _extract_text(blocks) -> str:
    if blocks is None:
        return ""
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)
