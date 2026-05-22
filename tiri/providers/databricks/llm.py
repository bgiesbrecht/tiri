"""DatabricksLLMProvider — Model Serving over OpenAI-compatible REST.

See docs/databricks_providers.md for the specification.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from tiri.data_models import LLMMessage, LLMResponse
from tiri.providers.base import LLMProvider, LLMProviderError


_DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_MAX_429_RETRIES = 3
_MAX_5XX_RETRIES = 1


class DatabricksLLMProvider(LLMProvider):
    """Calls Databricks Model Serving for completion/streaming/embedding."""

    def __init__(
        self,
        host: str,
        token: str,
        completion_endpoint: str = "databricks-meta-llama-3-3-70b-instruct",
        embed_endpoint: str = "databricks-bge-large-en",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not host:
            raise LLMProviderError("DatabricksLLMProvider requires host")
        if not token:
            raise LLMProviderError("DatabricksLLMProvider requires token")
        self._host = host.rstrip("/")
        self._token = token
        self._completion_endpoint = completion_endpoint
        self._embed_endpoint = embed_endpoint
        self._client = client or httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {token}"},
        )

    # ── public API ─────────────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task: str = "sql",
        model: str | None = None,
    ) -> LLMResponse:
        endpoint = model or self._completion_endpoint
        body = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = await self._post_json(
            f"/serving-endpoints/{endpoint}/invocations", body
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMProviderError(
                f"Unexpected completion response shape: {data!r}"
            ) from e
        usage = data.get("usage", {}) or {}
        return LLMResponse(content=content, usage=dict(usage), raw=data)

    async def stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        task: str = "sql",
        model: str | None = None,
    ) -> AsyncIterator[str]:
        endpoint = model or self._completion_endpoint
        body = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        url = f"{self._host}/serving-endpoints/{endpoint}/invocations"
        try:
            async with self._client.stream("POST", url, json=body) as response:
                if response.status_code >= 400:
                    text = await response.aread()
                    raise LLMProviderError(
                        f"Streaming completion failed {response.status_code}: "
                        f"{text!r}"
                    )
                async for line in response.aiter_lines():
                    chunk = _parse_sse_chunk(line)
                    if chunk:
                        yield chunk
        except httpx.HTTPError as e:
            raise LLMProviderError(f"Streaming HTTP error: {e}") from e

    async def embed(self, texts: list[str]) -> list[list[float]]:
        body = {"input": texts}
        data = await self._post_json(
            f"/serving-endpoints/{self._embed_endpoint}/invocations", body
        )
        try:
            return [item["embedding"] for item in data["data"]]
        except (KeyError, TypeError) as e:
            raise LLMProviderError(
                f"Unexpected embed response shape: {data!r}"
            ) from e

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── retry-aware POST ───────────────────────────────────────────────────

    async def _post_json(self, path: str, body: dict) -> dict[str, Any]:
        url = f"{self._host}{path}"
        for attempt in range(1, _MAX_429_RETRIES + 1):
            try:
                response = await self._client.post(url, json=body)
            except httpx.HTTPError as e:
                raise LLMProviderError(f"HTTP error calling {path}: {e}") from e

            status = response.status_code
            if 200 <= status < 300:
                try:
                    return response.json()
                except json.JSONDecodeError as e:
                    raise LLMProviderError(
                        f"Non-JSON response from {path}: {response.text!r}"
                    ) from e

            if status == 429:
                if attempt < _MAX_429_RETRIES:
                    await asyncio.sleep(_backoff_seconds(attempt))
                    continue
                raise LLMProviderError(
                    f"Rate-limited (HTTP 429) at {path} after "
                    f"{_MAX_429_RETRIES} attempts"
                )

            if 500 <= status < 600:
                if attempt <= _MAX_5XX_RETRIES:
                    await asyncio.sleep(_backoff_seconds(attempt))
                    continue
                raise LLMProviderError(
                    f"HTTP {status} at {path}: {response.text!r}"
                )

            # 4xx other than 429 — fail fast.
            raise LLMProviderError(
                f"HTTP {status} at {path}: {response.text!r}"
            )

        # Defensive: loop above always returns or raises.
        raise LLMProviderError(f"Exhausted retries at {path}")


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 0.5, 1.0, 2.0 seconds."""
    return 0.5 * (2 ** (attempt - 1))


def _parse_sse_chunk(line: str) -> str:
    """Extract `delta.content` from one SSE `data: {...}` line.

    Returns "" for non-data lines, the `[DONE]` sentinel, malformed payloads,
    or chunks that don't include a content delta.
    """
    if not line.startswith("data:"):
        return ""
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return ""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    try:
        return data["choices"][0]["delta"].get("content", "") or ""
    except (KeyError, IndexError, TypeError):
        return ""
