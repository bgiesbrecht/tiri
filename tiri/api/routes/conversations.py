"""Conversation routes — start, send message (blocking + SSE), list turns."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from tiri.api.auth import auth_token
from tiri.engine.room_engine import RoomEngine, RoomManager


router = APIRouter()


def _engine(request: Request) -> RoomEngine:
    container = request.app.state.container
    cfg = request.app.state.cfg
    return RoomEngine(
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


def _manager(request: Request) -> RoomManager:
    container = request.app.state.container
    return RoomManager(
        store=container["store"],
        vector=container["vector"],
        llm=container["llm"],
    )


@router.post("/{room_id}/conversations", status_code=201)
async def start_conversation(
    request: Request,
    room_id: str,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Create a new conversation_id under a room. Room must exist (404 otherwise).

    No store write happens here — the conversation is registered under the
    room when the first turn is persisted (see RoomEngine._persist_turn).
    """
    await _manager(request).get(room_id)  # raises RoomNotFoundError → 404
    return {"conversation_id": uuid.uuid4().hex}


@router.post("/{room_id}/conversations/{conv_id}/messages")
async def send_message(
    request: Request,
    room_id: str,
    conv_id: str,
    body: dict[str, Any],
    user_token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": "`question` must be a non-empty string",
            },
        )
    model_override = body.get("model_override") or None
    engine = _engine(request)
    turn = await engine.chat(
        room_id=room_id,
        conversation_id=conv_id,
        question=question,
        user_token=user_token,
        model_override=model_override if isinstance(model_override, str) else None,
    )
    return asdict(turn)


@router.get("/{room_id}/conversations/{conv_id}/messages/stream")
async def stream_messages(
    request: Request,
    room_id: str,
    conv_id: str,
    question: str,
    model_override: str | None = None,
    user_token: str | None = Depends(auth_token),
) -> StreamingResponse:
    """SSE endpoint. The question comes in as a query parameter — GET
    requests don't have bodies. The non-streaming POST takes it in the body.

    `model_override` is also a query parameter (same constraint — no body
    on GET). UI passes `?question=…&model_override=anthropic::claude-sonnet-4-6`
    to pin a single chat invocation to a specific backend for side-by-side
    comparison.
    """
    if not question.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": "`question` query parameter must be non-empty",
            },
        )
    engine = _engine(request)

    async def event_source():
        async for event in engine.stream_chat(
            room_id=room_id,
            conversation_id=conv_id,
            question=question,
            user_token=user_token,
            model_override=model_override,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.get("/{room_id}/conversations/{conv_id}/messages")
async def list_messages(
    request: Request,
    room_id: str,
    conv_id: str,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    """Return every turn in a conversation, in creation order."""
    store = request.app.state.container["store"]
    index = await store.get(f"conv:{conv_id}:index")
    turn_ids = (
        list(index.get("turn_ids", [])) if isinstance(index, dict) else []
    )
    turns: list[dict[str, Any]] = []
    for turn_id in turn_ids:
        raw = await store.get(f"conv:{conv_id}:turn:{turn_id}")
        if raw is not None:
            turns.append(raw)
    return {"turns": turns}
