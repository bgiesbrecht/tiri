import * as React from "react";
import { Play, Square, MessageSquare } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { BackendSelector } from "@/components/BackendSelector";
import { ResultColumn } from "@/views/ResultColumn";
import { listRoomKeys, createConversation } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { Backend, ConfigRoutingResponse, ProviderType } from "@/lib/types";

const PROVIDER_TYPE_FALLBACK = "custom" as ProviderType;

/**
 * AskView — the main query surface.
 *
 * Layout:
 *   A. Sticky query controls (room, backends, question + Ask/Stop)
 *   B. Per-backend result columns (one per selected backend)
 *   C. Question history (compact, scrollable)
 *
 * Each result column owns its own SSE stream — they all start
 * simultaneously when Ask is clicked. The conversation_id is per
 * (room, backend) so each stream's history lives in its own conversation.
 *
 * Cmd/Ctrl+Enter submits.
 */

interface AskViewProps {
  routing: ConfigRoutingResponse | null;
}

export function AskView({ routing }: AskViewProps) {
  const [roomIds, setRoomIds] = React.useState<string[]>([]);
  const [selectedRoom, setSelectedRoom] = React.useState<string>("");
  const [backends, setBackends] = React.useState<Backend[]>([]);
  const [selectedBackends, setSelectedBackends] = React.useState<string[]>([]);
  const [question, setQuestion] = React.useState<string>("");
  const [running, setRunning] = React.useState(false);
  const [activeQuestion, setActiveQuestion] = React.useState<string>("");
  const [history, setHistory] = React.useState<string[]>([]);
  const [conversationIds, setConversationIds] = React.useState<
    Record<string, string>
  >({});
  const stopRefs = React.useRef<Record<string, () => void>>({});

  // Initial room + backend loading
  React.useEffect(() => {
    void listRoomKeys().then((ids) => {
      setRoomIds(ids);
      if (ids.length > 0) setSelectedRoom(ids[0]);
    });
  }, []);

  React.useEffect(() => {
    if (!routing) return;
    // Build backend list from /config/routing. Each (provider, model)
    // combo that appears in the routing table becomes a candidate
    // backend in the selector — even if the same model serves multiple
    // tasks. Deduplicate by `provider::model`.
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
        provider,
        model,
        id,
        label: id,
        type: providerTypes[provider] ?? PROVIDER_TYPE_FALLBACK,
      });
    });
    // Sort: completion routes first (smaller index is intent etc.),
    // embed last. We already have insertion order from RoutingConfig which
    // happens to put intent → planning → sql → synthesis → clarify →
    // viz_summary → embed. Keep that.
    setBackends(list);
    // Default selection: the SQL route's backend (the most important one).
    if (list.length > 0 && selectedBackends.length === 0) {
      const sqlBackend = list.find((b) => b.id === routing.routing.sql);
      setSelectedBackends([sqlBackend?.id ?? list[0].id]);
    }
  }, [routing, selectedBackends.length]);

  const ensureConversation = React.useCallback(
    async (roomId: string, backendId: string): Promise<string> => {
      const key = `${roomId}::${backendId}`;
      const existing = conversationIds[key];
      if (existing) return existing;
      const conv = await createConversation(roomId);
      setConversationIds((prev) => ({ ...prev, [key]: conv }));
      return conv;
    },
    [conversationIds],
  );

  const canAsk =
    selectedRoom && selectedBackends.length > 0 && question.trim().length > 0;

  const handleAsk = async () => {
    if (!canAsk) return;
    setRunning(true);
    setActiveQuestion(question.trim());
    setHistory((prev) =>
      prev.includes(question.trim()) ? prev : [question.trim(), ...prev].slice(0, 20),
    );
    // ResultColumn picks up `activeQuestion` and runs the SSE stream.
    // We don't need to do anything else here — the columns handle the
    // streaming and report back via `onComplete` / `onStop` so we can
    // re-enable the Ask button when all columns finish.
  };

  const handleStop = () => {
    Object.values(stopRefs.current).forEach((fn) => fn());
    setRunning(false);
  };

  const handleColumnComplete = React.useCallback(() => {
    // Mark as not running once all columns have finished or errored.
    // We use a ref counter rather than state because the columns call
    // this in their effect cleanup which races with state updates.
    pendingColumns.current = Math.max(pendingColumns.current - 1, 0);
    if (pendingColumns.current === 0) setRunning(false);
  }, []);
  const pendingColumns = React.useRef(0);

  React.useEffect(() => {
    // Reset the pending counter every time activeQuestion changes
    pendingColumns.current = activeQuestion ? selectedBackends.length : 0;
  }, [activeQuestion, selectedBackends.length]);

  const onTextareaKeyDown: React.KeyboardEventHandler<HTMLTextAreaElement> = (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void handleAsk();
    }
  };

  return (
    <div className="space-y-4">
      {/* Section A — Query controls */}
      <Card className="sticky top-[4rem] z-20 bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/75">
        <CardContent className="space-y-4 py-4">
          <div className="grid gap-4 md:grid-cols-[260px_1fr]">
            <div className="space-y-1.5">
              <Label htmlFor="room-select" className="text-xs uppercase tracking-wider text-muted-foreground">
                Room
              </Label>
              <Select value={selectedRoom} onValueChange={setSelectedRoom}>
                <SelectTrigger id="room-select" className="font-mono text-sm">
                  <SelectValue placeholder="Choose a room…" />
                </SelectTrigger>
                <SelectContent>
                  {roomIds.length === 0 && (
                    <SelectItem value="__none__" disabled>
                      No rooms configured
                    </SelectItem>
                  )}
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
                  setBackends((prev) =>
                    prev.find((x) => x.id === b.id) ? prev : [...prev, b],
                  )
                }
              />
            </div>
          </div>

          <Separator />

          <div className="space-y-1.5">
            <Label htmlFor="question" className="text-xs uppercase tracking-wider text-muted-foreground">
              Question
            </Label>
            <textarea
              id="question"
              className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring transition-[height] resize-none"
              placeholder="Ask a question about your data…"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={onTextareaKeyDown}
              rows={2}
              onFocus={(e) => (e.currentTarget.rows = 4)}
              onBlur={(e) => (e.currentTarget.rows = question.split("\n").length > 1 ? 4 : 2)}
            />
            <p className="text-xs text-muted-foreground">
              <kbd className="rounded border bg-muted px-1.5 py-0.5 text-[10px] font-mono">
                ⌘
              </kbd>{" "}
              +{" "}
              <kbd className="rounded border bg-muted px-1.5 py-0.5 text-[10px] font-mono">
                Enter
              </kbd>{" "}
              to submit
            </p>
          </div>

          <div className="flex items-center justify-between">
            <div className="text-xs text-muted-foreground">
              {selectedBackends.length > 0 && (
                <>
                  Will run against{" "}
                  <strong className="text-foreground">
                    {selectedBackends.length}
                  </strong>{" "}
                  backend{selectedBackends.length === 1 ? "" : "s"} in parallel
                </>
              )}
            </div>
            <div className="flex items-center gap-2">
              {running && (
                <Button variant="outline" size="sm" onClick={handleStop}>
                  <Square className="h-3.5 w-3.5" />
                  Stop
                </Button>
              )}
              <Button onClick={handleAsk} disabled={!canAsk || running}>
                <Play className="h-3.5 w-3.5" />
                Ask
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Section B — Result columns */}
      {activeQuestion && (
        <div
          className={cn(
            "grid gap-4",
            selectedBackends.length === 1
              ? "grid-cols-1"
              : selectedBackends.length === 2
                ? "grid-cols-1 lg:grid-cols-2"
                : "grid-cols-1 lg:grid-cols-2 xl:grid-cols-3",
          )}
        >
          {selectedBackends.map((backendId) => {
            const backend = backends.find((b) => b.id === backendId);
            if (!backend || !selectedRoom) return null;
            return (
              <ResultColumn
                key={`${selectedRoom}::${backendId}::${activeQuestion}`}
                roomId={selectedRoom}
                backend={backend}
                question={activeQuestion}
                ensureConversation={ensureConversation}
                onComplete={handleColumnComplete}
                registerStop={(fn) => {
                  stopRefs.current[backendId] = fn;
                }}
              />
            );
          })}
        </div>
      )}

      {/* Section C — Question history */}
      {history.length > 0 && (
        <Card>
          <CardHeader className="py-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <MessageSquare className="h-4 w-4 text-muted-foreground" />
              Recent questions
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 py-0 pb-3">
            {history.map((q, i) => (
              <button
                key={i}
                onClick={() => setQuestion(q)}
                className="block w-full truncate rounded px-2 py-1 text-left text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                {q}
              </button>
            ))}
          </CardContent>
        </Card>
      )}

      {!activeQuestion && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center gap-2 py-12 text-center">
            <p className="text-sm font-medium">Ready to ask</p>
            <p className="text-xs text-muted-foreground max-w-md">
              Pick a room, choose one or more backends, and type a question.
              Each backend runs in parallel so you can compare answers side-by-side.
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// Avoid unused-var warning in the import.
void Badge;
