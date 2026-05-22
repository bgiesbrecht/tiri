"""Management routes — room CRUD and re-indexing trigger."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from tiri.api.auth import auth_token
from tiri.data_models import RoomConfig
from tiri.engine.room_engine import RoomManager, RoomNotFoundError


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
