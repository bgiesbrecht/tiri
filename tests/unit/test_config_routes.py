"""Tests for /config routes — UI support endpoints."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tiri.api.main import create_app
from tiri.config import Config, ProviderBackendConfig, RoutingConfig
from tiri.data_models import LLMMessage, LLMResponse
from tiri.providers.base import (
    CatalogProvider,
    LLMProvider,
    QueryProvider,
    StoreProvider,
    VectorProvider,
)


# ── Test doubles ───────────────────────────────────────────────────────────


class _StubLLM(LLMProvider):
    """Records credential mutations for the credentials-override tests."""

    def __init__(self) -> None:
        self._token = "original-token"
        # Mimic the DatabricksLLMProvider attribute shape so the route's
        # `setattr(backend, "_token", value)` + client header mutation path
        # has somewhere to write.

        class _Client:
            def __init__(self) -> None:
                # Mirror httpx.AsyncClient (Databricks backend uses this).
                self.headers: dict[str, str] = {
                    "Authorization": "Bearer original-token"
                }
                # Mirror AsyncAnthropic / AsyncOpenAI (those backends mutate
                # `api_key` directly on the SDK client). The route's
                # `hasattr(backend._client, 'api_key')` path needs this to
                # see the SDK-like shape, otherwise it emits a
                # 'no settable api_key' warning for valid setups.
                self.api_key = "original-api-key"

        self._client = _Client()

    async def complete(self, messages, **kw):  # pragma: no cover
        return LLMResponse(content="ok", usage={}, raw=None)

    async def stream(self, messages, **kw) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def embed(self, texts):  # pragma: no cover
        return [[0.0] for _ in texts]


class _Store(StoreProvider):
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    async def get(self, key):
        return self._data.get(key)

    async def put(self, key, value):
        self._data[key] = value

    async def list_keys(self, prefix):
        return [k for k in self._data if k.startswith(prefix)]

    async def delete(self, key):
        self._data.pop(key, None)


class _Catalog(CatalogProvider):
    async def get_table_meta(self, n):  # pragma: no cover
        raise NotImplementedError

    async def list_tables(self, c, s):  # pragma: no cover
        return []

    async def list_schemas(self, c):  # pragma: no cover
        return []

    async def search_tables(self, q, limit=10):  # pragma: no cover
        return []


class _Vector(VectorProvider):
    async def upsert(self, id, vector, payload):  # pragma: no cover
        return None

    async def query(self, vector, top_k=5, filter=None):  # pragma: no cover
        return []

    async def delete(self, id):  # pragma: no cover
        return None

    async def list_ids(self, filter=None):  # pragma: no cover
        return []


class _Query(QueryProvider):
    async def execute(self, sql, limit=10_000, user_token=None):  # pragma: no cover
        return None

    async def validate(self, sql, user_token=None):  # pragma: no cover
        return (True, None)


def _config(*, auth_disabled: bool = True) -> Config:
    return Config(
        llm_backends={
            "databricks": ProviderBackendConfig(
                name="databricks",
                type="databricks",
                host="h",
                token="original-token",
            ),
            "anthropic": ProviderBackendConfig(
                name="anthropic", type="anthropic", api_key="sk-ant-orig"
            ),
            "openai": ProviderBackendConfig(
                name="openai", type="openai", api_key="sk-orig"
            ),
            "ollama": ProviderBackendConfig(
                name="ollama", type="ollama", base_url="http://localhost:11434"
            ),
        },
        llm_routing=RoutingConfig(
            intent="databricks::databricks-meta-llama-3-1-8b-instruct",
            planning="databricks::databricks-meta-llama-3-3-70b-instruct",
            sql="databricks::databricks-meta-llama-3-3-70b-instruct",
            synthesis="databricks::databricks-meta-llama-3-3-70b-instruct",
            clarify="databricks::databricks-meta-llama-3-1-8b-instruct",
            viz_summary="databricks::databricks-meta-llama-3-1-8b-instruct",
            embed="databricks::databricks-bge-large-en",
        ),
        catalog_provider="static",
        query_provider="duckdb",
        vector_provider="chroma",
        store_provider="sqlite",
        auth_disabled=auth_disabled,
    )


def _build_app(*, auth_disabled: bool = True) -> tuple[FastAPI, dict[str, Any]]:
    cfg = _config(auth_disabled=auth_disabled)
    container: dict[str, Any] = {
        "llm": _StubLLM(),
        "llm_backends": {
            "databricks": _StubLLM(),
            "anthropic": _StubLLM(),
            "openai": _StubLLM(),
            "ollama": _StubLLM(),
        },
        "catalog": _Catalog(),
        "metadata_providers": [],
        "query": _Query(),
        "vector": _Vector(),
        "store": _Store(),
    }
    return create_app(cfg=cfg, container=container), container


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


# ── GET /config/routing ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_routing_lists_providers_and_routes() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.get("/config/routing")
    assert r.status_code == 200
    body = r.json()
    # Providers: {name, type} entries, one per backend
    names = {p["name"] for p in body["providers"]}
    assert names == {"databricks", "anthropic", "openai", "ollama"}
    types = {p["type"] for p in body["providers"]}
    assert types == {"databricks", "anthropic", "openai", "ollama"}
    # All 7 task routes present
    assert set(body["routing"]) == {
        "intent",
        "planning",
        "sql",
        "synthesis",
        "clarify",
        "viz_summary",
        "embed",
    }
    assert body["routing"]["sql"] == (
        "databricks::databricks-meta-llama-3-3-70b-instruct"
    )


@pytest.mark.asyncio
async def test_get_routing_never_exposes_credential_values() -> None:
    """The CRITICAL invariant: GET /config/routing MUST NOT include any
    token / api_key / host value anywhere in its response. Tests against
    the literal credential strings we configured."""
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.get("/config/routing")
    body_text = json.dumps(r.json())
    # None of these credential strings should appear in the response.
    for forbidden in ("original-token", "sk-ant-orig", "sk-orig"):
        assert forbidden not in body_text, (
            f"credential {forbidden!r} leaked into /config/routing response"
        )


# ── POST /config/credentials ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_credentials_databricks_pat_format_accepted() -> None:
    app, container = _build_app()
    # Mock PAT — `dapi` prefix is what the validator looks at; the trailing
    # body is deliberately repetitive so GitHub's secret scanner doesn't
    # flag this string as a real Databricks token.
    new_token = "dapi" + "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"
    async with _client(app) as c:
        r = await c.post(
            "/config/credentials",
            json={
                "credentials": [
                    {
                        "provider": "databricks",
                        "key": "DATABRICKS_TOKEN",
                        "value": new_token,
                    }
                ]
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert "databricks::DATABRICKS_TOKEN" in body["accepted"]
    assert body["rejected"] == []
    # No warning for the recognized dapi prefix
    assert not any("databricks" in w for w in body["warnings"])
    # The backend's _token was actually mutated
    assert container["llm_backends"]["databricks"]._token == new_token
    # And the client's Authorization header was rotated
    assert (
        container["llm_backends"]["databricks"]._client.headers["Authorization"]
        == f"Bearer {new_token}"
    )


@pytest.mark.asyncio
async def test_post_credentials_anthropic_format_accepted() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/config/credentials",
            json={
                "credentials": [
                    {
                        "provider": "anthropic",
                        "key": "ANTHROPIC_API_KEY",
                        "value": "sk-ant-newkey",
                    }
                ]
            },
        )
    body = r.json()
    assert "anthropic::ANTHROPIC_API_KEY" in body["accepted"]
    assert not any("anthropic" in w for w in body["warnings"])


@pytest.mark.asyncio
async def test_post_credentials_unrecognized_format_accepted_with_warning() -> None:
    """Ollama and custom endpoints have no standard key format. The route
    accepts the value (doesn't reject) but emits a warning so the UI can
    surface 'this key shape is unusual'."""
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/config/credentials",
            json={
                "credentials": [
                    {
                        "provider": "ollama",
                        "key": "OLLAMA_API_KEY",
                        "value": "some-custom-token-value",
                    }
                ]
            },
        )
    body = r.json()
    assert body["accepted"] == ["ollama::OLLAMA_API_KEY"]
    assert body["rejected"] == []
    assert any("ollama" in w.lower() for w in body["warnings"])


@pytest.mark.asyncio
async def test_post_credentials_databricks_wrong_prefix_accepted_with_warning() -> None:
    """A value that doesn't match the expected prefix for a known provider
    type is still accepted — operators can legitimately use a non-standard
    token if their proxy expects one. But we WARN so the UI surfaces it."""
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/config/credentials",
            json={
                "credentials": [
                    {
                        "provider": "databricks",
                        "key": "DATABRICKS_TOKEN",
                        "value": "not-a-pat-format",
                    }
                ]
            },
        )
    body = r.json()
    assert body["accepted"] == ["databricks::DATABRICKS_TOKEN"]
    assert any("dapi" in w for w in body["warnings"])


@pytest.mark.asyncio
async def test_post_credentials_rejects_unknown_provider() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/config/credentials",
            json={
                "credentials": [
                    {
                        "provider": "totally-made-up",
                        "key": "X",
                        "value": "v",
                    }
                ]
            },
        )
    body = r.json()
    assert body["accepted"] == []
    assert any("unknown provider" in s for s in body["rejected"])


@pytest.mark.asyncio
async def test_post_credentials_rejects_empty_value() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        r = await c.post(
            "/config/credentials",
            json={
                "credentials": [
                    {"provider": "databricks", "key": "X", "value": ""}
                ]
            },
        )
    body = r.json()
    assert body["accepted"] == []
    assert any("empty value" in s for s in body["rejected"])


@pytest.mark.asyncio
async def test_post_credentials_response_never_echoes_value() -> None:
    """Critical invariant: the response MUST NOT include the value we posted
    anywhere. The accepted/rejected/warnings strings should reference only
    provider + key labels, never the secret material."""
    app, _ = _build_app()
    secret = "sk-ant-very-secret-value-1234567890"
    async with _client(app) as c:
        r = await c.post(
            "/config/credentials",
            json={
                "credentials": [
                    {
                        "provider": "anthropic",
                        "key": "ANTHROPIC_API_KEY",
                        "value": secret,
                    }
                ]
            },
        )
    body_text = json.dumps(r.json())
    assert secret not in body_text


@pytest.mark.asyncio
async def test_delete_credentials_clears_session_state() -> None:
    app, _ = _build_app()
    async with _client(app) as c:
        # First add an override
        await c.post(
            "/config/credentials",
            json={
                "credentials": [
                    {
                        "provider": "databricks",
                        "key": "DATABRICKS_TOKEN",
                        "value": "dapinew",
                    }
                ]
            },
        )
        # Then clear
        r = await c.delete("/config/credentials")
    assert r.status_code == 200
    assert r.json() == {"cleared": True}
    assert app.state.session_credentials == {}
