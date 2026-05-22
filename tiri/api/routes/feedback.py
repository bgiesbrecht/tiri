"""Feedback routes — thumbs up/down and propose-examples."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from tiri.api.auth import auth_token
from tiri.engine.room_engine import RoomManager
from tiri.feedback.collector import Collector
from tiri.feedback.proposer import Proposer


router = APIRouter()


def _collector(request: Request) -> Collector:
    return Collector(store=request.app.state.container["store"])


def _proposer(request: Request) -> Proposer:
    container = request.app.state.container
    return Proposer(store=container["store"], llm=container["llm"])


def _manager(request: Request) -> RoomManager:
    container = request.app.state.container
    return RoomManager(
        store=container["store"],
        vector=container["vector"],
        llm=container["llm"],
    )


@router.post(
    "/{room_id}/conversations/{conv_id}/messages/{turn_id}/feedback"
)
async def record_feedback(
    request: Request,
    room_id: str,
    conv_id: str,
    turn_id: str,
    body: dict[str, Any],
    _token: str | None = Depends(auth_token),
) -> dict[str, str]:
    signal = body.get("signal")
    if signal not in ("up", "down"):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": "`signal` must be 'up' or 'down'",
            },
        )
    comment = body.get("comment", "")
    await _collector(request).record(
        conversation_id=conv_id,
        turn_id=turn_id,
        signal=signal,
        comment=comment,
    )
    return {"status": "ok"}


@router.post("/{room_id}/feedback/propose")
async def propose_examples(
    request: Request,
    room_id: str,
    _token: str | None = Depends(auth_token),
) -> dict[str, Any]:
    config = await _manager(request).get(room_id)
    proposed = await _proposer(request).propose(room_id, config)
    return {"proposed_examples": [asdict(ex) for ex in proposed]}
