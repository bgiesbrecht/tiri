"""Integration test gating.

Per CLAUDE.md, integration tests require a live Databricks workspace and are
skipped in CI unless `INTEGRATION_TESTS=true` is set in the environment. They
are also opt-in locally — running `pytest tests/` should not hit the network
unless the developer explicitly opted in.

Skipping is enforced via a session-level fixture that checks the env var.
Individual tests use `@pytest.mark.integration` so they can be deselected
by pytest expression too (`pytest -m "not integration"`).
"""

from __future__ import annotations

import os

import pytest


_INTEGRATION_DIR = os.path.dirname(os.path.abspath(__file__))


def pytest_collection_modifyitems(config, items):
    """Skip tests under tests/integration/ unless INTEGRATION_TESTS=true.

    Scoped by item path: conftest hooks fire for every collected item in the
    session (not just items in this directory), so we must filter explicitly.
    Without this filter, running `pytest tests/` skips unit tests too.
    """
    if os.environ.get("INTEGRATION_TESTS", "").lower() in ("true", "1", "yes"):
        return  # run as normal
    skip_marker = pytest.mark.skip(
        reason="Integration tests skipped — set INTEGRATION_TESTS=true to run"
    )
    for item in items:
        if str(item.fspath).startswith(_INTEGRATION_DIR + os.sep):
            item.add_marker(skip_marker)
