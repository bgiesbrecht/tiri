import { cn } from "@/lib/utils";

const FRIENDLY: Record<string, string> = {
  status: "Working…",
  mcp_context: "External context received",
  intent: "Classifying question…",
  plan: "Planning queries…",
  sql: "Generating SQL…",
  result: "Running query…",
  steps: "Multi-step complete",
  synthesis: "Synthesizing answer…",
  hypotheses: "Generating hypotheses…",
  viz: "Building chart…",
  clarify: "Asking for clarification…",
  done: "Done",
  error: "Error",
  "": "Idle",
};

/**
 * StreamStatus — animated dot + friendly stage label.
 *
 * The dot pulses while the stream is mid-flight (state === "running")
 * and goes solid for "done"; red for "error". The label maps from the
 * raw event type to a friendly verb so the user sees "Generating SQL…"
 * instead of "sql".
 */
export function StreamStatus({
  stage,
  state,
  error,
}: {
  stage: string;
  state: "idle" | "running" | "done" | "error";
  error?: string;
}) {
  const isError = state === "error";
  const isDone = state === "done";
  const dotClass = isError
    ? "bg-destructive"
    : isDone
      ? "bg-success"
      : state === "running"
        ? "bg-primary animate-pulse-soft"
        : "bg-muted-foreground";

  const label = isError
    ? FRIENDLY.error
    : isDone
      ? FRIENDLY.done
      : FRIENDLY[stage] || FRIENDLY[""];

  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <span className={cn("inline-block h-2 w-2 rounded-full", dotClass)} />
      <span className="leading-none">{label}</span>
      {isError && error && (
        <span className="text-destructive font-mono text-[10px] truncate max-w-[280px]">
          {error}
        </span>
      )}
    </div>
  );
}
