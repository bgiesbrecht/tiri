import type {
  ConfigRoutingResponse,
  ConversationTurn,
  RoomConfig,
} from "./types";

/**
 * Thin wrapper around fetch for the Tiri REST API. Adds:
 *  - JSON body serialization
 *  - Throws on non-2xx with the API's error message
 *  - Same-origin in production (FastAPI serves the UI at /app), proxied
 *    in dev (vite.config.ts proxies /rooms /config /conversations to :8000).
 */

const HEADERS_JSON = { "Content-Type": "application/json" };

class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) {
    super(message);
    this.name = "ApiError";
  }
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text();
    }
    const message =
      (typeof detail === "object" && detail && "message" in detail
        ? String((detail as { message: unknown }).message)
        : null) || `HTTP ${response.status}`;
    throw new ApiError(response.status, message, detail);
  }
  return response.json() as Promise<T>;
}

// ── Config ────────────────────────────────────────────────────────────────

export async function getRouting(): Promise<ConfigRoutingResponse> {
  return json(await fetch("/config/routing"));
}

export interface CredentialEntry {
  provider: string;
  key: string;
  value: string;
}

export async function postCredentials(
  credentials: CredentialEntry[],
): Promise<{ accepted: string[]; warnings: string[]; rejected: string[] }> {
  return json(
    await fetch("/config/credentials", {
      method: "POST",
      headers: HEADERS_JSON,
      body: JSON.stringify({ credentials }),
    }),
  );
}

export async function clearCredentials(): Promise<{ cleared: boolean }> {
  return json(await fetch("/config/credentials", { method: "DELETE" }));
}

// ── Rooms ────────────────────────────────────────────────────────────────

export async function listRoomKeys(): Promise<string[]> {
  // The API doesn't have a list endpoint; we use the MCP tool's list path
  // OR fall back to iterating known IDs. For UI bootstrap we hit the MCP
  // /mcp endpoint with tools/call tiri_list_rooms which IS available.
  const response = await fetch("/mcp", {
    method: "POST",
    headers: HEADERS_JSON,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "tiri_list_rooms", arguments: {} },
    }),
  });
  const data = await response.json();
  if (data.error) throw new ApiError(500, data.error.message ?? "MCP error");
  const rooms = data.result?.structuredContent?.rooms;
  if (!Array.isArray(rooms)) return [];
  return rooms.map((r: { room_id: string }) => r.room_id);
}

export async function getRoom(roomId: string): Promise<RoomConfig> {
  return json(await fetch(`/rooms/${encodeURIComponent(roomId)}`));
}

export async function createRoom(config: RoomConfig): Promise<{ room_id: string }> {
  return json(
    await fetch("/rooms", {
      method: "POST",
      headers: HEADERS_JSON,
      body: JSON.stringify(config),
    }),
  );
}

export async function getRoomTables(
  roomId: string,
): Promise<import("./types").RoomTablesResponse> {
  return json(await fetch(`/rooms/${encodeURIComponent(roomId)}/tables`));
}

// ── Conversations ────────────────────────────────────────────────────────

export async function createConversation(roomId: string): Promise<string> {
  const body = await json<{ conversation_id: string }>(
    await fetch(
      `/rooms/${encodeURIComponent(roomId)}/conversations`,
      { method: "POST" },
    ),
  );
  return body.conversation_id;
}

export async function listMessages(
  roomId: string,
  conversationId: string,
): Promise<ConversationTurn[]> {
  const body = await json<{ turns: ConversationTurn[] }>(
    await fetch(
      `/rooms/${encodeURIComponent(roomId)}/conversations/${encodeURIComponent(
        conversationId,
      )}/messages`,
    ),
  );
  return body.turns;
}

export async function sendMessage(
  roomId: string,
  conversationId: string,
  question: string,
  modelOverride?: string,
): Promise<ConversationTurn> {
  return json(
    await fetch(
      `/rooms/${encodeURIComponent(roomId)}/conversations/${encodeURIComponent(
        conversationId,
      )}/messages`,
      {
        method: "POST",
        headers: HEADERS_JSON,
        body: JSON.stringify({
          question,
          ...(modelOverride ? { model_override: modelOverride } : {}),
        }),
      },
    ),
  );
}

/** URL for the SSE stream endpoint. The hook uses fetch + ReadableStream
 * rather than EventSource so the model_override and question can be passed
 * as query params on a GET; the response is text/event-stream. */
export function streamUrl(
  roomId: string,
  conversationId: string,
  question: string,
  modelOverride?: string,
): string {
  const params = new URLSearchParams({ question });
  if (modelOverride) params.set("model_override", modelOverride);
  return `/rooms/${encodeURIComponent(roomId)}/conversations/${encodeURIComponent(
    conversationId,
  )}/messages/stream?${params.toString()}`;
}

export { ApiError };
