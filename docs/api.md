---
tags: [layer/surface]
status: stable
depends_on: [room_engine, data_models]
---

# API

## In this system

**Linked from:** [[README]]
**Links to:** [[room_engine]], [[data_models]], [[feedback]]
**Layer:** surface

---

## What this is

The external interface of the system: a FastAPI application exposing REST endpoints for conversations, room management, and feedback. The API layer calls [[room_engine]] and [[feedback]] — it contains no business logic of its own.

Two route groups map to the two API types in native Genie:
- **Conversation API** — stateful multi-turn conversation with a room
- **Management API** — CRUD for rooms and knowledge store config

A third group handles feedback, documented in [[feedback]].

---

## Authentication

All endpoints require an authenticated request. Token-source precedence:

1. **`Authorization: Bearer <token>`** — standard API clients (curl, SDK calls, external services).
2. **`X-Forwarded-Access-Token: <token>`** — fallback when no `Authorization` header is present. Databricks Apps automatically injects this header with the logged-in user's token, so a browser-based deployment of Tiri gets per-user auth with no client-side changes.

The token's *presence* is enforced at the FastAPI layer. Its *validity* is not — that is delegated to the data warehouse when EXT-6 forwards the token to `QueryProvider.execute()`. Unity Catalog rejects invalid tokens at query time, which is the authoritative trust boundary.

In local development, set `AUTH_DISABLED=true` to skip the check entirely.

---

## Conversation API

### `POST /rooms/{room_id}/conversations`

Start a new conversation.

**Response 201:**
```json
{"conversation_id": "<uuid>"}
```

### `POST /rooms/{room_id}/conversations/{conv_id}/messages`

Send a question and receive the full response (blocking).

**Request:**
```json
{"question": "What were total sales last month?"}
```

**Response 200:** `ConversationTurn` serialized to JSON (see [[data_models]]).

**Response 404:** room or conversation not found
**Response 422:** missing or invalid `question` field

### `GET /rooms/{room_id}/conversations/{conv_id}/messages/stream`

Send a question and receive a streaming response via Server-Sent Events.

**Query param:** `?question=<encoded question>`

**Response:** `text/event-stream` with events as defined in [[room_engine]] `stream_chat()`. The full sequence (when every extension is active and applicable):

```
data: {"type": "status", "text": "Building context..."}
data: {"type": "mcp_context", "entries": [...]}                              # EXT-5, only when populated
data: {"type": "status", "text": "Classifying question..."}
data: {"type": "status", "text": "Planning..."}                              # EXT-1
data: {"type": "plan", "steps": [...], "synthesis_instruction": "..."}       # EXT-1, multi-step only
data: {"type": "sql", "sql": "<step_1 sql>"}
data: {"type": "result", "columns": [...], "rows": [...], "truncated": bool}
data: {"type": "steps", "results": [...]}                                    # EXT-1, multi-step only
data: {"type": "viz", "spec": {...}, "summary": "..."}
data: {"type": "synthesis", "answer": "...", "data_supports": [...],
       "data_does_not_support": [...], "would_need": [...],
       "confidence": "high|medium|low", "confidence_rationale": "..."}       # EXT-7, only when attached
data: {"type": "hypotheses", "disclaimer": "...", "confidence": "low",
       "hypotheses": [...]}                                                  # EXT-11, only when gates pass
data: {"type": "done", "turn_id": "..."}
```

**Conditional events:**
- `mcp_context` — only when `RoomConfig.mcp_servers` is configured AND
  `MCPResolver.resolve()` returned at least one entry (EXT-5).
- `plan` — only when `PlanningAgent` produces a multi-step plan (≥ 2 steps).
  Single-step plans skip it; the stream is functionally identical to the
  pre-EXT-1 sequence.
- `steps` — same condition as `plan`. Emits a per-step `step_id / description /
  sql / columns / row_count` summary after the primary result.
- `synthesis` — when a `SynthesizedAnswer` is attached to the turn (always
  for multi-step plans; medium/low confidence only for single-step). EXT-7.
- `hypotheses` — when all three EXT-11 gates pass: `hypothesis_mode_enabled=True`,
  multi-step plan, AND causal-question phrasing.

The primary `sql` + `result` events carry **step_1** for multi-step plans —
the rest of the steps appear in the `steps` summary.

### `GET /rooms/{room_id}/conversations/{conv_id}/messages`

Retrieve all turns in a conversation.

**Response 200:**
```json
{"turns": [ConversationTurn, ...]}
```

---

## Management API

### `POST /rooms`

Create a new room from a config.

**Request:** `RoomConfig` as JSON (partial — `room_id` is generated server-side if omitted)

**Response 201:**
```json
{"room_id": "<id>", "config": RoomConfig}
```

**Response 422:** validation failure (missing `tables`, invalid `warehouse_id`, etc.)

### `GET /rooms/{room_id}`

Retrieve the current room config.

**Response 200:** `RoomConfig` as JSON
**Response 404:** room not found

### `PATCH /rooms/{room_id}`

Partial update of room config. Only supplied keys are replaced.

**Request:** partial `RoomConfig` — any subset of fields
```json
{
  "text_instruction": "Updated instructions...",
  "joins": [...]
}
```

**Response 200:** updated `RoomConfig`

**Notes:**
- Omitted keys are preserved from the current config
- If `examples` are changed, re-indexing is triggered automatically (see [[room_engine]])
- This is the programmatic equivalent of editing a room in the UI

### `DELETE /rooms/{room_id}`

Delete a room and all its data.

**Response 204:** success
**Response 404:** room not found

### `POST /rooms/{room_id}/index`

Trigger re-indexing of example SQLs into the vector store. Use after bulk example updates.

**Response 202:**
```json
{"status": "indexing", "room_id": "<id>"}
```

Indexing runs asynchronously. The endpoint returns immediately.

### `POST /rooms/{room_id}/benchmarks/run`

Run all benchmarks for a room and return the report.

**Response 200:** `BenchmarkReport` (see [[feedback]])

---

## MCP API (EXT-4)

Tiri exposes itself as a Model Context Protocol server so any MCP-compatible
client (Claude, Cursor, VS Code, other agents) can call Tiri rooms as tools.

**Mount point:** `POST /mcp` — a single JSON-RPC 2.0 endpoint (Streamable HTTP
transport). The endpoint coexists with the REST routes — no shared state,
no route conflicts.

**Authentication:** same `Authorization: Bearer` / `X-Forwarded-Access-Token`
precedence as REST. **Auth failures return HTTP 200 + a JSON-RPC error
(code `-32001`), NOT HTTP 401** — MCP clients expect protocol-level errors,
not transport-level ones.

**JSON-RPC methods:**
- `initialize` — protocol handshake. Returns server name/version and
  protocol version.
- `tools/list` — returns the three tool definitions below.
- `tools/call` — invokes a tool. Tool name in `params.name`, arguments in
  `params.arguments`.

**Tools:**

| Name | Arguments | Purpose |
|---|---|---|
| `tiri_query` | `room_id`, `question`, optional `conversation_id` | Ask a natural-language question. Returns the synthesized answer + SQL + row count + conversation_id (round-tripped for follow-ups). |
| `tiri_list_rooms` | none | List all configured rooms with title and table count. |
| `tiri_room_schema` | `room_id` | Tables and domain instruction for one room. |

**Tool result shape (MCP convention, not the REST ConversationTurn shape):**
```json
{
  "content": [{"type": "text", "text": "<human-readable prose>"}],
  "structuredContent": { ... compact programmatic payload ... },
  "isError": false
}
```

`isError=true` is the MCP convention for "the tool ran but returned an
error" (e.g. unknown room). True protocol failures (parse error, unknown
method, auth required) come back as JSON-RPC error objects instead.

---

## Request/response conventions

- All request and response bodies are JSON
- All timestamps are ISO 8601 UTC
- Errors follow the shape: `{"error": "<type>", "message": "<detail>"}`
- HTTP status codes follow REST conventions: 200 OK, 201 Created, 202 Accepted, 204 No Content, 404 Not Found, 422 Unprocessable Entity, 500 Internal Server Error

---

## FastAPI app structure

```python
app = FastAPI(title="Tiri API")

app.include_router(conversations.router, prefix="/rooms", tags=["conversations"])
app.include_router(management.router,   prefix="/rooms", tags=["management"])
app.include_router(feedback.router,     prefix="/rooms", tags=["feedback"])

@app.on_event("startup")
async def startup():
    container = build_container(Config.load())   # reads tiri.toml or env vars
    app.state.container = container
```

Routes access the container via `request.app.state.container`. `RoomEngine` and `RoomManager` are instantiated per request (lightweight — they hold no state).

**User token pass-through (EXT-6):** conversation routes extract the Bearer token from `request.headers["Authorization"]` and pass it as `user_token` to `RoomEngine.chat()`. Before EXT-6 is implemented, pass `None`.

---

## Test cases

| # | Scenario | MUST |
|---|---|---|
| 1 | `POST /rooms` with valid config | MUST return 201 with `room_id` |
| 2 | `POST /rooms` with missing `tables` | MUST return 422 |
| 3 | `POST /rooms/{id}/conversations/{cid}/messages` with valid question | MUST return 200 with `ConversationTurn` |
| 4 | `GET /rooms/{id}/conversations/{cid}/messages/stream` | MUST return `text/event-stream` content type |
| 5 | `GET /rooms/{id}/conversations/{cid}/messages/stream` | MUST yield a `done` event as the final event |
| 6 | `PATCH /rooms/{id}` with only `text_instruction` | MUST preserve all other config fields |
| 7 | `DELETE /rooms/{id}` then `GET /rooms/{id}` | MUST return 404 |
| 8 | Any endpoint without `Authorization` header | MUST return 401 (when auth enabled, unless `X-Forwarded-Access-Token` is present) |
| 8b | Request with only `X-Forwarded-Access-Token` (no Authorization) | MUST authenticate using the forwarded token |
| 8c | Request with both headers | MUST prefer `Authorization: Bearer` over forwarded |
| 9 | `POST /rooms/{id}/conversations` for nonexistent room | MUST return 404 |
| 10 | `POST /rooms/{id}/benchmarks/run` | MUST return a report with one result per benchmark |
| 11 | `POST /mcp` with `Authorization: Bearer <token>` | MUST authenticate and dispatch the JSON-RPC method |
| 12 | `POST /mcp` with neither Bearer nor X-Forwarded token (auth enabled) | MUST return HTTP 200 + JSON-RPC error code `-32001`, NOT HTTP 401 |
| 13 | `POST /mcp` with `tools/list` | MUST return all three tools: `tiri_query`, `tiri_list_rooms`, `tiri_room_schema` |
| 14 | `POST /mcp` with `tools/call` for unknown tool name | MUST return HTTP 200 + JSON-RPC error code `-32601` |
| 15 | `POST /mcp` with `tiri_query` for nonexistent room | MUST return HTTP 200 + result with `isError=true` (tool-level error, not protocol error) |
| 16 | `POST /mcp` with `tiri_query` and a `conversation_id` from a prior call | MUST persist the turn under the same conversation index (multi-turn context) |
| 17 | `/rooms` REST routes + `/mcp` in same client session | MUST coexist with no shared state and no route shadowing |
