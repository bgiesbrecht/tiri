"""Tests for tiri.config.

Covers test cases 1–5, 8, 9 from docs/configuration.md. Cases 6, 7, 10 cover
build_container() and are tested when container.py lands (Step 4).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from tiri.config import (
    Config,
    ConfigurationError,
    ProviderBackendConfig,
    RoutingConfig,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _clear_tiri_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every env var Config.load() reads, so tests start from a clean slate."""
    for key in (
        "LLM_PROVIDER",
        "CATALOG_PROVIDER",
        "QUERY_PROVIDER",
        "VECTOR_PROVIDER",
        "STORE_PROVIDER",
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_EMBED_MODEL",
        "ANTHROPIC_MODEL",
        "OLLAMA_BASE_URL",
        "OLLAMA_MODEL",
        "DB_LLM_ENDPOINT",
        "DB_EMBED_ENDPOINT",
        "DB_WAREHOUSE_ID",
        "DB_VECTOR_INDEX",
        "DB_VECTOR_ENDPOINT",
        "DB_STORE_TABLE",
        "STATIC_SCHEMA_FILE",
        "DUCKDB_DATA_DIR",
        "CHROMA_PATH",
        "SQLITE_PATH",
        "AUTH_DISABLED",
        "TIRI_HOST",
        "TIRI_PORT",
        "TIRI_LOG_LEVEL",
        "TIRI_CORS_ORIGINS",
        "TIRI_INTENT_THRESHOLD",
        "TIRI_SQL_MAX_RETRIES",
        "TIRI_QUERY_ROW_LIMIT",
        "TIRI_EXAMPLE_TOP_K",
        "TIRI_HISTORY_WINDOW",
        "TIRI_PLAN_MAX_STEPS",
        "TIRI_METADATA_CACHE_TTL",
        "DEFINITELY_MISSING_VAR",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_tiri_env(monkeypatch)


def _nonexistent_toml(tmp_path: Path) -> str:
    return str(tmp_path / "does-not-exist.toml")


# ── Test case 1: Config.load() with valid tiri.toml parses all backends ────


def test_config_load_parses_valid_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "tok-xyz")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DB_WAREHOUSE_ID", "wh-1")
    monkeypatch.setenv("DB_VECTOR_ENDPOINT", "ep-1")

    toml = tmp_path / "tiri.toml"
    toml.write_text(
        """
[llm.providers.db]
type  = "databricks"
host  = "${DATABRICKS_HOST}"
token = "${DATABRICKS_TOKEN}"

[llm.providers.oai]
type    = "openai"
api_key = "${OPENAI_API_KEY}"

[llm.routing]
intent      = "db::small"
planning    = "db::big"
sql         = "oai::gpt-4o"
synthesis   = "db::big"
clarify     = "db::small"
viz_summary = "db::small"
embed       = "oai::text-embedding-3-small"

[providers.query]
type         = "databricks"
warehouse_id = "${DB_WAREHOUSE_ID}"

[providers.vector]
type     = "databricks"
endpoint = "${DB_VECTOR_ENDPOINT}"
"""
    )
    cfg = Config.load(toml_path=str(toml))

    assert set(cfg.llm_backends) == {"db", "oai"}
    assert cfg.llm_backends["db"].type == "databricks"
    assert cfg.llm_backends["db"].host == "https://example.databricks.com"
    assert cfg.llm_backends["db"].token == "tok-xyz"
    assert cfg.llm_backends["oai"].api_key == "sk-test"
    assert cfg.llm_routing.sql == "oai::gpt-4o"
    assert cfg.llm_routing.embed == "oai::text-embedding-3-small"
    assert cfg.db_warehouse_id == "wh-1"
    assert cfg.db_vector_endpoint == "ep-1"


# ── Test case 2: no tiri.toml → simple-mode single backend ─────────────────


def test_config_load_simple_mode_synthesizes_single_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CATALOG_PROVIDER", "static")
    monkeypatch.setenv("QUERY_PROVIDER", "duckdb")
    monkeypatch.setenv("VECTOR_PROVIDER", "chroma")
    monkeypatch.setenv("STORE_PROVIDER", "sqlite")

    cfg = Config.load(toml_path=_nonexistent_toml(tmp_path))

    assert len(cfg.llm_backends) == 1
    backend = next(iter(cfg.llm_backends.values()))
    assert backend.type == "openai"
    assert backend.api_key == "sk-test"
    # All LLM tasks route to the single backend.
    assert cfg.llm_routing.sql.startswith("openai::")
    assert cfg.llm_routing.embed.startswith("openai::")
    assert cfg.catalog_provider == "static"
    assert cfg.query_provider == "duckdb"
    assert cfg.vector_provider == "chroma"
    assert cfg.store_provider == "sqlite"


def test_config_simple_mode_with_anthropic_falls_back_to_openai_for_embed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anthropic has no embed API — simple mode auto-adds an OpenAI embed backend."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    monkeypatch.setenv("CATALOG_PROVIDER", "static")
    monkeypatch.setenv("QUERY_PROVIDER", "duckdb")
    monkeypatch.setenv("VECTOR_PROVIDER", "chroma")
    monkeypatch.setenv("STORE_PROVIDER", "sqlite")

    cfg = Config.load(toml_path=_nonexistent_toml(tmp_path))

    assert set(cfg.llm_backends) == {"anthropic", "openai"}
    assert cfg.llm_backends["anthropic"].type == "anthropic"
    assert cfg.llm_backends["openai"].type == "openai"
    assert cfg.llm_routing.sql.startswith("anthropic::")
    assert cfg.llm_routing.embed.startswith("openai::")


# ── Test case 3: ${MISSING_VAR} → ConfigurationError naming the variable ───


def test_config_load_unresolved_var_raises_with_var_name(
    tmp_path: Path,
) -> None:
    toml = tmp_path / "tiri.toml"
    toml.write_text(
        """
[llm.providers.x]
type    = "openai"
api_key = "${DEFINITELY_MISSING_VAR}"

[llm.routing]
intent      = "x::m"
planning    = "x::m"
sql         = "x::m"
synthesis   = "x::m"
clarify     = "x::m"
viz_summary = "x::m"
embed       = "x::m"

[providers.query]
type         = "duckdb"

[providers.vector]
type     = "chroma"
"""
    )
    with pytest.raises(ConfigurationError, match="DEFINITELY_MISSING_VAR"):
        Config.load(toml_path=str(toml))


# ── Test case 4: routing to undefined backend → ConfigurationError ─────────


def test_routing_to_undefined_backend_raises(tmp_path: Path) -> None:
    toml = tmp_path / "tiri.toml"
    toml.write_text(
        """
[llm.providers.x]
type    = "openai"
api_key = "key"

[llm.routing]
intent      = "ghost::m"
planning    = "x::m"
sql         = "x::m"
synthesis   = "x::m"
clarify     = "x::m"
viz_summary = "x::m"
embed       = "x::m"

[providers.query]
type = "duckdb"

[providers.vector]
type = "chroma"
"""
    )
    with pytest.raises(ConfigurationError, match=r"ghost"):
        Config.load(toml_path=str(toml))


# ── Test case 5: embed → Anthropic backend → ConfigurationError ────────────


def test_embed_route_to_anthropic_raises(tmp_path: Path) -> None:
    toml = tmp_path / "tiri.toml"
    toml.write_text(
        """
[llm.providers.ant]
type    = "anthropic"
api_key = "key"

[llm.routing]
intent      = "ant::m"
planning    = "ant::m"
sql         = "ant::m"
synthesis   = "ant::m"
clarify     = "ant::m"
viz_summary = "ant::m"
embed       = "ant::m"

[providers.query]
type = "duckdb"

[providers.vector]
type = "chroma"
"""
    )
    with pytest.raises(ConfigurationError, match=r"[Aa]nthropic"):
        Config.load(toml_path=str(toml))


# ── Test case 8: components MUST NOT import os.environ or tomllib directly ─


def test_no_module_reads_env_or_toml_directly() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    tiri_dir = repo_root / "tiri"
    forbidden = (
        re.compile(r"\bos\.environ\b"),
        re.compile(r"\bos\.getenv\b"),
        re.compile(r"\btomllib\b"),
    )
    violations: list[str] = []
    for py in tiri_dir.rglob("*.py"):
        if py.name == "config.py":
            continue
        text = py.read_text()
        for pattern in forbidden:
            if pattern.search(text):
                violations.append(
                    f"{py.relative_to(repo_root)}: matches {pattern.pattern}"
                )
    assert not violations, (
        "Only tiri/config.py may read os.environ / os.getenv / tomllib; "
        "violations:\n  " + "\n  ".join(violations)
    )


# ── CLAUDE.md rule 1: engine/ and knowledge/ MUST NOT import SDKs directly ──


def test_engine_and_knowledge_do_not_import_sdks_directly() -> None:
    """tiri/engine/ and tiri/knowledge/ MUST NOT import requests, databricks,
    openai, anthropic, duckdb, chromadb, or sqlite3. All I/O goes through the
    provider interfaces defined in tiri/providers/base.py.

    tiri/providers/ is explicitly allowed to import these SDKs — that is where
    the implementations live.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    forbidden_sdks = (
        "requests",
        "databricks",
        "openai",
        "anthropic",
        "duckdb",
        "chromadb",
        "sqlite3",
    )
    # Match `import X`, `import X.sub`, `from X import ...`, `from X.sub import ...`.
    patterns = [
        re.compile(rf"^\s*(?:import|from)\s+{re.escape(sdk)}\b", re.MULTILINE)
        for sdk in forbidden_sdks
    ]
    violations: list[str] = []
    for sub in ("engine", "knowledge"):
        sub_dir = repo_root / "tiri" / sub
        if not sub_dir.exists():
            continue  # not yet built
        for py in sub_dir.rglob("*.py"):
            text = py.read_text()
            for sdk, pattern in zip(forbidden_sdks, patterns):
                if pattern.search(text):
                    violations.append(
                        f"{py.relative_to(repo_root)}: imports {sdk!r} "
                        "(engine/knowledge must use provider interfaces)"
                    )
    assert not violations, (
        "Engine and knowledge layers must use provider interfaces, not SDKs:\n  "
        + "\n  ".join(violations)
    )


# ── Test case 9: auth_disabled=True MUST log a WARNING ─────────────────────


def test_auth_disabled_logs_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("CATALOG_PROVIDER", "static")
    monkeypatch.setenv("QUERY_PROVIDER", "duckdb")
    monkeypatch.setenv("VECTOR_PROVIDER", "chroma")
    monkeypatch.setenv("STORE_PROVIDER", "sqlite")

    with caplog.at_level(logging.WARNING, logger="tiri.config"):
        cfg = Config.load(toml_path=_nonexistent_toml(tmp_path))

    assert cfg.auth_disabled is True
    assert any(
        "auth_disabled" in r.message.lower() or "auth" in r.message.lower()
        for r in caplog.records
    ), f"expected an auth-related WARNING, got: {[r.message for r in caplog.records]}"


def test_auth_disabled_false_does_not_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("CATALOG_PROVIDER", "static")
    monkeypatch.setenv("QUERY_PROVIDER", "duckdb")
    monkeypatch.setenv("VECTOR_PROVIDER", "chroma")
    monkeypatch.setenv("STORE_PROVIDER", "sqlite")

    with caplog.at_level(logging.WARNING, logger="tiri.config"):
        cfg = Config.load(toml_path=_nonexistent_toml(tmp_path))

    assert cfg.auth_disabled is False
    assert not any("auth" in r.message.lower() for r in caplog.records)


# ── Additional validation coverage ─────────────────────────────────────────


def test_databricks_query_without_warehouse_id_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "databricks")
    monkeypatch.setenv("DATABRICKS_HOST", "https://x")
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    monkeypatch.setenv("QUERY_PROVIDER", "databricks")
    monkeypatch.setenv("VECTOR_PROVIDER", "chroma")
    # DB_WAREHOUSE_ID intentionally unset
    monkeypatch.setenv("STORE_PROVIDER", "sqlite")
    monkeypatch.setenv("CATALOG_PROVIDER", "static")

    with pytest.raises(ConfigurationError, match="db_warehouse_id"):
        Config.load(toml_path=_nonexistent_toml(tmp_path))


def test_databricks_vector_without_endpoint_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "databricks")
    monkeypatch.setenv("DATABRICKS_HOST", "https://x")
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    monkeypatch.setenv("QUERY_PROVIDER", "duckdb")
    monkeypatch.setenv("VECTOR_PROVIDER", "databricks")
    # DB_VECTOR_ENDPOINT intentionally unset
    monkeypatch.setenv("STORE_PROVIDER", "sqlite")
    monkeypatch.setenv("CATALOG_PROVIDER", "static")

    with pytest.raises(ConfigurationError, match="db_vector_endpoint"):
        Config.load(toml_path=_nonexistent_toml(tmp_path))


def test_missing_routing_task_raises(tmp_path: Path) -> None:
    """Every task assignment is required — no implicit defaults."""
    toml = tmp_path / "tiri.toml"
    toml.write_text(
        """
[llm.providers.x]
type    = "openai"
api_key = "key"

[llm.routing]
intent      = "x::m"
sql         = "x::m"
# planning omitted — should error
synthesis   = "x::m"
clarify     = "x::m"
viz_summary = "x::m"
embed       = "x::m"

[providers.query]
type = "duckdb"

[providers.vector]
type = "chroma"
"""
    )
    with pytest.raises(ConfigurationError, match="planning"):
        Config.load(toml_path=str(toml))


def test_nested_var_substitution_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """${VAR} substitution applies inside larger strings, multiple times per line."""
    monkeypatch.setenv("CATALOG_ENV", "sf1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    toml = tmp_path / "tiri.toml"
    toml.write_text(
        """
[llm.providers.x]
type    = "openai"
api_key = "${OPENAI_API_KEY}"

[llm.routing]
intent      = "x::m"
planning    = "x::m"
sql         = "x::m"
synthesis   = "x::m"
clarify     = "x::m"
viz_summary = "x::m"
embed       = "x::m"

[providers.query]
type         = "duckdb"

[providers.vector]
type     = "chroma"

[providers.store]
type  = "sqlite"
table = "tpch.${CATALOG_ENV}.kv_store"
"""
    )
    cfg = Config.load(toml_path=str(toml))
    assert cfg.db_store_table == "tpch.sf1.kv_store"


def test_engine_tuning_env_vars_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("CATALOG_PROVIDER", "static")
    monkeypatch.setenv("QUERY_PROVIDER", "duckdb")
    monkeypatch.setenv("VECTOR_PROVIDER", "chroma")
    monkeypatch.setenv("STORE_PROVIDER", "sqlite")
    monkeypatch.setenv("TIRI_INTENT_THRESHOLD", "0.55")
    monkeypatch.setenv("TIRI_SQL_MAX_RETRIES", "5")
    monkeypatch.setenv("TIRI_PORT", "9000")

    cfg = Config.load(toml_path=_nonexistent_toml(tmp_path))

    assert cfg.intent_threshold == 0.55
    assert cfg.sql_max_retries == 5
    assert cfg.port == 9000


def test_metadata_provider_stack_is_parsed_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    toml = tmp_path / "tiri.toml"
    toml.write_text(
        """
[llm.providers.x]
type    = "openai"
api_key = "${OPENAI_API_KEY}"

[llm.routing]
intent      = "x::m"
planning    = "x::m"
sql         = "x::m"
synthesis   = "x::m"
clarify     = "x::m"
viz_summary = "x::m"
embed       = "x::m"

[providers.query]
type = "duckdb"

[providers.vector]
type = "chroma"

[[metadata.providers.stack]]
name = "uc_annotations"
type = "uc_annotations"

[[metadata.providers.stack]]
name = "domain_yaml"
type = "yaml"
path = "./meta.yaml"
"""
    )
    cfg = Config.load(toml_path=str(toml))
    assert [m["name"] for m in cfg.metadata_provider_configs] == [
        "uc_annotations",
        "domain_yaml",
    ]
    assert cfg.metadata_provider_configs[1]["path"] == "./meta.yaml"
