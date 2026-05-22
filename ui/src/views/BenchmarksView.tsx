import * as React from "react";
import { Download, Play } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { SqlBlock } from "@/components/SqlBlock";
import { BackendSelector } from "@/components/BackendSelector";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { useSSEStream } from "@/hooks/use-sse-stream";
import {
  createConversation,
  getRoom,
  listRoomKeys,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import type { Backend, ConfigRoutingResponse, ProviderType, RoomConfig } from "@/lib/types";

const PROVIDER_TYPE_FALLBACK = "custom" as ProviderType;

/**
 * BenchmarksView — run every benchmark for a room across multiple
 * backends in parallel, score with the same rules as
 * `BenchmarkRunner._compare_row_counts` (row-count match) since the UI
 * doesn't have access to the byte-equal SQL normalization the server
 * uses internally — row count is a strong-enough proxy for the visual
 * comparison.
 *
 * Row state lifecycle:
 *   pending → running (Skeleton in cell) → pass/fail/error (badge)
 */

type CellState =
  | { kind: "pending" }
  | { kind: "running" }
  | { kind: "pass"; rowCount: number }
  | { kind: "fail"; rowCount: number; generatedSql: string; expectedSql: string; synthesis?: string }
  | { kind: "error"; message: string };

interface BenchmarksViewProps {
  routing: ConfigRoutingResponse | null;
}

export function BenchmarksView({ routing }: BenchmarksViewProps) {
  const [roomIds, setRoomIds] = React.useState<string[]>([]);
  const [selectedRoom, setSelectedRoom] = React.useState<string>("");
  const [room, setRoom] = React.useState<RoomConfig | null>(null);
  const [backends, setBackends] = React.useState<Backend[]>([]);
  const [selectedBackends, setSelectedBackends] = React.useState<string[]>([]);
  // cellState[questionIndex][backendId]
  const [cells, setCells] = React.useState<Record<number, Record<string, CellState>>>({});
  const [running, setRunning] = React.useState(false);
  const [expanded, setExpanded] = React.useState<Record<string, boolean>>({});
  const { toast } = useToast();

  // Load room IDs and build backend list (same logic as AskView).
  React.useEffect(() => {
    void listRoomKeys().then((ids) => {
      setRoomIds(ids);
      if (ids.length > 0) setSelectedRoom(ids[0]);
    });
  }, []);

  React.useEffect(() => {
    if (!routing) return;
    const seen = new Set<string>();
    const list: Backend[] = [];
    const providerTypes = Object.fromEntries(
      routing.providers.map((p) => [p.name, p.type]),
    ) as Record<string, ProviderType>;
    Object.values(routing.routing).forEach((id) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      const [provider, ...rest] = id.split("::");
      const model = rest.join("::");
      list.push({
        provider, model, id, label: id,
        type: providerTypes[provider] ?? PROVIDER_TYPE_FALLBACK,
      });
    });
    setBackends(list);
    if (selectedBackends.length === 0 && list.length > 0) {
      const sqlBackend = list.find((b) => b.id === routing.routing.sql);
      setSelectedBackends([sqlBackend?.id ?? list[0].id]);
    }
  }, [routing, selectedBackends.length]);

  // Load room config when selectedRoom changes.
  React.useEffect(() => {
    if (!selectedRoom) return;
    void getRoom(selectedRoom).then(setRoom).catch(() => setRoom(null));
  }, [selectedRoom]);

  const benchmarks = room?.benchmarks ?? [];

  const handleRunAll = async () => {
    if (!selectedRoom || benchmarks.length === 0 || selectedBackends.length === 0) {
      return;
    }
    setRunning(true);
    // Initialize cells matrix to "running"
    const init: Record<number, Record<string, CellState>> = {};
    benchmarks.forEach((_, i) => {
      init[i] = {};
      selectedBackends.forEach((b) => {
        init[i][b] = { kind: "running" };
      });
    });
    setCells(init);

    // Fire all questions × backends in parallel. Each one creates its
    // own conversation so they don't trample each other.
    const tasks = benchmarks.flatMap((bench, qIdx) =>
      selectedBackends.map(async (backendId) => {
        try {
          const conv = await createConversation(selectedRoom);
          const result = await runOne(
            selectedRoom,
            conv,
            bench.question,
            backendId,
            bench.expected_sql ?? "",
            bench.expected_row_count ?? null,
          );
          setCells((prev) => ({
            ...prev,
            [qIdx]: { ...prev[qIdx], [backendId]: result },
          }));
        } catch (err) {
          setCells((prev) => ({
            ...prev,
            [qIdx]: {
              ...prev[qIdx],
              [backendId]: {
                kind: "error",
                message: (err as Error)?.message ?? "failed",
              },
            },
          }));
        }
      }),
    );
    await Promise.all(tasks);
    setRunning(false);
    toast({
      title: "Benchmark run complete",
      description: `${benchmarks.length} questions × ${selectedBackends.length} backends`,
      variant: "success",
    });
  };

  const handleExport = () => {
    if (benchmarks.length === 0) return;
    const lines: string[] = [];
    const header = ["#", "question", ...selectedBackends];
    lines.push(header.join(","));
    benchmarks.forEach((bench, i) => {
      const row = [String(i + 1), JSON.stringify(bench.question)];
      selectedBackends.forEach((b) => {
        const state = cells[i]?.[b];
        if (!state) row.push("");
        else if (state.kind === "pass") row.push(`pass:${state.rowCount}`);
        else if (state.kind === "fail") row.push(`fail:${state.rowCount}`);
        else if (state.kind === "error") row.push("error");
        else row.push(state.kind);
      });
      lines.push(row.join(","));
    });
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${selectedRoom}-benchmark.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Compute score per backend.
  const scores: Record<string, { pass: number; total: number }> = {};
  selectedBackends.forEach((b) => {
    let pass = 0;
    benchmarks.forEach((_, i) => {
      const state = cells[i]?.[b];
      if (state?.kind === "pass") pass++;
    });
    scores[b] = { pass, total: benchmarks.length };
  });

  return (
    <div className="space-y-4">
      <Card className="sticky top-[4rem] z-20 bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/75">
        <CardContent className="space-y-4 py-4">
          <div className="grid gap-4 md:grid-cols-[260px_1fr]">
            <div className="space-y-1.5">
              <Label htmlFor="bench-room" className="text-xs uppercase tracking-wider text-muted-foreground">
                Room
              </Label>
              <Select value={selectedRoom} onValueChange={setSelectedRoom}>
                <SelectTrigger id="bench-room" className="font-mono text-sm">
                  <SelectValue placeholder="Choose a room…" />
                </SelectTrigger>
                <SelectContent>
                  {roomIds.map((id) => (
                    <SelectItem key={id} value={id} className="font-mono">
                      {id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                Backends ({selectedBackends.length} selected)
              </Label>
              <BackendSelector
                backends={backends}
                selected={selectedBackends}
                onChange={setSelectedBackends}
                onAddCustom={(b) =>
                  setBackends((prev) => (prev.find((x) => x.id === b.id) ? prev : [...prev, b]))
                }
              />
            </div>
          </div>
          <div className="flex items-center justify-between">
            <div className="text-xs text-muted-foreground">
              {benchmarks.length > 0 && (
                <>
                  <strong className="text-foreground">{benchmarks.length}</strong>{" "}
                  question{benchmarks.length === 1 ? "" : "s"} configured for this room
                </>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={handleExport} disabled={Object.keys(cells).length === 0}>
                <Download className="h-3.5 w-3.5" />
                Export CSV
              </Button>
              <Button
                onClick={handleRunAll}
                disabled={
                  running || benchmarks.length === 0 || selectedBackends.length === 0
                }
              >
                <Play className="h-3.5 w-3.5" />
                Run all
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="py-3">
          <CardTitle className="text-sm">Benchmark results</CardTitle>
        </CardHeader>
        <CardContent className="py-0 pb-4">
          {benchmarks.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              {selectedRoom
                ? "This room has no benchmarks configured."
                : "Choose a room above."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">#</TableHead>
                  <TableHead className="w-[420px]">Question</TableHead>
                  {selectedBackends.map((b) => {
                    const backend = backends.find((x) => x.id === b);
                    return (
                      <TableHead key={b} className="min-w-[180px]">
                        <div className="flex items-center gap-1.5">
                          <Badge variant={(backend?.type ?? "custom") as "databricks" | "anthropic" | "openai" | "ollama" | "custom"}>
                            {backend?.provider || b.split("::")[0]}
                          </Badge>
                          <span className="font-mono text-[11px]">
                            {backend?.model || b.split("::").slice(1).join("::")}
                          </span>
                        </div>
                      </TableHead>
                    );
                  })}
                </TableRow>
              </TableHeader>
              <TableBody>
                {benchmarks.map((bench, i) => {
                  const benchKey = bench.id ?? `idx-${i}`;
                  return (
                    <React.Fragment key={benchKey}>
                      <TableRow>
                        <TableCell className="text-muted-foreground tabular-nums">
                          {i + 1}
                        </TableCell>
                        <TableCell className="text-sm">
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="line-clamp-2 cursor-default">
                                {bench.question}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-md whitespace-normal">
                              {bench.question}
                            </TooltipContent>
                          </Tooltip>
                          {bench.expected_row_count != null && (
                            <p className="mt-0.5 text-[10px] text-muted-foreground">
                              expects {bench.expected_row_count} rows
                            </p>
                          )}
                        </TableCell>
                        {selectedBackends.map((b) => {
                          const state = cells[i]?.[b];
                          const expandKey = `${i}::${b}`;
                          const isExpanded = !!expanded[expandKey];
                          return (
                            <TableCell key={b} className="align-top">
                              <CellContent
                                state={state}
                                onExpand={
                                  state?.kind === "fail"
                                    ? () =>
                                        setExpanded((prev) => ({
                                          ...prev,
                                          [expandKey]: !prev[expandKey],
                                        }))
                                    : undefined
                                }
                                isExpanded={isExpanded}
                              />
                            </TableCell>
                          );
                        })}
                      </TableRow>
                      {selectedBackends.map((b) => {
                        const expandKey = `${i}::${b}`;
                        if (!expanded[expandKey]) return null;
                        const state = cells[i]?.[b];
                        if (state?.kind !== "fail") return null;
                        return (
                          <TableRow key={`${expandKey}-detail`}>
                            <TableCell colSpan={2 + selectedBackends.length}>
                              <FailureDetail
                                generatedSql={state.generatedSql}
                                expectedSql={state.expectedSql}
                                generatedRowCount={state.rowCount}
                                expectedRowCount={bench.expected_row_count ?? null}
                                synthesis={state.synthesis}
                              />
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </React.Fragment>
                  );
                })}
              </TableBody>
              <TableFooter>
                <TableRow>
                  <TableCell colSpan={2} className="text-sm font-medium">
                    Score
                  </TableCell>
                  {selectedBackends.map((b) => {
                    const { pass, total } = scores[b];
                    const pct = total > 0 ? Math.round((pass / total) * 100) : 0;
                    const color =
                      pct >= 80 ? "text-success" : pct >= 60 ? "text-warning" : "text-destructive";
                    return (
                      <TableCell key={b} className={cn("font-mono text-sm font-medium", color)}>
                        {pass}/{total} ({pct}%)
                      </TableCell>
                    );
                  })}
                </TableRow>
              </TableFooter>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function CellContent({
  state,
  onExpand,
  isExpanded,
}: {
  state: CellState | undefined;
  onExpand?: () => void;
  isExpanded: boolean;
}) {
  if (!state || state.kind === "pending") {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  if (state.kind === "running") {
    return <Skeleton className="h-5 w-20" />;
  }
  if (state.kind === "pass") {
    return (
      <Badge variant="success" className="text-[10px]">
        ✓ {state.rowCount} row{state.rowCount === 1 ? "" : "s"}
      </Badge>
    );
  }
  if (state.kind === "error") {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant="warning" className="text-[10px] cursor-help">
            ⚠ error
          </Badge>
        </TooltipTrigger>
        <TooltipContent className="max-w-md whitespace-normal">
          {state.message}
        </TooltipContent>
      </Tooltip>
    );
  }
  return (
    <button
      type="button"
      onClick={onExpand}
      className="inline-flex items-center gap-1 cursor-pointer"
    >
      <Badge variant="destructive" className="text-[10px]">
        ✗ {state.rowCount} row{state.rowCount === 1 ? "" : "s"}
      </Badge>
      <span className="text-[10px] text-muted-foreground underline-offset-2 hover:underline">
        {isExpanded ? "hide" : "diff"}
      </span>
    </button>
  );
}

function FailureDetail({
  generatedSql,
  expectedSql,
  generatedRowCount,
  expectedRowCount,
  synthesis,
}: {
  generatedSql: string;
  expectedSql: string;
  generatedRowCount: number;
  expectedRowCount: number | null;
  synthesis?: string;
}) {
  return (
    <div className="space-y-3 rounded-md bg-muted/30 p-3">
      <div className="grid gap-3 lg:grid-cols-2">
        <div>
          <p className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
            Generated
          </p>
          <SqlBlock sql={generatedSql} label="generated" />
        </div>
        <div>
          <p className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
            Expected
          </p>
          <SqlBlock sql={expectedSql} label="expected" />
        </div>
      </div>
      <div className="flex items-center gap-3 text-xs">
        <Badge variant="destructive">Generated: {generatedRowCount} rows</Badge>
        {expectedRowCount != null && (
          <Badge variant="muted">Expected: {expectedRowCount} rows</Badge>
        )}
      </div>
      {synthesis && (
        <p className="rounded-md border-l-2 border-warning bg-warning/5 p-2 text-xs italic">
          {synthesis}
        </p>
      )}
    </div>
  );
}

/**
 * runOne — kick off a single benchmark via SSE and resolve to a CellState.
 *
 * We can't use useSSEStream here because it's a hook; this is a one-shot
 * imperative call from inside a Promise.all. We re-use the SSE parsing
 * logic via a small inline fetch+ReadableStream loop.
 */
async function runOne(
  roomId: string,
  conversationId: string,
  question: string,
  backendId: string,
  expectedSql: string,
  expectedRowCount: number | null,
): Promise<CellState> {
  const params = new URLSearchParams({
    question,
    model_override: backendId,
  });
  const response = await fetch(
    `/rooms/${encodeURIComponent(roomId)}/conversations/${encodeURIComponent(
      conversationId,
    )}/messages/stream?${params.toString()}`,
    { headers: { Accept: "text/event-stream" } },
  );
  if (!response.ok || !response.body) {
    return { kind: "error", message: `HTTP ${response.status}` };
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sql = "";
  let rowCount = 0;
  let synthesis: string | undefined;
  let error: string | undefined;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let separator = buffer.indexOf("\n\n");
    while (separator !== -1) {
      const frame = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      const payload = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (!payload) {
        separator = buffer.indexOf("\n\n");
        continue;
      }
      try {
        const ev = JSON.parse(payload) as { type: string; [k: string]: unknown };
        if (ev.type === "sql" && typeof ev.sql === "string") sql = ev.sql;
        if (ev.type === "result" && Array.isArray(ev.rows)) {
          rowCount = (ev.rows as unknown[]).length;
        }
        if (ev.type === "synthesis" && typeof ev.answer === "string")
          synthesis = ev.answer;
        if (ev.type === "error" && typeof ev.message === "string")
          error = ev.message;
      } catch {
        /* ignore */
      }
      separator = buffer.indexOf("\n\n");
    }
  }

  if (error) return { kind: "error", message: error };
  if (!sql) return { kind: "error", message: "no SQL produced" };
  const matches = expectedRowCount != null && rowCount === expectedRowCount;
  if (matches) return { kind: "pass", rowCount };
  return {
    kind: "fail",
    rowCount,
    generatedSql: sql,
    expectedSql,
    synthesis,
  };
}
