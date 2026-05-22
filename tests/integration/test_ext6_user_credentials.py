"""EXT-6 integration tests — per-user credential execution.

These tests verify the end-to-end behavior of per-user query execution
against a real Databricks workspace. They are skipped by default and run
only when `INTEGRATION_TESTS=true` is set in the environment (see
conftest.py).

Required environment to run:
  DATABRICKS_HOST                    — workspace URL
  DATABRICKS_TOKEN                   — service-principal credential (fallback)
  DB_WAREHOUSE_ID                    — SQL Warehouse to run against
  EXT6_USER_TOKEN_RESTRICTED         — token for a user with column-mask access only
  EXT6_USER_TOKEN_NO_SELECT          — token for a user lacking SELECT on the target table
  EXT6_TEST_TABLE_WITH_PII           — fully-qualified table with at least one masked column
  EXT6_TEST_TABLE_NO_ACCESS          — fully-qualified table the no-SELECT user cannot read

The tests describe contract behavior that the workspace MUST enforce when
`DatabricksQueryProvider.execute(user_token=...)` is called. The tests are
deliberate scaffolding — running them is what catches a future regression
in token plumbing or warehouse RBAC semantics. They are not expected to
run in CI; they are run by an engineer before a release that touches
auth, EXT-6, or the Databricks query provider.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tiri.providers.base import QueryProviderError
from tiri.providers.databricks.query import DatabricksQueryProvider


pytestmark = pytest.mark.integration


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set in environment")
    return value


@pytest.fixture
def host_and_service_token() -> tuple[str, str]:
    return _required_env("DATABRICKS_HOST"), _required_env("DATABRICKS_TOKEN")


@pytest.fixture
def warehouse_id() -> str:
    return _required_env("DB_WAREHOUSE_ID")


# ── Case 1: column-mask user sees masked values, not raw PII ───────────────


@pytest.mark.asyncio
async def test_restricted_user_sees_masked_columns(
    host_and_service_token: tuple[str, str],
    warehouse_id: str,
) -> None:
    """User with restricted column access queries a table with masked columns
    MUST return masked values, not raw PII.

    Setup expected in the workspace:
      - EXT6_TEST_TABLE_WITH_PII has a column with a UC column mask that
        the restricted user cannot bypass.
      - The service token (DATABRICKS_TOKEN) CAN see the raw values.
      - EXT6_USER_TOKEN_RESTRICTED is the restricted user's PAT/OAuth token.

    Assertion: the restricted-token query returns masks (e.g. "***"), while
    the service-token query returns raw values for the same row(s).
    """
    host, service_token = host_and_service_token
    restricted_token = _required_env("EXT6_USER_TOKEN_RESTRICTED")
    table = _required_env("EXT6_TEST_TABLE_WITH_PII")

    provider = DatabricksQueryProvider(
        host=host, token=service_token, warehouse_id=warehouse_id
    )
    sql = f"SELECT * FROM {table} LIMIT 1"

    service_result = await provider.execute(sql)
    restricted_result = await provider.execute(sql, user_token=restricted_token)

    assert service_result.rows, "service token should see at least one row"
    assert restricted_result.rows, "restricted token should also see the row"
    # At least one cell differs — the mask is doing its job.
    assert (
        service_result.rows[0] != restricted_result.rows[0]
    ), "expected at least one masked cell to differ between service and restricted views"


# ── Case 2: no-SELECT user gets permission error, not silent empty result ──


@pytest.mark.asyncio
async def test_no_select_user_gets_permission_error_not_empty_results(
    host_and_service_token: tuple[str, str],
    warehouse_id: str,
) -> None:
    """User without SELECT on a table asks about it MUST return a permission
    error, not silently return no rows.

    Setup expected: EXT6_USER_TOKEN_NO_SELECT has no SELECT grant on
    EXT6_TEST_TABLE_NO_ACCESS. The service token does.
    """
    host, service_token = host_and_service_token
    no_select_token = _required_env("EXT6_USER_TOKEN_NO_SELECT")
    table = _required_env("EXT6_TEST_TABLE_NO_ACCESS")

    provider = DatabricksQueryProvider(
        host=host, token=service_token, warehouse_id=warehouse_id
    )
    sql = f"SELECT 1 FROM {table} LIMIT 1"

    with pytest.raises(QueryProviderError) as excinfo:
        await provider.execute(sql, user_token=no_select_token)
    # The error message should indicate a permission failure, not "no rows".
    msg = str(excinfo.value).lower()
    assert any(
        keyword in msg
        for keyword in ("permission", "denied", "forbidden", "unauthorized", "not authorized")
    ), f"expected permission-related error; got: {excinfo.value}"


# ── Case 3: two users with different permissions get different results ─────


@pytest.mark.asyncio
async def test_two_users_with_different_permissions_get_different_results(
    host_and_service_token: tuple[str, str],
    warehouse_id: str,
) -> None:
    """Two users with different permissions querying the same question MUST
    return different results if their access differs.

    This is the catch-all test: it doesn't presume a specific RBAC mechanism
    (column mask / row filter / GRANT scope), only that swapping user tokens
    on the same query yields different result shapes.
    """
    host, service_token = host_and_service_token
    restricted_token = _required_env("EXT6_USER_TOKEN_RESTRICTED")
    no_select_token = _required_env("EXT6_USER_TOKEN_NO_SELECT")
    table = _required_env("EXT6_TEST_TABLE_WITH_PII")

    provider = DatabricksQueryProvider(
        host=host, token=service_token, warehouse_id=warehouse_id
    )
    sql = f"SELECT COUNT(*) AS n FROM {table}"

    restricted_result = await provider.execute(
        sql, user_token=restricted_token
    )
    # The no-SELECT user MUST fail; the restricted user MUST succeed.
    # Different outcomes for the same query → different access takes effect.
    with pytest.raises(QueryProviderError):
        await provider.execute(sql, user_token=no_select_token)
    assert restricted_result.row_count >= 1
