import * as React from "react";
import { streamUrl } from "@/lib/api";
import type { SSEEvent } from "@/lib/types";

/**
 * Stream a chat invocation against /rooms/.../messages/stream.
 *
 * Why fetch + ReadableStream rather than EventSource:
 *   The native EventSource doesn't let us send a model_override header or
 *   abort cleanly with backpressure. fetch() with a streaming response
 *   body works in all evergreen browsers, supports AbortController, and
 *   handles the same `data: <json>\n\n` SSE framing.
 *
 * Events are stored keyed by type so the consumer can render each section
 * progressively:
 *
 *   events.plan       — single plan event (last one wins)
 *   events.sql        — primary step SQL
 *   events.result     — primary step result table
 *   events.steps      — multi-step summary
 *   events.viz        — chart spec
 *   events.synthesis  — synthesized answer
 *   events.hypotheses — hypothesis result
 *   events.error      — error message
 *   events.statusLog  — array of status events (for the StreamStatus pill)
 *
 * The hook does NOT auto-start. The caller invokes `start(question)` to
 * kick off, and `cancel()` to abort. `state` is "idle" | "running" |
 * "done" | "error" — the StreamStatus component uses this directly.
 */

export interface StreamEvents {
  plan?: Extract<SSEEvent, { type: "plan" }>;
  sql?: Extract<SSEEvent, { type: "sql" }>;
  result?: Extract<SSEEvent, { type: "result" }>;
  steps?: Extract<SSEEvent, { type: "steps" }>;
  viz?: Extract<SSEEvent, { type: "viz" }>;
  synthesis?: Extract<SSEEvent, { type: "synthesis" }>;
  hypotheses?: Extract<SSEEvent, { type: "hypotheses" }>;
  clarify?: Extract<SSEEvent, { type: "clarify" }>;
  mcpContext?: Extract<SSEEvent, { type: "mcp_context" }>;
  done?: Extract<SSEEvent, { type: "done" }>;
  error?: Extract<SSEEvent, { type: "error" }>;
  statusLog: Array<{ text: string; t: number }>;
}

export type StreamState = "idle" | "running" | "done" | "error";

export interface UseSSEStreamResult {
  state: StreamState;
  events: StreamEvents;
  stage: string; // most recent event type — used by StreamStatus
  startedAt: number | null;
  finishedAt: number | null;
  start: (args: { roomId: string; conversationId: string; question: string; modelOverride?: string }) => Promise<void>;
  cancel: () => void;
  reset: () => void;
}

const EMPTY_EVENTS: StreamEvents = { statusLog: [] };

export function useSSEStream(): UseSSEStreamResult {
  const [state, setState] = React.useState<StreamState>("idle");
  const [events, setEvents] = React.useState<StreamEvents>(EMPTY_EVENTS);
  const [stage, setStage] = React.useState<string>("");
  const [startedAt, setStartedAt] = React.useState<number | null>(null);
  const [finishedAt, setFinishedAt] = React.useState<number | null>(null);
  const controllerRef = React.useRef<AbortController | null>(null);

  const reset = React.useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setState("idle");
    setEvents(EMPTY_EVENTS);
    setStage("");
    setStartedAt(null);
    setFinishedAt(null);
  }, []);

  const cancel = React.useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setState((s) => (s === "running" ? "idle" : s));
  }, []);

  const start = React.useCallback<UseSSEStreamResult["start"]>(
    async ({ roomId, conversationId, question, modelOverride }) => {
      // Reset any prior run; only one stream per hook instance at a time.
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;

      setEvents(EMPTY_EVENTS);
      setStage("");
      setStartedAt(Date.now());
      setFinishedAt(null);
      setState("running");

      try {
        const response = await fetch(
          streamUrl(roomId, conversationId, question, modelOverride),
          { signal: controller.signal, headers: { Accept: "text/event-stream" } },
        );
        if (!response.ok || !response.body) {
          const text = await response.text().catch(() => "");
          throw new Error(
            `HTTP ${response.status} ${response.statusText}${text ? `: ${text.slice(0, 200)}` : ""}`,
          );
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          // SSE frames are separated by blank lines (\n\n). Process any
          // complete frames in the buffer; keep the tail for the next read.
          let separator = buffer.indexOf("\n\n");
          while (separator !== -1) {
            const frame = buffer.slice(0, separator);
            buffer = buffer.slice(separator + 2);
            const dataLines = frame
              .split("\n")
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).trim());
            if (dataLines.length > 0) {
              const payload = dataLines.join("\n");
              try {
                const ev = JSON.parse(payload) as SSEEvent;
                applyEvent(ev, setEvents, setStage);
                if (ev.type === "done") {
                  setState("done");
                  setFinishedAt(Date.now());
                } else if (ev.type === "error") {
                  setState("error");
                  setFinishedAt(Date.now());
                }
              } catch {
                // Ignore malformed payloads — server only emits valid JSON
                // but defensive parsing keeps the stream alive on noise.
              }
            }
            separator = buffer.indexOf("\n\n");
          }
        }
        // Stream closed without a `done`/`error` event — treat as done.
        setState((s) => (s === "running" ? "done" : s));
        setFinishedAt((t) => t ?? Date.now());
      } catch (err) {
        if ((err as Error)?.name === "AbortError") {
          // User cancelled — already handled by `cancel()`.
          return;
        }
        const message = (err as Error)?.message || "Stream failed";
        setEvents((prev) => ({ ...prev, error: { type: "error", message } }));
        setState("error");
        setStage("error");
        setFinishedAt(Date.now());
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null;
        }
      }
    },
    [],
  );

  // Clean up on unmount.
  React.useEffect(() => {
    return () => {
      controllerRef.current?.abort();
    };
  }, []);

  return { state, events, stage, startedAt, finishedAt, start, cancel, reset };
}

function applyEvent(
  ev: SSEEvent,
  setEvents: React.Dispatch<React.SetStateAction<StreamEvents>>,
  setStage: React.Dispatch<React.SetStateAction<string>>,
) {
  setStage(ev.type);
  switch (ev.type) {
    case "status":
      setEvents((prev) => ({
        ...prev,
        statusLog: [...prev.statusLog, { text: ev.text, t: Date.now() }],
      }));
      break;
    case "plan":
      setEvents((prev) => ({ ...prev, plan: ev }));
      break;
    case "sql":
      setEvents((prev) => ({ ...prev, sql: ev }));
      break;
    case "result":
      setEvents((prev) => ({ ...prev, result: ev }));
      break;
    case "steps":
      setEvents((prev) => ({ ...prev, steps: ev }));
      break;
    case "viz":
      setEvents((prev) => ({ ...prev, viz: ev }));
      break;
    case "synthesis":
      setEvents((prev) => ({ ...prev, synthesis: ev }));
      break;
    case "hypotheses":
      setEvents((prev) => ({ ...prev, hypotheses: ev }));
      break;
    case "clarify":
      setEvents((prev) => ({ ...prev, clarify: ev }));
      break;
    case "mcp_context":
      setEvents((prev) => ({ ...prev, mcpContext: ev }));
      break;
    case "error":
      setEvents((prev) => ({ ...prev, error: ev }));
      break;
    case "done":
      setEvents((prev) => ({ ...prev, done: ev }));
      break;
  }
}
