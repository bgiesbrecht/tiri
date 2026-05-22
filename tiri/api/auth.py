"""Bearer-token authentication dependency.

This dependency enforces the *presence* of a Bearer token and returns it
for downstream use by EXT-6 (per-user credential execution). It does NOT
validate the token's signature, expiry, or identity — that is delegated to
the data warehouse when the token is forwarded to `QueryProvider.execute()`.
Unity Catalog rejects invalid tokens at query time, which is the correct
trust boundary: the warehouse is the authoritative validator of its own
credentials.

Token source precedence:
  1. `Authorization: Bearer <token>` — standard API clients
  2. `X-Forwarded-Access-Token: <token>` — Databricks Apps injects this
     header with the logged-in user's token. When Tiri is deployed as a
     Databricks App and a client does not supply an Authorization header,
     this header provides the user credential automatically.

The x-forwarded-access-token pattern is consistent with how other
Databricks services (Vector Search, SQL, UC functions) obtain per-user
credentials in Apps deployments. It is not a Databricks SDK feature —
it is an HTTP header convention used by the Apps runtime.

Set `AUTH_DISABLED=true` in env (via Config) to skip the check entirely
for local development.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request


_BEARER_PREFIX = "Bearer "


async def auth_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_forwarded_access_token: str | None = Header(default=None),
) -> str | None:
    """Returns the Bearer token, or None when auth is disabled.

    Checks Authorization: Bearer first, then X-Forwarded-Access-Token
    (Databricks Apps deployment). Raises HTTPException(401) when auth is
    enabled and neither header is present or usable.
    """
    cfg = request.app.state.cfg
    if getattr(cfg, "auth_disabled", False):
        return None

    # Preference 1: explicit Authorization: Bearer header
    if authorization and authorization.startswith(_BEARER_PREFIX):
        token = authorization[len(_BEARER_PREFIX):].strip()
        if token:
            return token

    # Preference 2: Databricks Apps forwarded token
    if x_forwarded_access_token and x_forwarded_access_token.strip():
        return x_forwarded_access_token.strip()

    raise HTTPException(
        status_code=401,
        detail={
            "error": "unauthorized",
            "message": (
                "Missing or malformed credentials. Provide either "
                "Authorization: Bearer <token> or, in Databricks Apps "
                "deployments, the X-Forwarded-Access-Token header."
            ),
        },
    )
