"""MCP server endpoint (EXT-4).

Exposes Tiri rooms as tools to any MCP-compatible client (Claude, Cursor,
VS Code, other agents). Implements the MCP Streamable-HTTP transport — a
JSON-RPC 2.0 request/response over a single POST endpoint — because that
transport is genuinely simple and avoids depending on an external library
whose stability can't be vouched for at this stage of the project.

Mount point: `/mcp` (single POST endpoint). The existing REST API at
`/rooms/*` is untouched and shares no state with this router.

Tools (per docs/extensions.md EXT-4):
  - tiri_query        — ask a natural-language question against a room.
                        Threads conversation_id for multi-turn context.
  - tiri_list_rooms   — list available rooms.
  - tiri_room_schema  — return tables + domain instruction for a room.

Auth: same Bearer / X-Forwarded-Access-Token model as the REST API, so the
user_token reaches QueryProvider.execute for EXT-6 RBAC. Auth failure
returns a JSON-RPC error (code -32001) with HTTP 200 — NOT HTTP 401 — so
MCP clients see a protocol-level error and not a transport-level one. This
matches the EXT-4 test case 5 requirement.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from tiri.data_models import RoomConfig
from tiri.engine.agents.synthesis_agent import SynthesisError
from tiri.engine.room_engine import (
    RoomEngine,
    RoomManager,
    RoomNotFoundError,
)


_log = logging.getLogger("tiri.api.mcp")

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "tiri"
_SERVER_VERSION = "0.1.0"
_BEARER_PREFIX = "Bearer "

# JSON-RPC error codes (-32000 to -32099 is the implementation-defined range)
_AUTH_REQUIRED = -32001
_INVALID_PARAMS = -32602
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603
_PARSE_ERROR = -32700

router = APIRouter()


# ── Tool definitions (returned by tools/list) ──────────────────────────────


_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "tiri_query",
        "description": (
            "Ask a natural-language question against a Tiri room. Returns "
            "the synthesized answer, the SQL used, row count, and the "
            "conversation_id (for follow-up questions in the same context). "
            "Call tiri_list_rooms first to pick a room_id, then "
            "tiri_room_schema if you need to verify the room covers the "
            "data you need."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "room_id": {
                    "type": "string",
                    "description": "Identifier of the room to query.",
                },
                "question": {
                    "type": "string",
                    "description": "Natural-language question.",
                },
                "conversation_id": {
                    "type": "string",
                    "description": (
                        "Optional. Pass the value returned by a prior "
                        "tiri_query call to continue the same conversation. "
                        "Omit to start a new conversation."
                    ),
                },
            },
            "required": ["room_id", "question"],
        },
    },
    {
        "name": "tiri_list_rooms",
        "description": (
            "List available Tiri rooms with their titles and descriptions. "
            "Use this before tiri_query to find the right room."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "tiri_room_schema",
        "description": (
            "Return the tables and domain instruction for a room. Use to "
            "verify a room covers the data you need before querying."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "room_id": {"type": "string"},
            },
            "required": ["room_id"],
        },
    },
]


# ── Request handling ───────────────────────────────────────────────────────


@router.post("")
async def mcp_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
    x_forwarded_access_token: str | None = Header(default=None),
) -> JSONResponse:
    """Single JSON-RPC endpoint. Each POST is one request, one response.

    Returns HTTP 200 with a JSON-RPC error body on auth failure rather than
    HTTP 401 (EXT-4 test case 5). MCP clients expect protocol-level errors,
    not transport-level ones.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _rpc_error(None, _PARSE_ERROR, "Invalid JSON")

    rpc_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _rpc_error(rpc_id, _INVALID_PARAMS, "Not a JSON-RPC 2.0 request")

    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # Auth: same precedence as REST (Authorization Bearer first, X-Forwarded
    # fallback). Errors come back as JSON-RPC errors, not HTTP 401.
    cfg = request.app.state.cfg
    user_token: str | None = None
    if not getattr(cfg, "auth_disabled", False):
        token = _extract_token(authorization, x_forwarded_access_token)
        if token is None:
            return _rpc_error(
                rpc_id,
                _AUTH_REQUIRED,
                "Authentication required: provide Authorization: Bearer "
                "<token> or X-Forwarded-Access-Token.",
            )
        user_token = token

    if method == "initialize":
        return _rpc_result(
            rpc_id,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
                "capabilities": {"tools": {}},
            },
        )

    if method == "tools/list":
        return _rpc_result(rpc_id, {"tools": _TOOL_DEFS})

    if method == "tools/call":
        return await _handle_tool_call(rpc_id, params, request, user_token)

    return _rpc_error(rpc_id, _METHOD_NOT_FOUND, f"Unknown method: {method!r}")


async def _handle_tool_call(
    rpc_id: Any,
    params: dict[str, Any],
    request: Request,
    user_token: str | None,
) -> JSONResponse:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _rpc_error(rpc_id, _INVALID_PARAMS, "`arguments` must be an object")

    if name == "tiri_query":
        return await _tool_query(rpc_id, arguments, request, user_token)
    if name == "tiri_list_rooms":
        return await _tool_list_rooms(rpc_id, request)
    if name == "tiri_room_schema":
        return await _tool_room_schema(rpc_id, arguments, request)

    return _rpc_error(rpc_id, _METHOD_NOT_FOUND, f"Unknown tool: {name!r}")


# ── Tools ──────────────────────────────────────────────────────────────────


async def _tool_query(
    rpc_id: Any,
    arguments: dict[str, Any],
    request: Request,
    user_token: str | None,
) -> JSONResponse:
    room_id = arguments.get("room_id")
    question = arguments.get("question")
    conversation_id = arguments.get("conversation_id") or uuid.uuid4().hex
    if not isinstance(room_id, str) or not room_id:
        return _rpc_error(
            rpc_id, _INVALID_PARAMS, "`room_id` is required and must be a string"
        )
    if not isinstance(question, str) or not question.strip():
        return _rpc_error(
            rpc_id, _INVALID_PARAMS, "`question` is required and must be a non-empty string"
        )

    engine = _build_engine(request)
    try:
        turn = await engine.chat(
            room_id=room_id,
            conversation_id=conversation_id,
            question=question,
            user_token=user_token,
        )
    except RoomNotFoundError as e:
        return _tool_error(rpc_id, f"Room not found: {e}")
    except SynthesisError as e:
        # Causal-language violation or unparseable synthesis response.
        # Surface as a tool error rather than a transport error.
        return _tool_error(rpc_id, f"Synthesis failed: {e}")

    text = _format_turn_text(turn, conversation_id)
    structured = _structured_turn(turn, conversation_id)
    return _rpc_result(
        rpc_id,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
            "isError": turn.error is not None,
        },
    )


async def _tool_list_rooms(rpc_id: Any, request: Request) -> JSONResponse:
    store = request.app.state.container["store"]
    keys = await store.list_keys("room:")
    rooms: list[dict[str, Any]] = []
    for key in keys:
        if not key.endswith(":config"):
            continue
        raw = await store.get(key)
        if not isinstance(raw, dict):
            continue
        try:
            cfg = RoomConfig.from_dict(raw)
        except Exception:
            _log.exception("Skipping malformed room config at key %s", key)
            continue
        rooms.append(
            {
                "room_id": cfg.room_id,
                "title": cfg.title,
                "description": cfg.text_instruction or "",
                "table_count": len(cfg.tables),
            }
        )
    rooms.sort(key=lambda r: r["room_id"])
    text = (
        "Available rooms:\n"
        + "\n".join(
            f"- {r['room_id']}: {r['title']} ({r['table_count']} tables)"
            for r in rooms
        )
        if rooms
        else "No rooms are configured."
    )
    return _rpc_result(
        rpc_id,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"rooms": rooms},
            "isError": False,
        },
    )


async def _tool_room_schema(
    rpc_id: Any, arguments: dict[str, Any], request: Request
) -> JSONResponse:
    room_id = arguments.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        return _rpc_error(rpc_id, _INVALID_PARAMS, "`room_id` is required")

    manager = _build_manager(request)
    try:
        config = await manager.get(room_id)
    except RoomNotFoundError as e:
        return _tool_error(rpc_id, f"Room not found: {e}")

    text_lines = [
        f"Room: {config.title} ({config.room_id})",
        f"Description: {config.text_instruction or '(none)'}",
        "Tables:",
        *[f"  - {t}" for t in config.tables],
    ]
    return _rpc_result(
        rpc_id,
        {
            "content": [{"type": "text", "text": "\n".join(text_lines)}],
            "structuredContent": {
                "room_id": config.room_id,
                "title": config.title,
                "description": config.text_instruction or "",
                "tables": list(config.tables),
            },
            "isError": False,
        },
    )


# ── Engine wiring (mirrors what conversations/management routes do) ────────


def _build_engine(request: Request) -> RoomEngine:
    container = request.app.state.container
    return RoomEngine(
        llm=container["llm"],
        catalog=container["catalog"],
        metadata_providers=container.get("metadata_providers", []),
        query=container["query"],
        vector=container["vector"],
        store=container["store"],
        mcp_providers=container.get("mcp_providers", {}),
    )


def _build_manager(request: Request) -> RoomManager:
    container = request.app.state.container
    return RoomManager(
        store=container["store"],
        vector=container["vector"],
        llm=container["llm"],
    )


# ── Formatting ─────────────────────────────────────────────────────────────


def _format_turn_text(turn, conversation_id: str) -> str:
    """Human-readable prose for the text content block. MCP clients
    typically surface this directly to the user (or to the calling agent).

    Prefers the SynthesizedAnswer prose when present (every multi-step
    turn and any single-step turn with medium/low confidence). Falls back
    to a compact result summary otherwise."""
    if turn.error:
        return f"Error: {turn.error}\n\nconversation_id: {conversation_id}"

    if turn.clarification_question:
        return (
            f"Clarification needed: {turn.clarification_question}\n\n"
            f"conversation_id: {conversation_id}"
        )

    lines: list[str] = []
    if turn.synthesized_answer is not None:
        sa = turn.synthesized_answer
        lines.append(sa.answer)
        if sa.data_supports:
            lines.append("\nData supports:")
            lines.extend(f"  - {b}" for b in sa.data_supports)
        if sa.data_does_not_support:
            lines.append("\nData does NOT support:")
            lines.extend(f"  - {b}" for b in sa.data_does_not_support)
        if sa.would_need:
            lines.append("\nWould need:")
            lines.extend(f"  - {b}" for b in sa.would_need)
        lines.append(f"\nConfidence: {sa.confidence} ({sa.confidence_rationale})")
    elif turn.viz and turn.viz.summary:
        lines.append(turn.viz.summary)
    else:
        lines.append("(Query completed.)")

    if turn.sql:
        lines.append(f"\nSQL:\n  {turn.sql}")
    if turn.query_result is not None:
        lines.append(
            f"\nRow count: {turn.query_result.row_count}"
            f"{' (truncated)' if turn.query_result.truncated else ''}"
        )
    lines.append(f"\nconversation_id: {conversation_id}")
    return "\n".join(lines)


def _structured_turn(turn, conversation_id: str) -> dict[str, Any]:
    """Compact structured payload alongside the prose text — for clients
    that want to programmatically inspect the answer fields. Deliberately
    NOT the full ConversationTurn JSON (that's a REST concern); only the
    fields useful to an MCP caller composing further actions."""
    return {
        "conversation_id": conversation_id,
        "turn_id": turn.turn_id,
        "answer": (
            turn.synthesized_answer.answer
            if turn.synthesized_answer is not None
            else (turn.viz.summary if turn.viz else None)
        ),
        "sql": turn.sql,
        "row_count": (
            turn.query_result.row_count if turn.query_result is not None else 0
        ),
        "confidence": (
            turn.synthesized_answer.confidence
            if turn.synthesized_answer is not None
            else None
        ),
        "error": turn.error,
        "clarification_question": turn.clarification_question,
    }


# ── JSON-RPC plumbing ──────────────────────────────────────────────────────


def _rpc_result(rpc_id: Any, result: Any) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"jsonrpc": "2.0", "id": rpc_id, "result": result},
    )


def _rpc_error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        },
    )


def _tool_error(rpc_id: Any, message: str) -> JSONResponse:
    """Tool-level error: HTTP 200 + JSON-RPC result with isError=true.

    This is the MCP convention for "the tool ran but returned an error"
    as opposed to "the protocol itself failed". The latter uses
    `_rpc_error` (a JSON-RPC error object)."""
    return _rpc_result(
        rpc_id,
        {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        },
    )


def _extract_token(
    authorization: str | None, x_forwarded: str | None
) -> str | None:
    """Same precedence as tiri.api.auth: Authorization Bearer first, then
    X-Forwarded-Access-Token. Returns None if neither is present."""
    if authorization and authorization.startswith(_BEARER_PREFIX):
        token = authorization[len(_BEARER_PREFIX):].strip()
        if token:
            return token
    if x_forwarded and x_forwarded.strip():
        return x_forwarded.strip()
    return None
