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
            "messages": _normalize_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            data = await self._post_json(
                f"/serving-endpoints/{endpoint}/invocations", body
            )
        except LLMProviderError as exc:
            # Some Databricks-hosted reasoning endpoints (claude-opus-4-x)
            # reject the `temperature` parameter at the proxy layer:
            # `Model ... does not support the temperature parameter.`
            # Retry once with temperature stripped — preserves determinism
            # on every other endpoint that accepts it.
            if "does not support the temperature parameter" in str(exc):
                body.pop("temperature", None)
                data = await self._post_json(
                    f"/serving-endpoints/{endpoint}/invocations", body
                )
            else:
                raise
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
            "messages": _normalize_messages(messages),
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


def _normalize_messages(messages: list[LLMMessage]) -> list[dict]:
    """Databricks Model Serving proxies upstream LLM APIs (Anthropic Claude,
    Vertex Gemini, OpenAI). Upstream APIs reject requests that contain only
    system messages — Anthropic with "messages: at least one message is
    required", Gemini with "at least one contents field is required". The
    native Llama endpoints accept system-only, but the proxy doesn't
    rewrite the request to fit the upstream contract.

    Caught during frontier-model benchmark validation (Opus 4.7 + Gemini
    2.5 Pro both returned 0/5). Fix: same pattern as AnthropicLLMProvider's
    `_split_messages` — inject a "Proceed." user turn when no user or
    assistant messages are present. Llama accepts the placeholder fine;
    Opus / Gemini now have at least one user message and pass through.
    """
    out = [{"role": m.role, "content": m.content} for m in messages]
    if not any(m["role"] in ("user", "assistant") for m in out):
        # The placeholder needs to be directive enough that models which
        # generate against the user message (Gemini in particular) actually
        # produce output. "Proceed." alone returns empty content on
        # databricks-gemini-2-5-pro; explicitly pointing back at the system
        # instructions works across Claude / Gemini / GPT proxies.
        out.append(
            {
                "role": "user",
                "content": "Please respond to the instructions above.",
            }
        )
    return out
