"""AnthropicLLMProvider — completion + streaming. No embed (raises)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic
from anthropic import APIError, APIStatusError, AnthropicError, RateLimitError

from tiri.data_models import LLMMessage, LLMResponse
from tiri.providers.base import LLMProvider, LLMProviderError


# Anthropic SDK defaults to a 600s per-request timeout and 2 retries, so a
# hung request can block for 30 minutes. That's catastrophic for the UI's
# SSE streaming UX — operators see a spinner with no signal anything went
# wrong. Cap at 120s per request and a single retry, so a stuck call
# surfaces as an error within ~4 minutes instead of half an hour.
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_MAX_RETRIES = 1


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
        self._client = client or AsyncAnthropic(
            api_key=api_key,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            max_retries=_DEFAULT_MAX_RETRIES,
        )

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
        # The Anthropic SDK ≥ 0.50 rejects `system=None` with
        # "system: Input should be a valid array". Omit the kwarg entirely
        # when there's no system content rather than passing None.
        kwargs: dict = {
            "model": chosen_model,
            "messages": anth_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_text:
            kwargs["system"] = system_text
        try:
            response = await self._client.messages.create(**kwargs)
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
        # See note in complete() — `system=None` is rejected by the modern
        # Anthropic SDK; omit the kwarg entirely when absent.
        kwargs: dict = {
            "model": chosen_model,
            "messages": anth_messages,
            "temperature": temperature,
            "max_tokens": 2048,
        }
        if system_text:
            kwargs["system"] = system_text
        try:
            async with self._client.messages.stream(**kwargs) as stream:
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
    """Anthropic takes system text separately; user/assistant in messages list.

    Anthropic also requires `messages` to be non-empty — system-only prompts
    raise `messages: at least one message is required`. Tiri's agents (Intent,
    Clarify, Planning, Synthesis, VizAgent summary, HypothesisAgent) all pass
    a single system message containing the entire prompt including the
    question. To keep that shape working with Anthropic, we inject a
    placeholder user turn when no user/assistant messages are present —
    the model already has all the actual instructions in `system`.

    OpenAI, Databricks Model Serving, and Ollama all accept system-only
    messages, so this normalization is Anthropic-specific.
    """
    system_parts: list[str] = []
    anth_messages: list[dict] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        else:
            anth_messages.append({"role": m.role, "content": m.content})
    if not anth_messages:
        anth_messages = [{"role": "user", "content": "Proceed."}]
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
