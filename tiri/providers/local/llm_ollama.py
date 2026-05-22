"""OllamaLLMProvider — local models via Ollama's OpenAI-compatible HTTP API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from tiri.data_models import LLMMessage, LLMResponse
from tiri.providers.base import LLMProvider, LLMProviderError


_DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=5.0)


class OllamaLLMProvider(LLMProvider):
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.3",
        embed_model: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._embed_model = embed_model or model
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        chosen_model = model or self._model
        body = {
            "model": chosen_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "options": {"temperature": temperature, "num_predict": max_tokens},
            "stream": False,
        }
        data = await self._post_json("/api/chat", body)
        content = (data.get("message") or {}).get("content", "")
        return LLMResponse(content=content, usage={}, raw=data)

    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        chosen_model = model or self._model
        body = {
            "model": chosen_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "options": {"temperature": temperature},
            "stream": True,
        }
        url = f"{self._base_url}/api/chat"
        try:
            async with self._client.stream("POST", url, json=body) as response:
                if response.status_code >= 400:
                    text = await response.aread()
                    raise LLMProviderError(
                        f"Ollama stream HTTP {response.status_code}: {text!r}"
                    )
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    content = (chunk.get("message") or {}).get("content")
                    if content:
                        yield content
        except httpx.HTTPError as e:
            raise LLMProviderError(f"Ollama streaming HTTP error: {e}") from e

    async def embed(self, texts: list[str]) -> list[list[float]]:
        body = {"model": self._embed_model, "input": texts}
        data = await self._post_json("/api/embed", body)
        embeddings = data.get("embeddings")
        if embeddings is None:
            raise LLMProviderError(
                f"Ollama embed response missing 'embeddings': {data!r}"
            )
        return [list(vec) for vec in embeddings]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post_json(self, path: str, body: dict) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = await self._client.post(url, json=body)
        except httpx.HTTPError as e:
            raise LLMProviderError(f"Ollama HTTP error at {path}: {e}") from e
        if response.status_code >= 400:
            raise LLMProviderError(
                f"HTTP {response.status_code} at {path}: {response.text!r}"
            )
        try:
            return response.json()
        except json.JSONDecodeError as e:
            raise LLMProviderError(
                f"Non-JSON response from {path}: {response.text!r}"
            ) from e
