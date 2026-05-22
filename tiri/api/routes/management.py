"""Management routes — room CRUD and re-indexing trigger."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from tiri.api.auth import auth_token
from tiri.data_models import ColumnMeta, RoomConfig, SchemaMeta, TableMeta
from tiri.engine.room_engine import RoomManager, RoomNotFoundError
from tiri.knowledge.metadata_fetcher import MetadataFetcher
from tiri.providers.base import TableNotFoundError


_log = logging.getLogger("tiri.api.management")
router = APIRouter()


def _manager(request: Request) -> RoomManager:
    container = request.app.state.container
    return RoomManager(
        store=container["store"],
        vector=container["vector"],
        llm=container["llm"],
    )


@router.post("", status_code=201)
async def create_room(
    request: Request,
    body: dict[str, Any],
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Create a new room.

    If the body omits `room_id`, one is generated server-side. The body
    otherwise mirrors the `RoomConfig` dataclass shape — see
    `docs/data_models.md`.
    """
    payload = dict(body)
    if not payload.get("room_id"):
        payload["room_id"] = uuid.uuid4().hex[:12]
    payload.setdefault("title", payload["room_id"])
    config = RoomConfig.from_dict(payload)
    manager = _manager(request)
    room_id = await manager.create(config)
    return {"room_id": room_id, "config": asdict(config)}


@router.get("/{room_id}")
async def get_room(
    request: Request,
    room_id: str,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    config = await _manager(request).get(room_id)
    return asdict(config)


@router.patch("/{room_id}")
async def patch_room(
    request: Request,
    room_id: str,
    body: dict[str, Any],
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    config = await _manager(request).update(room_id, body)
    return asdict(config)


@router.delete("/{room_id}", status_code=204)
async def delete_room(
    request: Request,
    room_id: str,
    _token: str | None = Depends(auth_token),
) -> None:
    manager = _manager(request)
    # Verify existence first so DELETE on a missing room is a clear 404.
    await manager.get(room_id)
    await manager.delete(room_id)


@router.post("/{room_id}/index", status_code=202)
async def trigger_reindex(
    request: Request,
    room_id: str,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Re-index examples asynchronously. Returns immediately."""
    manager = _manager(request)
    config = await manager.get(room_id)
    # Fire-and-forget: launch the re-index without awaiting it.
    asyncio.create_task(_reindex_safely(manager, config))
    return {"status": "indexing", "room_id": room_id}


async def _reindex_safely(manager: RoomManager, config: RoomConfig) -> None:
    try:
        await manager._indexer.index(config)
    except Exception:
        _log.exception(
            "Background re-index failed for room %s", config.room_id
        )


@router.post("/{room_id}/benchmarks/run")
async def run_benchmarks(
    request: Request,
    room_id: str,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Run every benchmark in the room and return the report."""
    from dataclasses import asdict as _asdict

    from tiri.engine.room_engine import RoomEngine
    from tiri.feedback.benchmark_runner import BenchmarkRunner

    container = request.app.state.container
    cfg = request.app.state.cfg
    engine = RoomEngine(
        llm=container["llm"],
        catalog=container["catalog"],
        metadata_providers=container["metadata_providers"],
        query=container["query"],
        vector=container["vector"],
        store=container["store"],
        mcp_providers=container.get("mcp_providers", {}),
        llm_backends=container.get("llm_backends", {}),
        history_window=cfg.history_window,
        intent_threshold=cfg.intent_threshold,
        sql_max_retries=cfg.sql_max_retries,
        query_row_limit=cfg.query_row_limit,
    )
    runner = BenchmarkRunner(engine=engine, store_query=container["query"])
    report = await runner.run(room_id)
    return _asdict(report)


@router.post("/{room_id}/benchmarks", status_code=201)
async def add_benchmark(
    request: Request,
    room_id: str,
    body: dict[str, Any],
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Append a benchmark to the room config."""
    from tiri.data_models import Benchmark

    bench = Benchmark(**body)
    current = await _manager(request).get(room_id)
    current.benchmarks.append(bench)
    await _manager(request).update(
        room_id, {"benchmarks": [_dict_for_benchmark(b) for b in current.benchmarks]}
    )
    return {"benchmark_id": bench.id}


@router.delete("/{room_id}/benchmarks/{benchmark_id}", status_code=204)
async def delete_benchmark(
    request: Request,
    room_id: str,
    benchmark_id: str,
    _token: str | None = Depends(auth_token),
) -> None:
    current = await _manager(request).get(room_id)
    new_list = [b for b in current.benchmarks if b.id != benchmark_id]
    await _manager(request).update(
        room_id, {"benchmarks": [_dict_for_benchmark(b) for b in new_list]}
    )


def _dict_for_benchmark(b) -> dict[str, Any]:
    from dataclasses import asdict as _asdict

    return _asdict(b)


# ─── Table metadata inspector ─────────────────────────────────────────────
# Read-only views over the fully-resolved metadata stack. These endpoints
# run MetadataFetcher (catalog + every MetadataProvider in declared order
# + RoomConfigMetadataProvider) and serialize the merged TableMeta. No LLM
# calls, no SQL execution — purely a stack of catalog + metadata-provider
# reads. ContextBuilder is intentionally NOT used here because it makes an
# embed() call in ExampleIndexer.retrieve() which is wasted for inspection.


@router.get("/{room_id}/tables")
async def list_room_tables(
    request: Request,
    room_id: str,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Merged metadata for every table in the room.

    The merge runs the full stack: CatalogProvider → external metadata
    providers in declared order → RoomConfigMetadataProvider (always
    last). Returns the same `TableMeta` shape the LLM sees, plus a
    per-column `conflicts` slice derived from `TableMeta.conflicts`.
    Also includes `schemas` — one entry per unique `catalog.schema`
    prefix referenced by the room's tables, populated via
    `MetadataProvider.enrich_schemas` (default no-op).
    """
    config = await _manager(request).get(room_id)
    fetcher = _build_metadata_fetcher(request)
    tables, schemas = await fetcher.fetch_all(config)
    return {
        "room_id": room_id,
        "schemas": [
            _serialize_schema_meta(schemas[name])
            for name in sorted(schemas)
        ],
        "tables": [
            _serialize_table_meta(tables[name])
            for name in config.tables
            if name in tables
        ],
    }


@router.get("/{room_id}/tables/{table_name:path}")
async def get_room_table(
    request: Request,
    room_id: str,
    table_name: str,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Merged metadata for one table in the room.

    `table_name` is the fully-qualified name (e.g.
    `samples.tpch.lineitem`). 404 if the table is not declared in the
    room config. The `:path` converter is required because FQNs contain
    dots that FastAPI's default string converter handles fine, but we
    keep `:path` so future schema-only paths (e.g. `samples.tpch`) work
    without a separate route.
    """
    config = await _manager(request).get(room_id)
    if table_name not in config.tables:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "table_not_in_room",
                "message": f"Table {table_name!r} is not declared in room {room_id!r}",
            },
        )
    try:
        tables = await _build_metadata_fetcher(request).fetch(config)
    except TableNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "table_not_found", "message": str(exc)},
        ) from exc
    if table_name not in tables:
        # MetadataFetcher returned without this table — should not happen
        # in normal operation, but guard so we never serialize None.
        raise HTTPException(
            status_code=404,
            detail={
                "error": "table_not_found",
                "message": f"Table {table_name!r} could not be resolved by the catalog",
            },
        )
    return _serialize_table_meta(tables[table_name])


def _build_metadata_fetcher(request: Request) -> MetadataFetcher:
    container = request.app.state.container
    return MetadataFetcher(
        catalog=container["catalog"],
        metadata_providers=container["metadata_providers"],
    )


def _serialize_schema_meta(s: SchemaMeta) -> dict[str, Any]:
    return {
        "name": s.full_name,
        "description": s.description,
        "domain": s.domain,
        "freshness": s.freshness,
        "owner": s.owner,
        "synonyms": list(s.synonyms),
        "notes": s.notes,
        "metadata_sources": list(s.metadata_sources),
    }


def _serialize_table_meta(t: TableMeta) -> dict[str, Any]:
    """TableMeta → JSON-friendly dict with per-column conflict slicing.

    `MetadataConflict` records live on TableMeta but carry a `column`
    field. We attach each column-scoped conflict to its column entry and
    keep table-scoped conflicts (column is None) at the top level — this
    matches the UI panel layout (fields card vs. columns card).
    """
    column_conflicts: dict[str, list[dict[str, Any]]] = {}
    table_conflicts: list[dict[str, Any]] = []
    for conflict in t.conflicts:
        record = {
            "field": conflict.field,
            "values": dict(conflict.values),
            "resolved_to": conflict.resolved_to,
        }
        if conflict.column:
            column_conflicts.setdefault(conflict.column, []).append(record)
        else:
            table_conflicts.append(record)

    return {
        "name": t.full_name,
        "description": t.description,
        "synonyms": list(t.synonyms),
        "grain": t.grain,
        "domain": t.domain,
        "freshness": t.freshness,
        "default_date_column": t.default_date_column,
        "default_filter": t.default_filter,
        "recommended_joins": list(t.recommended_joins),
        "row_count": t.row_count,
        "metadata_sources": list(t.metadata_sources),
        "conflicts": table_conflicts,
        "columns": [
            _serialize_column_meta(c, column_conflicts.get(c.name, []))
            for c in t.columns
        ],
    }


def _serialize_column_meta(
    c: ColumnMeta, conflicts: list[dict[str, Any]]
) -> dict[str, Any]:
    """ColumnMeta → JSON-friendly dict.

    `metadata_sources` is emitted as a list per the API spec even though
    ColumnMeta tracks only the winning source as a scalar. The list
    includes the winning source plus any losing sources surfaced via
    per-column conflicts — the UI uses this to render source badges.
    """
    sources = [c.metadata_source] if c.metadata_source else []
    for conflict in conflicts:
        for provider in conflict["values"]:
            if provider not in sources:
                sources.append(provider)
    return {
        "name": c.name,
        "data_type": c.data_type,
        "description": c.description,
        "synonyms": list(c.synonyms),
        "sample_values": list(c.sample_values),
        "value_description": c.value_description,
        "semantic_type": c.semantic_type,
        "currency_code": c.currency_code,
        "date_format": c.date_format,
        "is_primary_key": c.is_primary_key,
        "is_foreign_key": c.is_foreign_key,
        "foreign_key_table": c.foreign_key_table,
        "foreign_key_column": c.foreign_key_column,
        "is_high_cardinality": c.is_high_cardinality,
        "exclude_from_select_star": c.exclude_from_select_star,
        "metadata_sources": sources,
        "conflicts": conflicts,
    }
