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


_DEFAULT_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
# Read timeout bumped from 60s to 300s for reasoning models. GPT-5.5 Pro on
# the Databricks Responses API runs `effort: high` by default and can spend
# multiple minutes on internal reasoning before producing a single token of
# output. The connect timeout stays at 10s since establishing TCP isn't
# affected by the model's reasoning budget.
_MAX_429_RETRIES = 3
_MAX_5XX_RETRIES = 2
# Bumped from 1 to 2 (3 total attempts) for the Responses-API path.
# Reasoning-model upstreams (GPT-5 family on /serving-endpoints/responses)
# have higher latency variance than Chat Completions endpoints — the
# proxy intermittently returns HTTP 502 INTERNAL_ERROR when the upstream
# takes too long to respond. A single retry isn't generous enough; the
# pattern was reproduced 2/2 runs against tpch-sales on GPT-5.5 Pro
# (same question, same 502, both runs). The wider retry window catches
# the recovery without burning the whole turn.

# Reasoning models on the Responses API charge BOTH reasoning and final
# output against `max_output_tokens`. The agent's caller asks for e.g.
# 2048 tokens of output, but the model may consume that entire budget on
# reasoning and leave none for the actual message. Bump the budget on the
# Responses API path so reasoning has its own headroom AND the requested
# output budget still fits.
_RESPONSES_API_TOKEN_FLOOR = 8192


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
            return _from_chat_completions(data)
        except LLMProviderError as exc:
            s = str(exc)
            # Some Databricks-hosted reasoning endpoints (claude-opus-4-x)
            # reject the `temperature` parameter at the proxy layer:
            # `Model ... does not support the temperature parameter.`
            # Retry once with temperature stripped — preserves determinism
            # on every other endpoint that accepts it.
            if "does not support the temperature parameter" in s:
                body.pop("temperature", None)
                data = await self._post_json(
                    f"/serving-endpoints/{endpoint}/invocations", body
                )
                return _from_chat_completions(data)
            # GPT-5 family on Databricks routes through the Responses API,
            # not Chat Completions. The proxy returns a 400 directing us
            # to /serving-endpoints/responses; we retry through that path
            # with the alternate request shape. The Responses API also
            # doesn't honor `temperature` on reasoning models, so we omit
            # it here unconditionally (the Chat Completions path keeps
            # passing it for backends that do honor it).
            if (
                "only supports the Responses API" in s
                or "/serving-endpoints/responses" in s
            ):
                responses_body = {
                    "model": endpoint,
                    "input": _normalize_messages(messages),
                    "max_output_tokens": max(
                        max_tokens, _RESPONSES_API_TOKEN_FLOOR
                    ),
                }
                data = await self._post_json(
                    "/serving-endpoints/responses", responses_body
                )
                return _from_responses_api(data)
            raise

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


def _from_chat_completions(data: dict) -> LLMResponse:
    """Parse a standard Chat Completions response shape."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMProviderError(
            f"Unexpected completion response shape: {data!r}"
        ) from e
    usage = data.get("usage", {}) or {}
    return LLMResponse(content=content, usage=dict(usage), raw=data)


def _from_responses_api(data: dict) -> LLMResponse:
    """Parse a Responses API response (GPT-5 family).

    The output is a list of items mixing `reasoning` and `message`
    entries. The assistant text lives on the message item's content
    block where `type == "output_text"`. Reasoning items have no
    user-visible content and MUST be skipped. Usage keys are renamed
    so callers see the same `prompt_tokens` / `completion_tokens`
    shape as Chat Completions.
    """
    content = ""
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue  # skip "reasoning" items and anything else
        for block in item.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text", "")
                if text:
                    content = text
                    break
        if content:
            break
    if not content:
        raise LLMProviderError(
            f"Responses API returned no output_text content: {data!r}"
        )
    raw_usage = data.get("usage", {}) or {}
    usage = {
        "prompt_tokens": raw_usage.get("input_tokens", 0),
        "completion_tokens": raw_usage.get("output_tokens", 0),
    }
    return LLMResponse(content=content, usage=usage, raw=data)


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
