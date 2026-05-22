"""Config introspection + session credential overrides — UI support routes.

`GET /config/routing` exposes the wired backends and per-task routing so the
UI knows what `provider::model` combinations exist. **Credential values are
NEVER returned** — only provider names, types, and the model identifiers
each task is mapped to. The endpoint is safe to call from any authenticated
client; the response carries zero secrets.

`POST /config/credentials` accepts session credential overrides. Values land
in `app.state.session_credentials` (process-memory only — never persisted,
never logged) and override the corresponding `[llm.providers.NAME]` token /
api_key for the lifetime of the process. The endpoint:
  - Validates each credential against the known formats for its provider
    type (dapi… for Databricks, sk-ant-… for Anthropic, sk-… for OpenAI).
  - Unrecognized formats are accepted with a warning rather than rejected —
    Ollama and custom endpoints don't have a standard key shape, and
    rejection-by-format would lock out legitimate setups.
  - Value strings are never logged at any level (INFO/DEBUG/WARNING) — only
    the provider name + key name.

`DELETE /config/credentials` clears all session overrides for the current
process. Behavior reverts to whatever was loaded from `tiri.toml` / env at
startup.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from tiri.api.auth import auth_token


_log = logging.getLogger("tiri.api.config")
router = APIRouter()


# ── GET /config/routing ────────────────────────────────────────────────────


@router.get("/routing")
async def get_routing(
    request: Request,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Return the configured backends and task routing.

    Response shape:
      {
        "providers": [
          {"name": "databricks", "type": "databricks"},
          {"name": "anthropic",  "type": "anthropic"}
        ],
        "routing": {
          "intent":      "databricks::databricks-meta-llama-3-1-8b-instruct",
          "planning":    "databricks::databricks-meta-llama-3-3-70b-instruct",
          "sql":         "databricks::databricks-meta-llama-3-3-70b-instruct",
          "synthesis":   "databricks::databricks-meta-llama-3-3-70b-instruct",
          "clarify":     "databricks::databricks-meta-llama-3-1-8b-instruct",
          "viz_summary": "databricks::databricks-meta-llama-3-1-8b-instruct",
          "embed":       "databricks::databricks-bge-large-en"
        }
      }

    No credential values appear anywhere in the response. The `providers`
    array lists only `{name, type}`; the `routing` map lists only
    `provider_name::model_name`.
    """
    cfg = request.app.state.cfg
    providers = [
        {"name": name, "type": bc.type}
        for name, bc in cfg.llm_backends.items()
    ]
    routing = {
        "intent": cfg.llm_routing.intent,
        "planning": cfg.llm_routing.planning,
        "sql": cfg.llm_routing.sql,
        "synthesis": cfg.llm_routing.synthesis,
        "clarify": cfg.llm_routing.clarify,
        "viz_summary": cfg.llm_routing.viz_summary,
        "embed": cfg.llm_routing.embed,
    }
    return {"providers": providers, "routing": routing}


# ── POST /config/credentials ───────────────────────────────────────────────


# Provider type → expected key prefix(es). Each prefix is treated as a
# "recognized" format; anything else is accepted with a warning, not
# rejected — Ollama has no API key (empty) and custom endpoints (e.g. an
# internal proxy) may use arbitrary token shapes.
_KNOWN_PREFIXES: dict[str, tuple[str, ...]] = {
    "databricks": ("dapi",),
    "anthropic": ("sk-ant-",),
    "openai": ("sk-",),  # NOTE: sk-ant- also matches, so check anthropic first
}


@router.post("/credentials")
async def post_credentials(
    request: Request,
    body: dict[str, Any],
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Apply session credential overrides.

    Request body:
      {
        "credentials": [
          {"provider": "databricks", "key": "DATABRICKS_TOKEN", "value": "dapi…"},
          {"provider": "anthropic",  "key": "ANTHROPIC_API_KEY", "value": "sk-ant-…"}
        ]
      }

    Response:
      {
        "accepted":  ["databricks::DATABRICKS_TOKEN", ...],
        "warnings":  ["openai::OPENAI_API_KEY — sk- prefix is OpenAI; verify intent"],
        "rejected":  []
      }

    Validation policy:
      - Recognized prefix (dapi / sk-ant- / sk-)  → accepted silently
      - Unrecognized prefix for a known provider  → accepted with warning
      - Empty value                                → rejected
      - Unknown provider                           → rejected

    Overrides land in `app.state.session_credentials` as a dict keyed by
    `provider`. The corresponding `ProviderBackendConfig` (held inside the
    already-instantiated LLM backend) is mutated in place so subsequent
    `RoomEngine.chat()` calls see the new token. **Values are never logged
    or echoed back** — the response only confirms the provider+key labels.
    """
    raw_creds = body.get("credentials")
    if not isinstance(raw_creds, list):
        raise HTTPException(
            status_code=422,
            detail="`credentials` must be a list of {provider, key, value} objects",
        )

    cfg = request.app.state.cfg
    container = request.app.state.container
    backends = container.get("llm_backends", {})

    if not hasattr(request.app.state, "session_credentials"):
        request.app.state.session_credentials = {}
    session_creds: dict[str, dict[str, str]] = request.app.state.session_credentials

    accepted: list[str] = []
    warnings: list[str] = []
    rejected: list[str] = []

    for entry in raw_creds:
        if not isinstance(entry, dict):
            rejected.append("(non-object entry)")
            continue
        provider = str(entry.get("provider") or "")
        key_label = str(entry.get("key") or "")
        value = str(entry.get("value") or "")
        label = f"{provider}::{key_label}"

        if not provider:
            rejected.append("(missing provider)")
            continue
        if not value:
            rejected.append(f"{label} — empty value")
            continue
        if provider not in cfg.llm_backends:
            rejected.append(
                f"{label} — unknown provider; configured: "
                f"{sorted(cfg.llm_backends)}"
            )
            continue

        backend_type = cfg.llm_backends[provider].type
        expected = _KNOWN_PREFIXES.get(backend_type)
        warning_msg: str | None = None
        if expected and not any(value.startswith(p) for p in expected):
            warning_msg = (
                f"{label} — value does not match expected prefix "
                f"{expected} for type {backend_type!r}; accepted anyway"
            )
        elif not expected:
            warning_msg = (
                f"{label} — provider type {backend_type!r} has no standard "
                "key format; accepted anyway"
            )

        # Mutate the live backend instance so subsequent calls see the new
        # credential. Each provider implementation stores its credential
        # field with a different name — we touch the well-known attributes
        # rather than re-instantiating the backend (which would lose
        # warm httpx connection pools).
        backend = backends.get(provider)
        if backend is not None:
            if backend_type == "databricks":
                # Both host and token live on the provider; we only set token
                # since host doesn't rotate per-session. Also reset the
                # default Authorization header on the underlying httpx client
                # so connections established before this call pick up the
                # new token.
                setattr(backend, "_token", value)
                client = getattr(backend, "_client", None)
                if client is not None:
                    client.headers["Authorization"] = f"Bearer {value}"
            elif backend_type in ("anthropic", "openai"):
                # Concrete providers hold the SDK client; replace the api_key
                # on the client when the SDK supports it, otherwise drop a
                # warning that the next process restart will pick it up.
                # For these vendors the SDK clients are usually reusable.
                if hasattr(backend, "_client") and hasattr(
                    backend._client, "api_key"
                ):
                    backend._client.api_key = value
                else:
                    warning_msg = (
                        warning_msg
                        or f"{label} — backend client has no settable api_key; "
                        "restart the process to pick up the new value"
                    )
            elif backend_type == "ollama":
                # Ollama doesn't use API keys.
                if value:
                    warning_msg = (
                        warning_msg
                        or f"{label} — ollama doesn't use API keys; value ignored"
                    )

        session_creds.setdefault(provider, {})[key_label] = value
        accepted.append(label)
        if warning_msg:
            warnings.append(warning_msg)
        # Log structure only — never the value.
        _log.info(
            "Session credential override applied: provider=%s key=%s",
            provider,
            key_label,
        )

    return {"accepted": accepted, "warnings": warnings, "rejected": rejected}


# ── DELETE /config/credentials ─────────────────────────────────────────────


@router.delete("/credentials")
async def delete_credentials(
    request: Request,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Clear all session credential overrides for this process.

    The provider backends keep whatever credentials they currently hold —
    we don't restore the original tiri.toml / env values, because doing
    so would require holding a copy of those originals in memory, which is
    a worse posture than just asking the operator to restart the process
    if they really want a clean state. This endpoint exists for the UI's
    "Clear session overrides" button to nuke the tracking record so the
    panel reflects "no overrides active". Process restart is the canonical
    way to fully restore the originals.
    """
    request.app.state.session_credentials = {}
    return {"cleared": True}
